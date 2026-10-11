#include "audio_bridge.h"

#include <string.h>

#include "config.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "audio";

// Downlink jitter buffer: playback starts once 3 frames are queued and holds up
// to 50. On underrun the task plays silence and waits for 3 frames again.
#define DOWNLINK_CAPACITY 50
#define DOWNLINK_PRIME 3

static i2s_chan_handle_t s_tx, s_rx;
static TaskHandle_t s_task;
static volatile bool s_running;
static audio_sink_fn s_sink;

static uint8_t s_ring[DOWNLINK_CAPACITY][AUDIO_FRAME_BYTES];
static size_t s_head, s_count;
static bool s_playing;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

static const uint8_t SILENCE[AUDIO_FRAME_BYTES];

bool audio_bridge_push_downlink(const uint8_t *frame, size_t len)
{
    if (len != AUDIO_FRAME_BYTES) {
        return false;
    }
    bool ok = false;
    portENTER_CRITICAL(&s_mux);
    if (s_count < DOWNLINK_CAPACITY) {
        memcpy(s_ring[(s_head + s_count) % DOWNLINK_CAPACITY], frame, AUDIO_FRAME_BYTES);
        s_count++;
        ok = true;
    }
    portEXIT_CRITICAL(&s_mux);
    return ok;
}

void audio_bridge_clear_downlink(void)
{
    portENTER_CRITICAL(&s_mux);
    s_head = 0;
    s_count = 0;
    s_playing = false;
    portEXIT_CRITICAL(&s_mux);
}

size_t audio_bridge_downlink_level(void)
{
    portENTER_CRITICAL(&s_mux);
    size_t n = s_count;
    portEXIT_CRITICAL(&s_mux);
    return n;
}

// Copies the next frame to play into out; silence if not primed or empty.
static void next_downlink(uint8_t *out)
{
    bool have = false;
    portENTER_CRITICAL(&s_mux);
    if (!s_playing && s_count >= DOWNLINK_PRIME) {
        s_playing = true;
    }
    if (s_playing && s_count > 0) {
        memcpy(out, s_ring[s_head], AUDIO_FRAME_BYTES);
        s_head = (s_head + 1) % DOWNLINK_CAPACITY;
        s_count--;
        have = true;
    } else {
        s_playing = false;  // underrun: re-prime
    }
    portEXIT_CRITICAL(&s_mux);
    if (!have) {
        memcpy(out, SILENCE, AUDIO_FRAME_BYTES);
    }
}

// Highest-priority task. The module's PCM clock paces it: each read returns
// one 20 ms frame. It never waits on the network or the card.
static void audio_task(void *arg)
{
    int16_t rx[AUDIO_FRAME_SAMPLES];
    uint8_t tx[AUDIO_FRAME_BYTES];
    while (true) {
        if (!s_running) {
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            continue;
        }
        size_t got = 0;
        esp_err_t err = i2s_channel_read(s_rx, rx, sizeof(rx), &got, pdMS_TO_TICKS(100));
        if (err != ESP_OK || got != sizeof(rx)) {
            // No PCM clock: the call has ended or the module is not sending.
            continue;
        }
        if (s_sink) {
            s_sink(rx);
        }
        next_downlink(tx);
        size_t written = 0;
        i2s_channel_write(s_tx, tx, sizeof(tx), &written, pdMS_TO_TICKS(40));
    }
}

esp_err_t audio_bridge_init(void)
{
    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_SLAVE);
    chan.dma_desc_num = 4;
    chan.dma_frame_num = AUDIO_FRAME_SAMPLES;
    ESP_ERROR_CHECK(i2s_new_channel(&chan, &s_tx, &s_rx));

    // PCM short-frame timing: one-bit sync pulse, data starts right after it.
    // Confirm bit clock and sync with a logic analyser on the bench (phase 3);
    // the module drives both, so only the slot format must match.
    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(8000),
        .slot_cfg = I2S_STD_PCM_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PIN_I2S_BCLK,
            .ws = PIN_I2S_WS,
            .dout = PIN_I2S_DOUT,
            .din = PIN_I2S_DIN,
            .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false},
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &std));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx, &std));
    xTaskCreatePinnedToCore(audio_task, "audio", 4096, NULL, configMAX_PRIORITIES - 2,
                            &s_task, 1);
    ESP_LOGI(TAG, "I2S slave ready (PCM short frame, 8 kHz, 16-bit mono)");
    return ESP_OK;
}

void audio_bridge_start(audio_sink_fn sink)
{
    if (s_running) {
        s_sink = sink;
        return;
    }
    audio_bridge_clear_downlink();
    s_sink = sink;
    i2s_channel_enable(s_rx);
    i2s_channel_enable(s_tx);
    s_running = true;
    xTaskNotifyGive(s_task);
    ESP_LOGI(TAG, "bridge started");
}

void audio_bridge_stop(void)
{
    if (!s_running) {
        return;
    }
    s_running = false;
    s_sink = NULL;
    vTaskDelay(pdMS_TO_TICKS(120));  // let the task leave its blocking read
    i2s_channel_disable(s_tx);
    i2s_channel_disable(s_rx);
    audio_bridge_clear_downlink();
    ESP_LOGI(TAG, "bridge stopped");
}

bool audio_bridge_running(void) { return s_running; }
