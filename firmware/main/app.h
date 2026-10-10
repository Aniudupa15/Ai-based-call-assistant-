// State owned by app_main.c that other modules read.
#pragma once

#include <stdbool.h>

bool app_wifi_connected(void);

// True when no call is in progress (safe to upload voicemail).
bool app_call_idle(void);
