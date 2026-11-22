// Copy to config.h and fill in. config.h is git-ignored; it holds secrets.
// Changing anything here needs a re-flash.
#pragma once

// ---- Network -----------------------------------------------------------------
#define WIFI_SSID          "your-wifi"
#define WIFI_PASS          "your-wifi-password"
#define PC_HOST            "192.168.1.50"   // fixed by a DHCP reservation in the router
#define PC_PORT            8765
#define ESP_TOKEN          "change-me"      // network.esp_token in server/config.yaml

// POSIX time zone for file names and call IDs (India: IST, UTC+5:30)
#define LOCAL_TZ           "IST-5:30"

// ---- Owner -------------------------------------------------------------------
#define OWNER_NUMBER       "+91xxxxxxxxxx"  // where voicemail SMS goes (main Airtel number)

// ---- Timers and limits ---------------------------------------------------------
#define DECISION_TIMEOUT_MS   3000   // wait for the PC's decision before voicemail
#define PC_SILENT_MS          3000   // no frames from the PC during a bot call: apology, hang up
#define VM_MAX_SECONDS        120    // voicemail recording limit
#define BOT_CALL_MAX_SECONDS  300    // local backstop; the PC normally ends calls sooner
#define RING_STOPPED_MS       8000   // no RING for this long: the caller gave up
#define MODEM_CHECK_MS        30000  // AT liveness check when idle
#define MODEM_MAX_FAILURES    3      // then power-cycle the module
#define HEALTH_INTERVAL_MS    (5 * 60 * 1000)
#define VM_MIN_FREE_MB        20     // below this, voicemail is not offered

// ---- Pins (ESP32 WROOM) ----------------------------------------------------------
// On WROVER boards GPIO16/17 serve the PSRAM: use 27 and 14 for the modem UART.
#define PIN_MODEM_RX       16        // module TXD -> ESP32
#define PIN_MODEM_TX       17        // ESP32 -> module RXD
#define PIN_MODEM_PWRKEY   4         // through a transistor if the board needs it
#define MODEM_PWRKEY_ACTIVE_HIGH 1   // 1 if driving the pin high presses PWRKEY
#define MODEM_UART_BAUD    115200

#define PIN_I2S_BCLK       26        // PCM_CLK from the module (input)
#define PIN_I2S_WS         25        // PCM_SYNC from the module (input)
#define PIN_I2S_DIN        33        // PCM_OUT: caller's voice
#define PIN_I2S_DOUT       32        // PCM_IN: bot's voice

#define PIN_SD_CS          5
#define PIN_SD_SCK         18
#define PIN_SD_MOSI        23
#define PIN_SD_MISO        19

#define PIN_POWER_SENSE    -1        // GPIO high while mains power is present; -1 if not fitted

// ---- Module PCM set-up -------------------------------------------------------------
// Module-specific: confirm against your module's AT command manual on the bench
// (build phase 3), then record the working commands here. The values below are a
// starting point for SIM7600: digital PCM, 8 kHz, 16-bit, module as master.
#define MODEM_PCM_SETUP { "AT+CPCMREG=1", "AT+CPCMFRM=0", NULL }
