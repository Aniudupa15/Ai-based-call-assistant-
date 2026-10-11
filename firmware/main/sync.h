// Uploads stored voicemail recordings to the PC once it is reachable.
#pragma once

// Uploads every recording not yet confirmed. Stops at the first failure; the
// rest wait for the next attempt. Uploading the same file twice is safe.
void sync_run(void);
