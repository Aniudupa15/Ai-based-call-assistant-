# Personal AI Call Assistant

A self-hosted assistant that answers the calls you miss or decline, talks to the
caller in English, Hindi or Kannada, takes a message, and sends you a summary.
Calls are forwarded from your main SIM to a second SIM in a GSM module at home.
An ESP32 bridges the call audio to your PC, which runs speech recognition, a local
language model and speech synthesis. Nothing leaves your own equipment apart from
the phone network that carries the call.

**Setting it up at home? Follow [`docs/HOME_SETUP.md`](docs/HOME_SETUP.md)** — a step-by-step
checklist from first install to a real forwarded call.

The full requirements and design are in
[`docs/AI-Call-Assistant-Design-Document.pdf`](docs/AI-Call-Assistant-Design-Document.pdf).
The ESP32 ↔ PC protocol is in [`docs/protocol.md`](docs/protocol.md).

```
Caller ──> Airtel (your phone) ──forward if declined/unanswered──> Vi SIM in SIM7600 module
                                                                        │ UART + PCM
                                                                      ESP32 ──Wi-Fi WebSocket──> Home PC
                                                                        │                         Whisper / IndicConformer
                                                          voicemail to microSD                    local LLM (Ollama)
                                                          when the PC is off                      Piper / Indic Parler-TTS
                                                                                                  SQLite: rules, call log
Your phone <── app over WireGuard, or SMS from the module ─────────────────────────────────────────┘
```

## Repository layout

```
firmware/   ESP-IDF project for the ESP32 (C, FreeRTOS)
  main/       app_main.c (call task), call_fsm.c (state machine), modem.c (AT, RING, SMS),
              audio_bridge.c (I2S PCM), pc_link.c (WebSocket), voicemail.c, sync.c
  test/       unit tests: on the ESP32 (idf.py) or on the PC (make -C firmware/test/host)
server/     Python service on the PC
  app/        gateway.py (ESP32 link), session.py (one live call), vad/stt/llm/tts.py,
              langid.py, rules.py, postcall.py, recorder.py, notify.py, calls.py, api.py, db.py
  prompts/    system prompt, post-call prompt, fixed phrases in three languages
  tests/      pytest suite, including whole calls driven over the WebSocket
android/    Kotlin + Jetpack Compose app: call history, record view, rules, status, settings
docs/       design document, protocol, home setup checklist
deploy/     install_server.sh (venv, packages, tokens, systemd service), WireGuard setup
tools/      doctor.py        checks the PC is ready and suggests model sizes for your GPU
            desk_test.py     phase 1: talk to the bot with the PC's mic and speakers
            modem_bench.py   phase 2: module checks over a USB-serial adapter
            replay_call.py   feed a recorded call into the server without hardware
            make_greetings.py  phrase cache and microSD greeting files
            timing_report.py / severity_report.py  check the reply-delay and severity targets
```

## Quick start: the server without any hardware

The server runs with a `fake` model backend, so you can try the whole call flow on
any machine before the GPU models or the module are set up.

```bash
cd server
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                   # 59 tests, including full simulated calls

cp config.example.yaml config.yaml       # set models.backend: fake for now
python -m app.main --config config.yaml  # listens on :8765

# In another shell: replay a recorded call as if it came from the ESP32
pip install websockets
python ../tools/replay_call.py caller.wav --esp-token change-me --app-token change-me-too
```

## Running the real models on the PC

1. Install the GPU driver, Python 3.11+, [Ollama](https://ollama.com) and `ffmpeg`.
2. `pip install -r requirements.txt` (install the CUDA build of PyTorch for your GPU first).
3. `ollama pull <model>` and set `models.llm` in `config.yaml`. Pick sizes once you know
   the GPU's memory: speech-to-text, the language model and Indic Parler-TTS share it.
4. Download a Piper English voice (`.onnx` + `.onnx.json`) and set `models.tts_english`.
5. Set `owner`, `network.esp_token`, `network.app_token` and `models.backend: local`.
6. `python ../tools/make_greetings.py --config config.yaml --card ../sdcard` builds the
   phrase cache and the microSD greeting files.
7. `python -m app.main --config config.yaml`. Start it at boot (systemd on Linux) and set
   the BIOS to power on after a power cut.

Turn timings (end-of-speech wait, transcription, first token, first audio) are logged
for every reply and stored with each bot turn, so the 1.5 s target can be checked.

## Firmware

```bash
cd firmware
cp main/config.example.h main/config.h   # Wi-Fi, PC address, token, owner number, pins
idf.py set-target esp32
idf.py build flash monitor
```

Requires ESP-IDF 5.1 or newer (built and checked with 5.3). `esp_websocket_client`
comes from the component registry on first build. Format the microSD card as FAT32 and
copy the `greet/` folder from `make_greetings.py` onto it.

The PCM set-up commands are module-specific: confirm them against your module's AT
manual on the bench (build phase 3) and put the working ones in `MODEM_PCM_SETUP` in
`config.h`. A logic analyser on PCM_CLK and PCM_SYNC saves hours here.

Unit tests for the call state machine:

```bash
make -C firmware/test/host               # on the PC, no ESP-IDF needed
idf.py -C firmware/test flash monitor    # on the ESP32
```

## Android app

CI builds the debug APK on every push: download `call-assistant-debug-apk` from the
GitHub Actions run and sideload it (minimum Android 10). Or open `android/` in Android Studio.
On first launch, Settings asks for the PC's home address, its WireGuard address and the
app token. The Settings screen also dials the call-forwarding codes for the module's
number (`**61*`, `**67*`, `**62*`) and cancels them all (`##002#`).

The app keeps its own connection to the PC through WireGuard (a foreground service
long-polls `/api/events`); no push service is used. When the PC cannot be reached,
urgent and unseen summaries arrive by SMS from the module.

## Network

- Give the PC a fixed address with a DHCP reservation.
- Install WireGuard on the PC, create a peer for the phone and forward the WireGuard UDP
  port on the router. No other port is opened.
- If the home connection has no public IP (CGNAT), skip WireGuard: the app works at home
  and SMS covers the rest.

## Build phases

| Phase | What | Where it lives |
|---|---|---|
| 0 | Forwarding proof with a spare phone | Settings screen dial codes |
| 1 | Voice pipeline on the PC | `server/` with `backend: local`; `tools/replay_call.py` |
| 2 | Module control: power, RING, caller ID, ATA, AT+CHUP | `firmware/main/modem.c` |
| 3 | One-way audio, module → PC | `audio_bridge.c`, `pc_link.c`, `MODEM_PCM_SETUP` |
| 4 | Two-way audio | same, plus the PC's paced playback |
| 5 | PC-off fallback: voicemail, SMS, upload | `voicemail.c`, `sync.c`, `/esp/voicemail` |
| 6 | Full bot on a real call | `server/app/session.py` |
| 7 | After-call record, rules, SMS summaries | `postcall.py`, `rules.py`, `notify.py` |
| 8 | Android app | `android/` |

Wake-on-LAN (F12) and Tulu are deferred to a later version, as the design decides.

## Before daily use

- [ ] All acceptance tests in the design document pass on end-to-end forwarded calls.
- [ ] Hindi and Kannada phrases in `server/prompts/phrases.yaml` checked by a native speaker.
- [ ] One full day of real use, reviewing every record against its recording.
- [ ] Forwarding cancel code tested, so the system can be switched off in seconds.
