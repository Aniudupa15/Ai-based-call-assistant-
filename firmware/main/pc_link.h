// Wi-Fi WebSocket link to the PC: JSON control messages and binary audio frames.
// docs/protocol.md is the source of truth for message names and fields.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#define PROTOCOL_VERSION "1.0"
#define FW_VERSION "1.0.0"

// Starts the link task. Decisions, hangups and link loss are posted to
// call_events as call_event_t.
esp_err_t pc_link_start(QueueHandle_t call_events);

bool pc_link_connected(void);

// Milliseconds (esp_timer clock) when anything last arrived from the PC.
int64_t pc_link_last_rx_ms(void);

// While true, caller frames pushed with pc_link_uplink_push are sent, and bot
// frames from the PC are passed to the audio bridge.
void pc_link_set_call_active(bool active);

// Audio sink for bot calls: queues one caller frame for sending (drops the
// oldest if the link is slow).
void pc_link_uplink_push(const int16_t *frame);

void pc_link_send_call_start(const char *call_id, const char *number, int64_t ts);
void pc_link_send_call_answered(const char *call_id);
void pc_link_send_call_end(const char *call_id, const char *reason);
void pc_link_send_health(bool registered, int rssi, int sd_free_mb, bool on_battery);
void pc_link_send_sms_result(int id, bool ok);

// Set once the PC's time message has set the clock.
bool pc_link_clock_set(void);
