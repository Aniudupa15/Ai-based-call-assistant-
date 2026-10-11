// Call audio between the module's PCM port and the rest of the firmware.
// 8 kHz, 16-bit signed, mono, little-endian; one frame is 160 samples, 320 bytes, 20 ms.
// The module is PCM master; the ESP32 runs I2S as slave in PCM short-frame mode.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#define AUDIO_FRAME_SAMPLES 160
#define AUDIO_FRAME_BYTES 320

// Receives each caller frame (from the audio task; must not block).
typedef void (*audio_sink_fn)(const int16_t *frame);

esp_err_t audio_bridge_init(void);

// Starts moving audio: caller frames go to sink, bot frames come from the
// downlink buffer (silence when it is empty).
void audio_bridge_start(audio_sink_fn sink);
void audio_bridge_stop(void);
bool audio_bridge_running(void);

// Bot audio, from the PC or the voicemail player. Returns false if the buffer is full.
bool audio_bridge_push_downlink(const uint8_t *frame, size_t len);

// Drops all buffered bot audio at once (barge-in).
void audio_bridge_clear_downlink(void);

// Frames waiting to be played.
size_t audio_bridge_downlink_level(void);
