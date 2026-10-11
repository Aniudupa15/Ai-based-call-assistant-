// Voicemail mode and the microSD card.
//
// Card layout:
//   /greet/greeting.wav   voicemail greeting, 8 kHz 16-bit mono
//   /greet/apology.wav    played if the PC link drops mid-call
//   /greet/beep.wav
//   /vm/20261012-143210_9198xxxxxxxx.wav
//   /vm/index.txt         one line per recording: file,caller,start,seconds,uploaded
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#define VM_FILE_LEN 64

typedef struct {
    char file[VM_FILE_LEN];  // path relative to /vm
    char caller[24];
    int64_t start;           // epoch seconds
    int seconds;
    bool uploaded;
} vm_entry_t;

esp_err_t voicemail_init(void);

// Card mounted with at least VM_MIN_FREE_MB free.
bool voicemail_card_ok(void);
int voicemail_free_mb(void);

// Answered call: plays greeting and beep, then records the caller to the card.
// Posts EV_TIME_LIMIT to call_events at VM_MAX_SECONDS.
void voicemail_start(const char *number, QueueHandle_t call_events);

// Closes the recording, adds it to the index and stops the audio bridge.
// Returns the seconds recorded.
int voicemail_stop(void);

// Plays /greet/apology.wav into the running bot call; blocks until it has played.
void voicemail_play_apology(void);

int voicemail_pending_count(void);
int voicemail_list(vm_entry_t *out, int max);
void voicemail_mark_uploaded(const char *file);

// Full path of a recording, for upload.
void voicemail_path(const char *file, char *out, int len);
