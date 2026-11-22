#include "pc_link.h"

#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "app.h"
#include "audio_bridge.h"
#include "call_fsm.h"
#include "cJSON.h"
#include "config.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "freertos/task.h"
#include "modem.h"
#include "sync.h"
#include "voicemail.h"

static const char *TAG = "pc_link";

#define UPLINK_FRAMES 10      // ring buffer of caller frames; oldest dropped when full
#define RX_TEXT_MAX 1024
#define CONNECT_WAIT_MS 5000
#define BACKOFF_MAX_S 30
#define SYNC_EVERY_MS 30000

typedef struct {
    int16_t s[AUDIO_FRAME_SAMPLES];
} frame_t;

static esp_websocket_client_handle_t s_client;
static QueueHandle_t s_events;
static QueueHandle_t s_uplink;
static volatile bool s_connected;
static volatile bool s_call_active;
static volatile bool s_clock_set;
static volatile int64_t s_last_rx_ms;
static char s_rx[RX_TEXT_MAX + 1];

static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

bool pc_link_connected(void) { return s_connected; }
int64_t pc_link_last_rx_ms(void) { return s_last_rx_ms; }
bool pc_link_clock_set(void) { return s_clock_set; }

void pc_link_set_call_active(bool active)
{
    s_call_active = active;
    if (!active) {
        xQueueReset(s_uplink);
    }
    s_last_rx_ms = now_ms();
}

void pc_link_uplink_push(const int16_t *frame)
{
    if (!s_call_active) {
        return;
    }
    if (xQueueSend(s_uplink, frame, 0) != pdTRUE) {
        frame_t drop;
        xQueueReceive(s_uplink, &drop, 0);  // link is slow: drop the oldest frame
        xQueueSend(s_uplink, frame, 0);
    }
}

// ---- sending JSON ---------------------------------------------------------------

static void send_json(cJSON *obj)
{
    char *text = cJSON_PrintUnformatted(obj);
    cJSON_Delete(obj);
    if (!text) {
        return;
    }
    if (s_connected) {
        if (esp_websocket_client_send_text(s_client, text, strlen(text), pdMS_TO_TICKS(500)) < 0) {
            ESP_LOGW(TAG, "send failed: %s", text);
        }
    } else {
        ESP_LOGW(TAG, "offline, dropped: %s", text);
    }
    cJSON_free(text);
}

static cJSON *msg(const char *type)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "type", type);
    return o;
}

static void send_hello(void)
{
    uint8_t mac[6];
    char device[20];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(device, sizeof(device), "esp-%02x%02x%02x", mac[3], mac[4], mac[5]);
    cJSON *o = msg("hello");
    cJSON_AddStringToObject(o, "device_id", device);
    cJSON_AddStringToObject(o, "fw_version", FW_VERSION);
    cJSON_AddStringToObject(o, "proto", PROTOCOL_VERSION);
    cJSON_AddNumberToObject(o, "vm_pending", voicemail_pending_count());
    send_json(o);
}

void pc_link_send_call_start(const char *call_id, const char *number, int64_t ts)
{
    cJSON *o = msg("call_start");
    cJSON_AddStringToObject(o, "call_id", call_id);
    cJSON_AddStringToObject(o, "number", number);
    cJSON_AddNumberToObject(o, "ts", (double)ts);
    send_json(o);
}

void pc_link_send_call_answered(const char *call_id)
{
    cJSON *o = msg("call_answered");
    cJSON_AddStringToObject(o, "call_id", call_id);
    send_json(o);
}

void pc_link_send_call_end(const char *call_id, const char *reason)
{
    cJSON *o = msg("call_end");
    cJSON_AddStringToObject(o, "call_id", call_id);
    cJSON_AddStringToObject(o, "reason", reason ? reason : "caller_hangup");
    send_json(o);
}

void pc_link_send_health(bool registered, int rssi, int sd_free_mb, bool on_battery)
{
    cJSON *o = msg("health");
    cJSON_AddBoolToObject(o, "registered", registered);
    cJSON_AddNumberToObject(o, "rssi", rssi);
    cJSON_AddNumberToObject(o, "sd_free_mb", sd_free_mb);
    cJSON_AddBoolToObject(o, "on_battery", on_battery);
    send_json(o);
}

void pc_link_send_sms_result(int id, bool ok)
{
    cJSON *o = msg("sms_result");
    cJSON_AddNumberToObject(o, "id", id);
    cJSON_AddBoolToObject(o, "ok", ok);
    send_json(o);
}

// ---- receiving --------------------------------------------------------------------

static const char *str_field(cJSON *root, const char *name)
{
    cJSON *v = cJSON_GetObjectItemCaseSensitive(root, name);
    return cJSON_IsString(v) ? v->valuestring : "";
}

static void post(call_event_t *ev)
{
    if (xQueueSend(s_events, ev, pdMS_TO_TICKS(100)) != pdTRUE) {
        ESP_LOGE(TAG, "call event queue full");
    }
}

static void handle_text(const char *text)
{
    cJSON *root = cJSON_Parse(text);
    if (!root) {
        ESP_LOGW(TAG, "bad JSON: %.80s", text);
        return;
    }
    const char *type = str_field(root, "type");
    call_event_t ev = {0};
    strncpy(ev.call_id, str_field(root, "call_id"), sizeof(ev.call_id) - 1);

    if (strcmp(type, "time") == 0) {
        cJSON *epoch = cJSON_GetObjectItemCaseSensitive(root, "epoch");
        if (cJSON_IsNumber(epoch)) {
            struct timeval tv = {.tv_sec = (time_t)epoch->valuedouble};
            settimeofday(&tv, NULL);
            s_clock_set = true;
        }
    } else if (strcmp(type, "decision") == 0) {
        const char *action = str_field(root, "action");
        cJSON *max = cJSON_GetObjectItemCaseSensitive(root, "max_seconds");
        ev.type = EV_DECISION;
        ev.action = strcmp(action, "answer") == 0   ? DECISION_ANSWER
                    : strcmp(action, "reject") == 0 ? DECISION_REJECT
                                                    : DECISION_IGNORE;
        ev.max_seconds = cJSON_IsNumber(max) ? (uint32_t)max->valuedouble : 0;
        post(&ev);
    } else if (strcmp(type, "clear_audio") == 0) {
        audio_bridge_clear_downlink();  // barge-in: act at once, not via the call task
    } else if (strcmp(type, "hangup") == 0) {
        ev.type = EV_PC_HANGUP;
        post(&ev);
    } else if (strcmp(type, "send_sms") == 0) {
        cJSON *id = cJSON_GetObjectItemCaseSensitive(root, "id");
        modem_queue_sms(str_field(root, "to"), str_field(root, "text"),
                        cJSON_IsNumber(id) ? (int)id->valuedouble : -1);
    } else if (strcmp(type, "error") == 0) {
        ESP_LOGE(TAG, "PC error %s: %s", str_field(root, "code"), str_field(root, "detail"));
    } else {
        ESP_LOGW(TAG, "unknown message type '%s'", type);
    }
    cJSON_Delete(root);
}

static void ws_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    esp_websocket_event_data_t *d = (esp_websocket_event_data_t *)data;
    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        s_connected = true;
        s_last_rx_ms = now_ms();
        ESP_LOGI(TAG, "connected to %s:%d", PC_HOST, PC_PORT);
        send_hello();
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
    case WEBSOCKET_EVENT_CLOSED:
    case WEBSOCKET_EVENT_ERROR:
        if (s_connected) {
            s_connected = false;
            ESP_LOGW(TAG, "link down");
            call_event_t ev = {.type = EV_LINK_DOWN};
            post(&ev);
        }
        break;
    case WEBSOCKET_EVENT_DATA:
        s_last_rx_ms = now_ms();
        if (d->op_code == 0x2) {  // binary: bot audio
            if (s_call_active && d->payload_len == AUDIO_FRAME_BYTES &&
                d->data_len == AUDIO_FRAME_BYTES) {
                audio_bridge_push_downlink((const uint8_t *)d->data_ptr, d->data_len);
            }
        } else if (d->op_code == 0x1 || (d->op_code == 0x0 && d->payload_offset > 0)) {
            // Text, possibly delivered in pieces.
            if (d->payload_len > RX_TEXT_MAX) {
                break;
            }
            if (d->payload_offset + d->data_len <= RX_TEXT_MAX) {
                memcpy(s_rx + d->payload_offset, d->data_ptr, d->data_len);
            }
            if (d->payload_offset + d->data_len >= d->payload_len) {
                s_rx[d->payload_len] = '\0';
                handle_text(s_rx);
            }
        }
        break;
    default:
        break;
    }
}

// ---- task -------------------------------------------------------------------------

static void link_task(void *arg)
{
    int backoff_s = 1;
    bool started = false;
    int64_t last_sync = 0;
    frame_t f;

    while (true) {
        if (!s_connected) {
            if (started) {
                esp_websocket_client_stop(s_client);
                started = false;
            }
            if (!app_wifi_connected()) {
                vTaskDelay(pdMS_TO_TICKS(500));
                continue;
            }
            esp_websocket_client_start(s_client);
            started = true;
            int64_t deadline = now_ms() + CONNECT_WAIT_MS;
            while (!s_connected && now_ms() < deadline) {
                vTaskDelay(pdMS_TO_TICKS(50));
            }
            if (!s_connected) {
                ESP_LOGW(TAG, "PC not reachable; retry in %d s", backoff_s);
                esp_websocket_client_stop(s_client);
                started = false;
                vTaskDelay(pdMS_TO_TICKS(backoff_s * 1000));
                backoff_s = backoff_s * 2 > BACKOFF_MAX_S ? BACKOFF_MAX_S : backoff_s * 2;
                continue;
            }
            backoff_s = 1;
        }

        // Send caller audio as it arrives.
        while (xQueueReceive(s_uplink, &f, pdMS_TO_TICKS(20)) == pdTRUE) {
            if (s_call_active && s_connected) {
                esp_websocket_client_send_bin(s_client, (const char *)f.s, AUDIO_FRAME_BYTES,
                                              pdMS_TO_TICKS(50));
            }
        }

        // Upload stored voicemail while nothing else is happening.
        if (app_call_idle() && now_ms() - last_sync > SYNC_EVERY_MS &&
            voicemail_pending_count() > 0) {
            last_sync = now_ms();
            sync_run();
        }
    }
}

esp_err_t pc_link_start(QueueHandle_t call_events)
{
    static char uri[96];
    s_events = call_events;
    s_uplink = xQueueCreate(UPLINK_FRAMES, sizeof(frame_t));
    snprintf(uri, sizeof(uri), "ws://%s:%d/esp", PC_HOST, PC_PORT);

    esp_websocket_client_config_t cfg = {
        .uri = uri,
        .headers = "X-Token: " ESP_TOKEN "\r\n",
        .disable_auto_reconnect = true,   // the link task reconnects with backoff
        .ping_interval_sec = 5,
        .pingpong_timeout_sec = 15,       // three missed pings: link down
        .network_timeout_ms = 5000,
        .buffer_size = 1024,
        .task_stack = 6144,
        .task_prio = 9,
    };
    s_client = esp_websocket_client_init(&cfg);
    if (!s_client) {
        return ESP_FAIL;
    }
    esp_websocket_register_events(s_client, WEBSOCKET_EVENT_ANY, ws_event, NULL);
    xTaskCreatePinnedToCore(link_task, "pc_link", 6144, NULL, 8, NULL, 0);
    return ESP_OK;
}
