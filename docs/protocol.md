# ESP32 ↔ PC protocol

This file is the single source for message names and fields. The firmware
(`firmware/main/pc_link.c`, `call_fsm.c`) and the server
(`server/app/gateway.py`, `server/app/protocol.py`) are both checked against it
in review. Change it first, then both sides, in the same commit.

Protocol version: **1.0**. The PC refuses a firmware whose protocol *major*
version differs and logs why.

## Connection

- The ESP32 connects to `ws://<pc-address>:8765/esp` on the home network.
- It sends the shared token in an `X-Token` header. The PC closes any connection
  without it (close code `4401`).
- Either side sends a WebSocket ping every 5 seconds. Three missed pings mean
  the link is down.
- Only one ESP32 link is held at a time. A new connection replaces the old one.

## Messages from the ESP32 (JSON text frames)

| Type            | Fields                                          | Sent when |
|-----------------|-------------------------------------------------|-----------|
| `hello`         | `device_id`, `fw_version`, `proto`, `vm_pending`| Right after connecting |
| `call_start`    | `call_id`, `number`, `ts`                       | `RING` with caller ID |
| `call_answered` | `call_id`                                       | After `ATA` succeeds |
| `call_end`      | `call_id`, `reason`                             | Call finished |
| `health`        | `registered`, `rssi`, `sd_free_mb`, `on_battery`| Every 5 minutes |
| `sms_result`    | `id`, `ok`                                      | After an SMS attempt |

- `call_end.reason` is one of `caller_hangup`, `pc_hangup`, `link_lost`,
  `time_limit`.
- `number` is the raw caller ID from `+CLIP`; an empty string means withheld.
  The PC normalises it to international format.
- `call_id` is `c-YYYYMMDD-HHMMSS` from the ESP32 clock.
- `proto` is `"1.0"`; `fw_version` is the firmware build string.

## Messages from the PC (JSON text frames)

| Type          | Fields                            | Meaning |
|---------------|-----------------------------------|---------|
| `time`        | `epoch`                           | Sets the ESP32 clock; sent after `hello` |
| `decision`    | `call_id`, `action`, `max_seconds`| `action` is `answer`, `reject` or `ignore` |
| `clear_audio` | `call_id`                         | Drop buffered bot audio now; the caller interrupted |
| `hangup`      | `call_id`                         | End the call |
| `send_sms`    | `id`, `to`, `text`                | Send a text through the module; 160 characters at most |
| `error`       | `code`, `detail`                  | Sent before the PC closes the link (e.g. version mismatch) |

If no `decision` arrives within 3 seconds of `call_start`, the ESP32 takes the
call in voicemail mode.

## Audio frames (binary)

- Each binary WebSocket message is exactly **320 bytes**: 160 samples of 8 kHz,
  16-bit signed, little-endian mono (20 ms).
- ESP32 → PC is the caller's voice. PC → ESP32 is the bot's voice.
- Frames are valid only between `call_answered` and `call_end`. Either side
  discards frames outside that window.
- The PC sends bot audio no faster than real time plus 200 ms, so the ESP32's
  buffer cannot overflow.
- During a bot call the PC sends a frame every 20 ms, silence when the bot is
  quiet. If the ESP32 receives nothing from the PC for 3 seconds during a bot
  call, it treats the link as lost, plays `/greet/apology.wav` and hangs up.

## Example exchange

```
ESP32 -> PC   {"type":"hello","device_id":"esp-a1b2c3","fw_version":"1.0.0","proto":"1.0","vm_pending":0}
PC -> ESP32   {"type":"time","epoch":1791815500}
ESP32 -> PC   {"type":"call_start","call_id":"c-20261012-143205","number":"+9198xxxxxxxx","ts":1791815525}
PC -> ESP32   {"type":"decision","call_id":"c-20261012-143205","action":"answer","max_seconds":180}
ESP32 -> PC   {"type":"call_answered","call_id":"c-20261012-143205"}
PC -> ESP32   <binary> greeting audio, 320-byte frames
ESP32 -> PC   <binary> caller audio, 320-byte frames
PC -> ESP32   {"type":"clear_audio","call_id":"c-20261012-143205"}
PC -> ESP32   {"type":"hangup","call_id":"c-20261012-143205"}
ESP32 -> PC   {"type":"call_end","call_id":"c-20261012-143205","reason":"pc_hangup"}
```

## Voicemail upload (HTTP)

- `POST http://<pc-address>:8765/esp/voicemail` with the WAV file
  (8 kHz, 16-bit mono) as the body.
- Headers: `X-Token`, `X-Caller` (raw number, may be empty), `X-Start` (epoch
  seconds), `X-Seconds`.
- The PC replies `200` with `{"ok":true,"sha256":"...","call_id":"..."}`. The
  ESP32 marks the file uploaded and deletes it.
- Any other reply leaves the file on the card for the next attempt. Uploading
  the same file twice is safe: the PC matches on caller and start time.
