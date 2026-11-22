// GSM module control over UART: AT commands, RING and caller ID, SMS.
// Only the modem task writes to the UART; other tasks queue requests.
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

typedef struct {
    bool alive;        // answers AT
    bool registered;   // +CREG/+CEREG home or roaming
    int rssi;          // +CSQ value, 0..31, 99 unknown
} modem_status_t;

// Starts the UART and the modem task. Call events (EV_INCOMING, EV_NO_CARRIER)
// are posted to call_events as call_event_t.
esp_err_t modem_init(QueueHandle_t call_events);

// Sends one command and waits for OK or ERROR. resp receives the lines before
// the final result (may be NULL).
bool modem_cmd(const char *cmd, char *resp, size_t resp_len, uint32_t timeout_ms);

bool modem_answer(void);   // ATA
bool modem_hangup(void);   // AT+CHUP

// Queues a text. id is reported back to the PC in sms_result (-1: local, not reported).
void modem_queue_sms(const char *to, const char *text, int id);

// Called with (id, ok) after each SMS attempt.
void modem_set_sms_result_cb(void (*cb)(int id, bool ok));

modem_status_t modem_status(void);
