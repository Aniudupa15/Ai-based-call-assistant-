// Start-up and the call task.
//
// Four tasks: modem (UART), audio (I2S, highest priority), link (Wi-Fi WebSocket)
// and call (this file), which runs the state machine in call_fsm.c and carries out
// the actions it returns. The firmware keeps working with no PC and no Wi-Fi:
// calls then go to voicemail on the card.
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "app.h"
#include "audio_bridge.h"
#include "call_fsm.h"
#include "config.h"
#include "driver/gpio.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "freertos/timers.h"
#include "modem.h"
#include "nvs_flash.h"
#include "pc_link.h"
#include "voicemail.h"

static const char *TAG = "call";

// Internal events, sent on the same queue as call_event_t but never given to the FSM.
#define EV_TICK ((call_event_type_t)100)
#define EV_HEALTH ((call_event_type_t)101)
#define TICK_MS 500

static QueueHandle_t s_events;
static call_fsm_t s_fsm;
static TimerHandle_t s_decision_timer, s_call_timer, s_tick_timer, s_health_timer;
static int64_t s_last_ring_ms;
static int s_vm_seconds;
static EventGroupHandle_t s_wifi;
#define WIFI_UP BIT0

static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

bool app_wifi_connected(void) { return (xEventGroupGetBits(s_wifi) & WIFI_UP) != 0; }

bool app_call_idle(void) { return s_fsm.state == CALL_IDLE; }

// ---- timers -------------------------------------------------------------------------

static void post_type(call_event_type_t type)
{
    call_event_t ev = {.type = type};
    xQueueSend(s_events, &ev, 0);
}

static void timer_cb(TimerHandle_t t)
{
    post_type((call_event_type_t)(intptr_t)pvTimerGetTimerID(t));
}

static void start_timer(TimerHandle_t t, uint32_t ms)
{
    xTimerChangePeriod(t, pdMS_TO_TICKS(ms), 0);  // also starts it
}

static void stop_call_timers(void)
{
    xTimerStop(s_decision_timer, 0);
    xTimerStop(s_call_timer, 0);
}

// ---- actions --------------------------------------------------------------------------

static void handle(const call_event_t *ev);

static void sms_time(char *out, size_t len)
{
    time_t t = time(NULL);
    struct tm tm;
    localtime_r(&t, &tm);
    strftime(out, len, "%H:%M %d/%m", &tm);
}

static void run_actions(uint32_t acts)
{
    char text[161], when[16];

    if (acts & ACT_SEND_CALL_START) {
        pc_link_send_call_start(s_fsm.call_id, s_fsm.number, (int64_t)time(NULL));
    }
    if (acts & ACT_START_DECISION_TIMER) {
        start_timer(s_decision_timer, DECISION_TIMEOUT_MS);
    }
    if (acts & ACT_REJECT) {
        xTimerStop(s_decision_timer, 0);
        modem_hangup();
    }
    if (acts & ACT_ANSWER) {
        xTimerStop(s_decision_timer, 0);
        bool ok = modem_answer();
        call_event_t ev = {.type = ok ? EV_ANSWER_OK : EV_ANSWER_FAILED};
        handle(&ev);
        return;  // the nested handle() carried out everything that follows
    }
    if (acts & ACT_START_BRIDGE) {
        pc_link_set_call_active(true);
        audio_bridge_start(pc_link_uplink_push);
    }
    if (acts & ACT_SEND_CALL_ANSWERED) {
        pc_link_send_call_answered(s_fsm.call_id);
    }
    if (acts & ACT_START_CALL_TIMER) {
        start_timer(s_call_timer, BOT_CALL_MAX_SECONDS * 1000);
    }
    if (acts & ACT_START_VOICEMAIL) {
        voicemail_start(s_fsm.number, s_events);
    }
    if (acts & ACT_STOP_BRIDGE) {
        pc_link_set_call_active(false);
        if (!(acts & ACT_PLAY_APOLOGY)) {
            audio_bridge_stop();
        }
    }
    if (acts & ACT_STOP_VOICEMAIL) {
        s_vm_seconds = voicemail_stop();
    }
    if (acts & ACT_PLAY_APOLOGY) {
        voicemail_play_apology();
        audio_bridge_stop();
    }
    if (acts & ACT_HANGUP) {
        modem_hangup();
    }
    if (acts & ACT_SEND_CALL_END) {
        pc_link_send_call_end(s_fsm.call_id, s_fsm.end_reason);
    }
    if (acts & ACT_QUEUE_VM_SMS) {
        sms_time(when, sizeof(when));
        snprintf(text, sizeof(text), "Voicemail from %s at %s, %ds",
                 s_fsm.number[0] ? s_fsm.number : "withheld", when, s_vm_seconds);
        modem_queue_sms(OWNER_NUMBER, text, -1);
    }
    if (acts & ACT_QUEUE_MISSED_SMS) {
        sms_time(when, sizeof(when));
        snprintf(text, sizeof(text), "Missed call from %s at %s (voicemail unavailable)",
                 s_fsm.number[0] ? s_fsm.number : "withheld", when);
        modem_queue_sms(OWNER_NUMBER, text, -1);
    }
    if (acts & ACT_CLEANUP) {
        stop_call_timers();
        call_event_t ev = {.type = EV_CLEANUP_DONE};
        handle(&ev);
    }
}

static void handle(const call_event_t *ev)
{
    s_fsm.link_up = pc_link_connected();
    s_fsm.sd_ok = voicemail_card_ok();
    call_state_t before = s_fsm.state;
    uint32_t acts = call_fsm_handle(&s_fsm, ev);
    if (s_fsm.state != before) {
        ESP_LOGI(TAG, "[%s] %s -> %s", s_fsm.call_id, call_state_name(before),
                 call_state_name(s_fsm.state));
    }
    if (acts) {
        run_actions(acts);
    }
}

// ---- periodic checks ------------------------------------------------------------------

static void send_health(void);

static void on_tick(void)
{
    static bool was_connected;
    int64_t now = now_ms();
    bool connected = pc_link_connected();
    if (connected && !was_connected) {
        send_health();  // the PC gets a report as soon as the link is up
    }
    was_connected = connected;
    if (s_fsm.state == CALL_BOT && now - pc_link_last_rx_ms() > PC_SILENT_MS) {
        ESP_LOGW(TAG, "[%s] PC silent for %d ms", s_fsm.call_id, PC_SILENT_MS);
        call_event_t ev = {.type = EV_LINK_DOWN};
        handle(&ev);
    } else if (s_fsm.state == CALL_RINGING && s_fsm.left_ringing &&
               now - s_last_ring_ms > RING_STOPPED_MS) {
        call_event_t ev = {.type = EV_RING_STOPPED};
        handle(&ev);
    }
}

static bool on_battery(void)
{
#if PIN_POWER_SENSE >= 0
    return gpio_get_level(PIN_POWER_SENSE) == 0;
#else
    return false;
#endif
}

static void send_health(void)
{
    modem_status_t st = modem_status();
    pc_link_send_health(st.registered, st.rssi, voicemail_free_mb(), on_battery());
}

static void call_task(void *arg)
{
    call_event_t ev;
    while (true) {
        if (xQueueReceive(s_events, &ev, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        if (ev.type == EV_TICK) {
            on_tick();
            continue;
        }
        if (ev.type == EV_HEALTH) {
            send_health();
            continue;
        }
        if (ev.type == EV_INCOMING) {
            s_last_ring_ms = now_ms();
            if (s_fsm.state == CALL_IDLE) {
                ESP_LOGI(TAG, "incoming call from %s", ev.number[0] ? ev.number : "withheld");
            }
        }
        handle(&ev);
    }
}

// ---- Wi-Fi ----------------------------------------------------------------------------

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && (id == WIFI_EVENT_STA_START || id == WIFI_EVENT_STA_DISCONNECTED)) {
        xEventGroupClearBits(s_wifi, WIFI_UP);
        esp_wifi_connect();  // keep trying; the router may reboot
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(s_wifi, WIFI_UP);
        ESP_LOGI(TAG, "Wi-Fi connected");
    }
}

static void wifi_start(void)
{
    s_wifi = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));
    wifi_config_t cfg = {0};
    strncpy((char *)cfg.sta.ssid, WIFI_SSID, sizeof(cfg.sta.ssid) - 1);
    strncpy((char *)cfg.sta.password, WIFI_PASS, sizeof(cfg.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));  // power save adds audio latency
    ESP_ERROR_CHECK(esp_wifi_start());
}

// ---- start-up -------------------------------------------------------------------------

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }
    setenv("TZ", LOCAL_TZ, 1);
    tzset();

#if PIN_POWER_SENSE >= 0
    gpio_config_t sense = {.pin_bit_mask = 1ULL << PIN_POWER_SENSE, .mode = GPIO_MODE_INPUT};
    gpio_config(&sense);
#endif

    s_events = xQueueCreate(16, sizeof(call_event_t));
    s_decision_timer = xTimerCreate("decision", pdMS_TO_TICKS(DECISION_TIMEOUT_MS), pdFALSE,
                                    (void *)(intptr_t)EV_DECISION_TIMEOUT, timer_cb);
    s_call_timer = xTimerCreate("call_max", pdMS_TO_TICKS(BOT_CALL_MAX_SECONDS * 1000), pdFALSE,
                                (void *)(intptr_t)EV_TIME_LIMIT, timer_cb);
    s_tick_timer = xTimerCreate("tick", pdMS_TO_TICKS(TICK_MS), pdTRUE,
                                (void *)(intptr_t)EV_TICK, timer_cb);
    s_health_timer = xTimerCreate("health", pdMS_TO_TICKS(HEALTH_INTERVAL_MS), pdTRUE,
                                  (void *)(intptr_t)EV_HEALTH, timer_cb);
    call_fsm_init(&s_fsm);

    voicemail_init();  // a missing card is logged; calls are still texted
    ESP_ERROR_CHECK(audio_bridge_init());
    wifi_start();
    ESP_ERROR_CHECK(modem_init(s_events));
    modem_set_sms_result_cb(pc_link_send_sms_result);
    ESP_ERROR_CHECK(pc_link_start(s_events));

    xTaskCreatePinnedToCore(call_task, "call", 6144, NULL, 9, NULL, 0);
    xTimerStart(s_tick_timer, 0);
    xTimerStart(s_health_timer, 0);
    ESP_LOGI(TAG, "call assistant firmware %s ready", FW_VERSION);
}
