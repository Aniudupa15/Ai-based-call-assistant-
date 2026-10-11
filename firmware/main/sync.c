#include "sync.h"

#include <stdio.h>
#include <string.h>
#include <sys/stat.h>

#include "config.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "voicemail.h"

static const char *TAG = "sync";

#define MAX_ENTRIES 32
#define CHUNK 1024

// POST one file. Returns true when the PC replied 200 with "ok":true.
static bool upload(const vm_entry_t *e)
{
    char path[96], url[96], num[24];
    voicemail_path(e->file, path, sizeof(path));
    struct stat st;
    if (stat(path, &st) != 0) {
        ESP_LOGW(TAG, "%s listed but missing; marking done", e->file);
        return true;
    }
    FILE *f = fopen(path, "rb");
    if (!f) {
        return false;
    }
    snprintf(url, sizeof(url), "http://%s:%d/esp/voicemail", PC_HOST, PC_PORT);
    esp_http_client_config_t cfg = {.url = url, .method = HTTP_METHOD_POST, .timeout_ms = 15000};
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    esp_http_client_set_header(c, "X-Token", ESP_TOKEN);
    esp_http_client_set_header(c, "X-Caller", e->caller);
    snprintf(num, sizeof(num), "%lld", (long long)e->start);
    esp_http_client_set_header(c, "X-Start", num);
    snprintf(num, sizeof(num), "%d", e->seconds);
    esp_http_client_set_header(c, "X-Seconds", num);
    esp_http_client_set_header(c, "Content-Type", "audio/wav");

    bool ok = false;
    if (esp_http_client_open(c, (int)st.st_size) == ESP_OK) {
        static char buf[CHUNK];
        size_t n;
        bool sent = true;
        while ((n = fread(buf, 1, sizeof(buf), f)) > 0) {
            if (esp_http_client_write(c, buf, (int)n) != (int)n) {
                sent = false;
                break;
            }
        }
        if (sent && esp_http_client_fetch_headers(c) >= 0) {
            char body[256] = "";
            int len = esp_http_client_read_response(c, body, sizeof(body) - 1);
            body[len > 0 ? len : 0] = '\0';
            int status = esp_http_client_get_status_code(c);
            ok = status == 200 && strstr(body, "\"ok\":true") != NULL;
            if (!ok) {
                ESP_LOGW(TAG, "%s rejected: %d %s", e->file, status, body);
            }
        }
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    fclose(f);
    return ok;
}

void sync_run(void)
{
    static vm_entry_t entries[MAX_ENTRIES];
    int n = voicemail_list(entries, MAX_ENTRIES);
    for (int i = 0; i < n; i++) {
        if (entries[i].uploaded) {
            continue;
        }
        ESP_LOGI(TAG, "uploading %s", entries[i].file);
        if (!upload(&entries[i])) {
            return;  // PC unreachable; try again later
        }
        voicemail_mark_uploaded(entries[i].file);
    }
}
