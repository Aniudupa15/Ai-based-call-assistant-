#include "modem.h"

#include <stdio.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "call_fsm.h"
#include "config.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "modem";

#define UART_PORT UART_NUM_2
#define MODEM_LINE_MAX 256
#define RESP_MAX 512
#define SMS_TEXT_MAX 161
#define CLIP_WAIT_MS 400  // +CLIP normally follows RING at once

typedef enum { REQ_CMD, REQ_SMS } req_type_t;

typedef struct {
    req_type_t type;
    char cmd[64];
    char *resp;
    size_t resp_len;
    uint32_t timeout_ms;
    bool *ok;
    SemaphoreHandle_t done;
} cmd_req_t;

typedef struct {
    char to[NUMBER_LEN];
    char text[SMS_TEXT_MAX];
    int id;
} sms_req_t;

static QueueHandle_t s_events;
static QueueHandle_t s_cmds;
static QueueHandle_t s_sms;
static modem_status_t s_status = {.rssi = 99};
static void (*s_sms_cb)(int id, bool ok);

static char s_line[MODEM_LINE_MAX];
static size_t s_line_len;
static bool s_ring_pending;
static int64_t s_ring_at_us;

static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

// ---- line reading -------------------------------------------------------------

// Reads one line (without CR/LF) or the SMS prompt "> ". Returns false on timeout.
static bool read_line(char *out, size_t len, uint32_t timeout_ms)
{
    int64_t deadline = now_ms() + timeout_ms;
    while (true) {
        uint8_t c;
        int64_t left = deadline - now_ms();
        if (left < 0) {
            return false;
        }
        int n = uart_read_bytes(UART_PORT, &c, 1, pdMS_TO_TICKS(left > 20 ? 20 : left));
        if (n <= 0) {
            continue;
        }
        if (c == '\r') {
            continue;
        }
        if (c == '\n') {
            if (s_line_len == 0) {
                continue;  // blank line
            }
            s_line[s_line_len] = '\0';
            strncpy(out, s_line, len - 1);
            out[len - 1] = '\0';
            s_line_len = 0;
            return true;
        }
        if (s_line_len < MODEM_LINE_MAX - 1) {
            s_line[s_line_len++] = (char)c;
        }
        if (s_line_len == 2 && s_line[0] == '>' && s_line[1] == ' ') {
            strncpy(out, "> ", len);
            s_line_len = 0;
            return true;
        }
    }
}

static void write_raw(const char *s) { uart_write_bytes(UART_PORT, s, strlen(s)); }

// ---- unsolicited lines --------------------------------------------------------

static void post_event(call_event_type_t type, const char *number)
{
    call_event_t ev = {.type = type};
    if (number) {
        strncpy(ev.number, number, sizeof(ev.number) - 1);
    }
    if (type == EV_INCOMING) {
        time_t t = time(NULL);
        struct tm tm;
        localtime_r(&t, &tm);
        strftime(ev.call_id, sizeof(ev.call_id), "c-%Y%m%d-%H%M%S", &tm);
    }
    xQueueSend(s_events, &ev, 0);
}

// Handles lines the module sends without being asked. Returns true if consumed.
static bool handle_urc(const char *line)
{
    if (strcmp(line, "RING") == 0) {
        s_ring_pending = true;
        s_ring_at_us = esp_timer_get_time();
        return true;
    }
    if (strncmp(line, "+CLIP:", 6) == 0) {
        // +CLIP: "+919812345678",145,"",0,"",0   (empty number when withheld)
        char number[NUMBER_LEN] = "";
        const char *q1 = strchr(line, '"');
        const char *q2 = q1 ? strchr(q1 + 1, '"') : NULL;
        if (q1 && q2 && q2 > q1 + 1) {
            size_t n = (size_t)(q2 - q1 - 1);
            if (n >= sizeof(number)) {
                n = sizeof(number) - 1;
            }
            memcpy(number, q1 + 1, n);
            number[n] = '\0';
        }
        s_ring_pending = false;
        post_event(EV_INCOMING, number);
        return true;
    }
    if (strcmp(line, "NO CARRIER") == 0 || strncmp(line, "VOICE CALL: END", 15) == 0 ||
        strncmp(line, "MISSED_CALL", 11) == 0 || strcmp(line, "BUSY") == 0) {
        s_ring_pending = false;
        post_event(EV_NO_CARRIER, NULL);
        return true;
    }
    if (strncmp(line, "VOICE CALL: BEGIN", 17) == 0 || strncmp(line, "+CMTI:", 6) == 0 ||
        strncmp(line, "+CPIN:", 6) == 0 || strcmp(line, "SMS DONE") == 0 ||
        strcmp(line, "PB DONE") == 0 || strncmp(line, "+CREG:", 6) == 0 ||
        strncmp(line, "+CEREG:", 7) == 0 || strcmp(line, "RDY") == 0) {
        return true;  // informational
    }
    return false;
}

static void check_ring_without_clip(void)
{
    if (s_ring_pending && esp_timer_get_time() - s_ring_at_us > CLIP_WAIT_MS * 1000) {
        s_ring_pending = false;
        post_event(EV_INCOMING, "");  // no caller ID: withheld
    }
}

// ---- commands -----------------------------------------------------------------

// Runs a command inside the modem task. URCs that arrive meanwhile are handled.
static bool run_cmd(const char *cmd, char *resp, size_t resp_len, uint32_t timeout_ms)
{
    char line[MODEM_LINE_MAX];
    size_t used = 0;
    if (resp && resp_len) {
        resp[0] = '\0';
    }
    write_raw(cmd);
    write_raw("\r");
    int64_t deadline = now_ms() + timeout_ms;
    while (now_ms() < deadline) {
        if (!read_line(line, sizeof(line), (uint32_t)(deadline - now_ms()))) {
            break;
        }
        if (strcmp(line, cmd) == 0) {
            continue;  // echo
        }
        if (strcmp(line, "OK") == 0) {
            return true;
        }
        if (strcmp(line, "ERROR") == 0 || strncmp(line, "+CME ERROR", 10) == 0 ||
            strncmp(line, "+CMS ERROR", 10) == 0 || strcmp(line, "NO CARRIER") == 0) {
            if (strcmp(line, "NO CARRIER") == 0) {
                handle_urc(line);
            }
            ESP_LOGW(TAG, "%s -> %s", cmd, line);
            return false;
        }
        if (handle_urc(line)) {
            continue;
        }
        if (resp && used + strlen(line) + 2 < resp_len) {
            used += (size_t)snprintf(resp + used, resp_len - used, "%s\n", line);
        }
    }
    ESP_LOGW(TAG, "%s -> timeout", cmd);
    return false;
}

static bool run_sms(const sms_req_t *sms)
{
    char line[MODEM_LINE_MAX];
    char cmd[48];
    if (!run_cmd("AT+CMGF=1", NULL, 0, 2000)) {
        return false;
    }
    snprintf(cmd, sizeof(cmd), "AT+CMGS=\"%s\"\r", sms->to);
    write_raw(cmd);
    int64_t deadline = now_ms() + 5000;
    bool prompt = false;
    while (!prompt && now_ms() < deadline) {
        if (!read_line(line, sizeof(line), (uint32_t)(deadline - now_ms()))) {
            break;
        }
        prompt = strcmp(line, "> ") == 0;
        if (!prompt) {
            handle_urc(line);
        }
    }
    if (!prompt) {
        write_raw("\x1b");  // cancel
        return false;
    }
    write_raw(sms->text);
    write_raw("\x1a");  // Ctrl-Z sends
    deadline = now_ms() + 60000;
    while (now_ms() < deadline) {
        if (!read_line(line, sizeof(line), (uint32_t)(deadline - now_ms()))) {
            break;
        }
        if (strcmp(line, "OK") == 0) {
            return true;
        }
        if (strstr(line, "ERROR")) {
            return false;
        }
        handle_urc(line);
    }
    return false;
}

// ---- power and set-up ---------------------------------------------------------

static void pwrkey_pulse(uint32_t ms)
{
    int on = MODEM_PWRKEY_ACTIVE_HIGH ? 1 : 0;
    gpio_set_level(PIN_MODEM_PWRKEY, on);
    vTaskDelay(pdMS_TO_TICKS(ms));
    gpio_set_level(PIN_MODEM_PWRKEY, !on);
}

static bool wait_alive(int tries)
{
    for (int i = 0; i < tries; i++) {
        if (run_cmd("AT", NULL, 0, 1000)) {
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    return false;
}

static void power_cycle(void)
{
    ESP_LOGW(TAG, "power-cycling the module");
    pwrkey_pulse(2600);  // off (SIM7600: >= 2.5 s)
    vTaskDelay(pdMS_TO_TICKS(8000));
    pwrkey_pulse(600);   // on (SIM7600: >= 500 ms)
    vTaskDelay(pdMS_TO_TICKS(12000));
}

static void set_clock_from_network(void)
{
    if (time(NULL) > 1700000000) {
        return;  // already set by the PC's time message
    }
    char resp[RESP_MAX];
    int yy, mo, dd, hh, mi, ss;
    if (!run_cmd("AT+CCLK?", resp, sizeof(resp), 2000)) {
        return;
    }
    // +CCLK: "26/10/12,14:32:05+22" is local time; TZ (LOCAL_TZ) turns it into UTC.
    const char *p = strstr(resp, "+CCLK: \"");
    if (!p || sscanf(p + 8, "%d/%d/%d,%d:%d:%d", &yy, &mo, &dd, &hh, &mi, &ss) < 6) {
        return;
    }
    if (yy < 24) {
        return;  // module has no network time yet
    }
    struct tm tm = {.tm_year = yy + 100, .tm_mon = mo - 1, .tm_mday = dd,
                    .tm_hour = hh, .tm_min = mi, .tm_sec = ss, .tm_isdst = -1};
    struct timeval tv = {.tv_sec = mktime(&tm)};
    settimeofday(&tv, NULL);
    ESP_LOGI(TAG, "clock set from network time");
}

static bool setup_module(void)
{
    if (!wait_alive(10)) {
        return false;
    }
    run_cmd("ATE0", NULL, 0, 1000);
    run_cmd("AT+CLIP=1", NULL, 0, 1000);
    run_cmd("AT+CMGF=1", NULL, 0, 1000);
    run_cmd("AT+CREG=0", NULL, 0, 1000);  // query only; no +CREG URCs
    static const char *pcm[] = MODEM_PCM_SETUP;
    for (int i = 0; pcm[i] != NULL; i++) {
        if (!run_cmd(pcm[i], NULL, 0, 2000)) {
            ESP_LOGE(TAG, "PCM set-up command failed: %s", pcm[i]);
        }
    }
    set_clock_from_network();
    return true;
}

static void refresh_status(void)
{
    char resp[RESP_MAX];
    int n, stat = 0, rssi = 99, ber;
    bool reg = false;
    if (run_cmd("AT+CREG?", resp, sizeof(resp), 2000)) {
        const char *p = strstr(resp, "+CREG:");
        if (p && sscanf(p, "+CREG: %d,%d", &n, &stat) == 2) {
            reg = stat == 1 || stat == 5;
        }
    }
    if (!reg && run_cmd("AT+CEREG?", resp, sizeof(resp), 2000)) {
        const char *p = strstr(resp, "+CEREG:");
        if (p && sscanf(p, "+CEREG: %d,%d", &n, &stat) == 2) {
            reg = stat == 1 || stat == 5;
        }
    }
    if (run_cmd("AT+CSQ", resp, sizeof(resp), 2000)) {
        const char *p = strstr(resp, "+CSQ:");
        if (p) {
            sscanf(p, "+CSQ: %d,%d", &rssi, &ber);
        }
    }
    s_status.registered = reg;
    s_status.rssi = rssi;
}

// ---- task -------------------------------------------------------------------------

static void modem_task(void *arg)
{
    char line[MODEM_LINE_MAX];
    int failures = 0;
    int64_t last_check = 0;

    s_status.alive = setup_module();
    if (!s_status.alive) {
        power_cycle();
        s_status.alive = setup_module();
    }
    refresh_status();
    ESP_LOGI(TAG, "module %s, registered=%d rssi=%d", s_status.alive ? "ready" : "NOT responding",
             s_status.registered, s_status.rssi);

    while (true) {
        if (read_line(line, sizeof(line), 50)) {
            if (!handle_urc(line)) {
                ESP_LOGD(TAG, "unhandled: %s", line);
            }
        }
        check_ring_without_clip();

        cmd_req_t req;
        if (xQueueReceive(s_cmds, &req, 0) == pdTRUE) {
            *req.ok = run_cmd(req.cmd, req.resp, req.resp_len, req.timeout_ms);
            xSemaphoreGive(req.done);
        }

        sms_req_t sms;
        if (xQueueReceive(s_sms, &sms, 0) == pdTRUE) {
            bool ok = run_sms(&sms);
            ESP_LOGI(TAG, "SMS %d to %s: %s", sms.id, sms.to, ok ? "sent" : "failed");
            if (s_sms_cb && sms.id >= 0) {
                s_sms_cb(sms.id, ok);
            }
        }

        // Liveness check, only while no call is ringing.
        if (now_ms() - last_check > MODEM_CHECK_MS && !s_ring_pending) {
            last_check = now_ms();
            if (run_cmd("AT", NULL, 0, 1000)) {
                failures = 0;
                s_status.alive = true;
                refresh_status();
            } else if (++failures >= MODEM_MAX_FAILURES) {
                s_status.alive = false;
                power_cycle();
                s_status.alive = setup_module();
                failures = 0;
            }
        }
    }
}

esp_err_t modem_init(QueueHandle_t call_events)
{
    s_events = call_events;
    s_cmds = xQueueCreate(4, sizeof(cmd_req_t));
    s_sms = xQueueCreate(8, sizeof(sms_req_t));

    gpio_config_t io = {.pin_bit_mask = 1ULL << PIN_MODEM_PWRKEY, .mode = GPIO_MODE_OUTPUT};
    gpio_config(&io);
    gpio_set_level(PIN_MODEM_PWRKEY, MODEM_PWRKEY_ACTIVE_HIGH ? 0 : 1);

    uart_config_t cfg = {
        .baud_rate = MODEM_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(UART_PORT, 2048, 1024, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(UART_PORT, &cfg));
    ESP_ERROR_CHECK(uart_set_pin(UART_PORT, PIN_MODEM_TX, PIN_MODEM_RX, UART_PIN_NO_CHANGE,
                                 UART_PIN_NO_CHANGE));
    xTaskCreatePinnedToCore(modem_task, "modem", 4096, NULL, 10, NULL, 0);
    return ESP_OK;
}

bool modem_cmd(const char *cmd, char *resp, size_t resp_len, uint32_t timeout_ms)
{
    bool ok = false;
    cmd_req_t req = {.type = REQ_CMD, .resp = resp, .resp_len = resp_len,
                     .timeout_ms = timeout_ms, .ok = &ok,
                     .done = xSemaphoreCreateBinary()};
    strncpy(req.cmd, cmd, sizeof(req.cmd) - 1);
    if (xQueueSend(s_cmds, &req, pdMS_TO_TICKS(1000)) != pdTRUE) {
        vSemaphoreDelete(req.done);
        return false;
    }
    // The modem task may be busy with an SMS (up to a minute); wait for it.
    xSemaphoreTake(req.done, pdMS_TO_TICKS(timeout_ms + 70000));
    vSemaphoreDelete(req.done);
    return ok;
}

bool modem_answer(void) { return modem_cmd("ATA", NULL, 0, 5000); }

bool modem_hangup(void) { return modem_cmd("AT+CHUP", NULL, 0, 3000); }

void modem_queue_sms(const char *to, const char *text, int id)
{
    sms_req_t sms = {.id = id};
    strncpy(sms.to, to, sizeof(sms.to) - 1);
    strncpy(sms.text, text, sizeof(sms.text) - 1);
    if (xQueueSend(s_sms, &sms, 0) != pdTRUE) {
        ESP_LOGE(TAG, "SMS queue full; dropped text %d", id);
        if (s_sms_cb && id >= 0) {
            s_sms_cb(id, false);
        }
    }
}

void modem_set_sms_result_cb(void (*cb)(int id, bool ok)) { s_sms_cb = cb; }

modem_status_t modem_status(void) { return s_status; }
