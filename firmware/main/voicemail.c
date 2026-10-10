#include "voicemail.h"

#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "audio_bridge.h"
#include "call_fsm.h"
#include "config.h"
#include "driver/sdspi_host.h"
#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdmmc_cmd.h"

static const char *TAG = "voicemail";

#define MOUNT "/sdcard"
#define VM_DIR MOUNT "/vm"
#define INDEX_PATH VM_DIR "/index.txt"
#define INDEX_TMP VM_DIR "/index.tmp"
#define GREETING MOUNT "/greet/greeting.wav"
#define BEEP MOUNT "/greet/beep.wav"
#define APOLOGY MOUNT "/greet/apology.wav"
#define REC_QUEUE_FRAMES 50   // one second of slack for slow card writes
#define WAV_HEADER 44

typedef struct {
    int16_t s[AUDIO_FRAME_SAMPLES];
} frame_t;

static bool s_mounted;
static QueueHandle_t s_rec_q;
static SemaphoreHandle_t s_done;
static TaskHandle_t s_task;
static QueueHandle_t s_events;
static volatile bool s_recording, s_stop;
static volatile int s_frames;
static char s_number[24];
static char s_file[VM_FILE_LEN];
static time_t s_start;
static int s_pending = -1;  // cached count, -1 = unknown

// ---- WAV helpers ------------------------------------------------------------------

static void put_le(uint8_t *p, uint32_t v, int n)
{
    for (int i = 0; i < n; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

static void wav_header(uint8_t *h, uint32_t data_bytes)
{
    memcpy(h, "RIFF", 4);
    put_le(h + 4, 36 + data_bytes, 4);
    memcpy(h + 8, "WAVEfmt ", 8);
    put_le(h + 16, 16, 4);       // fmt chunk size
    put_le(h + 20, 1, 2);        // PCM
    put_le(h + 22, 1, 2);        // mono
    put_le(h + 24, 8000, 4);     // sample rate
    put_le(h + 28, 16000, 4);    // byte rate
    put_le(h + 32, 2, 2);        // block align
    put_le(h + 34, 16, 2);       // bits per sample
    memcpy(h + 36, "data", 4);
    put_le(h + 40, data_bytes, 4);
}

// Streams a WAV file into the downlink at the pace the audio task plays it.
// Returns false if the file is missing.
static bool play_file(const char *path, volatile bool *abort_flag, int max_ms)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        ESP_LOGW(TAG, "missing %s", path);
        return false;
    }
    fseek(f, WAV_HEADER, SEEK_SET);
    uint8_t frame[AUDIO_FRAME_BYTES];
    int played_ms = 0;
    while (played_ms < max_ms && !(abort_flag && *abort_flag)) {
        size_t n = fread(frame, 1, sizeof(frame), f);
        if (n == 0) {
            break;
        }
        if (n < sizeof(frame)) {
            memset(frame + n, 0, sizeof(frame) - n);
        }
        while (audio_bridge_downlink_level() >= 8 && !(abort_flag && *abort_flag)) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        audio_bridge_push_downlink(frame, sizeof(frame));
        played_ms += 20;
    }
    fclose(f);
    // Wait for the buffer to drain so the next sound follows cleanly.
    for (int i = 0; i < 100 && audio_bridge_downlink_level() > 0; i++) {
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    return true;
}

// ---- index ------------------------------------------------------------------------

static bool parse_line(const char *line, vm_entry_t *e)
{
    int up = 0;
    long long start = 0;
    memset(e, 0, sizeof(*e));
    if (sscanf(line, "%63[^,],%23[^,],%lld,%d,%d", e->file, e->caller, &start, &e->seconds,
               &up) == 5 ||
        sscanf(line, "%63[^,],,%lld,%d,%d", e->file, &start, &e->seconds, &up) == 4) {
        e->start = start;
        e->uploaded = up != 0;
        return true;
    }
    return false;
}

static int count_pending(void)
{
    int n = 0;
    vm_entry_t e;
    char line[160];
    FILE *f = fopen(INDEX_PATH, "r");
    if (!f) {
        return 0;
    }
    while (fgets(line, sizeof(line), f)) {
        if (parse_line(line, &e) && !e.uploaded) {
            n++;
        }
    }
    fclose(f);
    return n;
}

int voicemail_pending_count(void)
{
    if (!s_mounted) {
        return 0;
    }
    if (s_pending < 0) {
        s_pending = count_pending();
    }
    return s_pending;
}

int voicemail_list(vm_entry_t *out, int max)
{
    int n = 0;
    FILE *f = s_mounted ? fopen(INDEX_PATH, "r") : NULL;
    if (!f) {
        return 0;
    }
    char line[160];
    while (n < max && fgets(line, sizeof(line), f)) {
        if (parse_line(line, &out[n])) {
            n++;
        }
    }
    fclose(f);
    return n;
}

void voicemail_mark_uploaded(const char *file)
{
    FILE *in = fopen(INDEX_PATH, "r");
    FILE *out = fopen(INDEX_TMP, "w");
    if (!in || !out) {
        if (in) fclose(in);
        if (out) fclose(out);
        return;
    }
    char line[160];
    vm_entry_t e;
    while (fgets(line, sizeof(line), in)) {
        if (!parse_line(line, &e)) {
            continue;  // drop damaged lines
        }
        if (strcmp(e.file, file) == 0) {
            e.uploaded = true;
        }
        fprintf(out, "%s,%s,%lld,%d,%d\n", e.file, e.caller, (long long)e.start, e.seconds,
                e.uploaded ? 1 : 0);
    }
    fclose(in);
    fclose(out);
    unlink(INDEX_PATH);
    rename(INDEX_TMP, INDEX_PATH);
    char path[96];
    voicemail_path(file, path, sizeof(path));
    unlink(path);
    s_pending = -1;
    ESP_LOGI(TAG, "%s uploaded and removed from the card", file);
}

void voicemail_path(const char *file, char *out, int len) { snprintf(out, len, VM_DIR "/%s", file); }

// ---- card -------------------------------------------------------------------------

int voicemail_free_mb(void)
{
    if (!s_mounted) {
        return 0;
    }
    uint64_t total = 0, free_bytes = 0;
    if (esp_vfs_fat_info(MOUNT, &total, &free_bytes) != ESP_OK) {
        return 0;
    }
    return (int)(free_bytes / (1024 * 1024));
}

bool voicemail_card_ok(void) { return s_mounted && voicemail_free_mb() >= VM_MIN_FREE_MB; }

// ---- recording ----------------------------------------------------------------------

static void vm_sink(const int16_t *frame)
{
    if (s_recording) {
        xQueueSend(s_rec_q, frame, 0);  // never block the audio task
    }
}

static void vm_task(void *arg)
{
    frame_t f;
    while (true) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        s_frames = 0;

        // Greeting and beep. If the greeting is missing the beep still plays.
        play_file(GREETING, &s_stop, 20000);
        play_file(BEEP, &s_stop, 2000);

        char path[96];
        voicemail_path(s_file, path, sizeof(path));
        FILE *out = s_stop ? NULL : fopen(path, "wb");
        if (out) {
            uint8_t header[WAV_HEADER];
            wav_header(header, 0);
            fwrite(header, 1, sizeof(header), out);
            xQueueReset(s_rec_q);
            s_recording = true;
            bool limit_sent = false;
            while (!s_stop) {
                if (xQueueReceive(s_rec_q, &f, pdMS_TO_TICKS(100)) == pdTRUE) {
                    fwrite(f.s, 1, AUDIO_FRAME_BYTES, out);
                    s_frames++;
                }
                if (!limit_sent && s_frames >= VM_MAX_SECONDS * 50) {
                    limit_sent = true;
                    call_event_t ev = {.type = EV_TIME_LIMIT};
                    xQueueSend(s_events, &ev, 0);
                }
            }
            s_recording = false;
            while (xQueueReceive(s_rec_q, &f, 0) == pdTRUE) {
                fwrite(f.s, 1, AUDIO_FRAME_BYTES, out);
                s_frames++;
            }
            uint32_t bytes = (uint32_t)s_frames * AUDIO_FRAME_BYTES;
            wav_header(header, bytes);
            fseek(out, 0, SEEK_SET);
            fwrite(header, 1, sizeof(header), out);
            fclose(out);

            FILE *idx = fopen(INDEX_PATH, "a");
            if (idx) {
                fprintf(idx, "%s,%s,%lld,%d,0\n", s_file, s_number, (long long)s_start,
                        s_frames / 50);
                fclose(idx);
            }
            s_pending = -1;
            ESP_LOGI(TAG, "recorded %s (%d s)", s_file, s_frames / 50);
        } else if (!s_stop) {
            ESP_LOGE(TAG, "cannot create %s", path);
        }
        xSemaphoreGive(s_done);
    }
}

void voicemail_start(const char *number, QueueHandle_t call_events)
{
    s_events = call_events;
    s_stop = false;
    s_frames = 0;
    strncpy(s_number, number, sizeof(s_number) - 1);
    s_number[sizeof(s_number) - 1] = '\0';
    s_start = time(NULL);
    struct tm tm;
    localtime_r(&s_start, &tm);
    char stamp[20];
    strftime(stamp, sizeof(stamp), "%Y%m%d-%H%M%S", &tm);
    // Digits only in the file name; withheld numbers become "withheld".
    char digits[24] = "";
    size_t j = 0;
    for (size_t i = 0; number[i] && j < sizeof(digits) - 1; i++) {
        if (number[i] >= '0' && number[i] <= '9') {
            digits[j++] = number[i];
        }
    }
    digits[j] = '\0';
    snprintf(s_file, sizeof(s_file), "%s_%s.wav", stamp, j ? digits : "withheld");
    xSemaphoreTake(s_done, 0);  // clear any stale signal
    audio_bridge_start(vm_sink);
    xTaskNotifyGive(s_task);
    ESP_LOGI(TAG, "voicemail for %s -> %s", number[0] ? number : "withheld", s_file);
}

int voicemail_stop(void)
{
    s_stop = true;
    xSemaphoreTake(s_done, pdMS_TO_TICKS(3000));
    audio_bridge_stop();
    return s_frames / 50;
}

void voicemail_play_apology(void)
{
    if (s_mounted && audio_bridge_running()) {
        audio_bridge_clear_downlink();
        play_file(APOLOGY, NULL, 8000);
    }
}

esp_err_t voicemail_init(void)
{
    s_rec_q = xQueueCreate(REC_QUEUE_FRAMES, sizeof(frame_t));
    s_done = xSemaphoreCreateBinary();
    xTaskCreatePinnedToCore(vm_task, "voicemail", 4096, NULL, 7, &s_task, 0);

    sdmmc_host_t host = SDSPI_HOST_DEFAULT();
    spi_bus_config_t bus = {
        .mosi_io_num = PIN_SD_MOSI,
        .miso_io_num = PIN_SD_MISO,
        .sclk_io_num = PIN_SD_SCK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4096,
    };
    esp_err_t err = spi_bus_initialize(host.slot, &bus, SDSPI_DEFAULT_DMA);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPI bus init failed: %s", esp_err_to_name(err));
        return err;
    }
    sdspi_device_config_t slot = SDSPI_DEVICE_CONFIG_DEFAULT();
    slot.gpio_cs = PIN_SD_CS;
    slot.host_id = host.slot;
    esp_vfs_fat_sdmmc_mount_config_t mount = {
        .format_if_mount_failed = false,
        .max_files = 4,
        .allocation_unit_size = 16 * 1024,
    };
    sdmmc_card_t *card;
    err = esp_vfs_fat_sdspi_mount(MOUNT, &host, &slot, &mount, &card);
    if (err != ESP_OK) {
        // No card: calls are still texted to the owner, just not recorded.
        ESP_LOGE(TAG, "microSD not mounted (%s); voicemail disabled", esp_err_to_name(err));
        return err;
    }
    s_mounted = true;
    mkdir(VM_DIR, 0775);
    struct stat st;
    if (stat(GREETING, &st) != 0) {
        ESP_LOGW(TAG, "%s missing: run tools/make_greetings.py and copy /greet", GREETING);
    }
    ESP_LOGI(TAG, "card mounted, %d MB free, %d recordings waiting", voicemail_free_mb(),
             voicemail_pending_count());
    return ESP_OK;
}
