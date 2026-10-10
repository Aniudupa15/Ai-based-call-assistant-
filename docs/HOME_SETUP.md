# Home setup checklist

Work through this in order. Each step says what to run and what you should see.
Steps 1–4 need only the PC and your phone; the module comes in at step 6.
Tick the boxes as you go; if a step fails, stop there and note the output.

## 1. Get the code onto the PC (10 min)

```bash
git clone https://github.com/Aniudupa15/Ai-based-call-assistant-.git
cd Ai-based-call-assistant-
git checkout ccr-b1522b37-h0f07m
```

- [ ] Code is on the PC.

## 2. Try the server with fake models first (10 min)

This proves the code runs on your PC before any big downloads.

```bash
./deploy/install_server.sh --dev --no-service
```

You should see the test suite pass (`59 passed`) and new tokens printed. **Save
both tokens**: `esp_token` goes into the firmware, `app_token` into the phone app.

- [ ] Tests pass on the PC.
- [ ] Tokens saved.

## 3. Phase 0: forwarding proof (15 min, no hardware)

1. Put the **Vi SIM** in a spare phone and check it can receive a call.
2. From your **Airtel** phone, dial each code (replace `NUMBER` with the Vi number):
   - `**61*NUMBER#` (no answer)
   - `**67*NUMBER#` (busy or declined)
   - `**62*NUMBER#` (unreachable)
3. From a third phone, call your Airtel number and **decline** it.

- [ ] The spare phone rings.
- [ ] It shows the **original caller's** number, not your Airtel number. Note which.
- [ ] `##002#` cancels forwarding (test it, then set the codes again).

## 4. Phase 1: the real voice pipeline (1–2 hours, mostly downloads)

```bash
# Install the GPU driver first if you haven't (nvidia-smi should work).
sudo apt install ffmpeg
curl -fsSL https://ollama.com/install.sh | sh
./deploy/install_server.sh              # full install, also sets up the boot service
cd server
../server/.venv/bin/python ../tools/doctor.py --config config.yaml
```

The doctor prints your GPU and **suggests model sizes**. Then:

1. `ollama pull <suggested model>` and put its name in `config.yaml` → `models.llm`.
2. Download a Piper voice into `server/voices/`: both `en_US-lessac-medium.onnx` and
   `en_US-lessac-medium.onnx.json` from huggingface.co/rhasspy/piper-voices.
3. In `config.yaml`: set `owner.name`, `owner.number` (`+91...`), `models.backend: local`,
   `models.stt_english` as suggested.
4. Run the doctor again until it says **All checks passed**.
5. Build the phrases and SD card files (the first run downloads models, so it's slow):
   ```bash
   .venv/bin/python ../tools/make_greetings.py --config config.yaml --card ../sdcard
   ```
6. **Talk to the bot** with headphones on:
   ```bash
   pip install sounddevice   # if the installer didn't
   .venv/bin/python ../tools/desk_test.py --config config.yaml
   ```
   Try English, Hindi and Kannada, and mixed Kannada-English. Interrupt it once.
7. Check the speed:
   ```bash
   .venv/bin/python ../tools/timing_report.py --db data/calls.db
   ```

- [ ] Doctor passes.
- [ ] The bot greets, understands you and replies in **English**.
- [ ] Same in **Hindi**.
- [ ] Same in **Kannada**.
- [ ] Interrupting stops the bot.
- [ ] The printed record has a sensible reason and severity.
- [ ] Timing report: most replies under 1.5 s (if not, note the medians it prints).

## 5. Phone app (30 min)

1. Open **GitHub → Actions → CI** for this branch. The `android` job builds the app;
   download **call-assistant-debug-apk** from the run, unzip it, and install
   `app-debug.apk` on the phone (allow "install unknown apps").
   If the job failed, send me its log; or open `android/` in Android Studio.
2. Start the server: `sudo systemctl start call-assistant`
   (logs: `journalctl -u call-assistant -f`).
3. In the app → **Settings**: home address `http://<PC-IP>:8765`, the `app_token`, Save.
4. **Status** screen should say *PC: reachable*.
5. Add yourself or family under **Contacts**, check **Rules**.

- [ ] App installs and connects at home.
- [ ] The desk-test calls show up in **Calls**, with audio playback.

## 6. Phase 2: module on the bench (1 hour)

Power the SIM7600 from its own 5 V 3 A supply (never from the ESP32's 3.3 V pin), with
the 1000 µF capacitor close to it. Put the Vi SIM in and fit the antenna. Connect a
USB-to-serial adapter to the module's UART (check whether it needs 1.8 V or 3.3 V levels).

```bash
pip install pyserial
python tools/modem_bench.py --port /dev/ttyUSB0 check
python tools/modem_bench.py --port /dev/ttyUSB0 watch --answer --hangup-after 10
python tools/modem_bench.py --port /dev/ttyUSB0 sms --to +91<your number>
```

- [ ] `check`: SIM ready, registered (home/roaming), signal fair or better.
- [ ] `watch`: a call from a test phone shows the right caller ID, is answered, and
      hangs up after 10 s.
- [ ] A **forwarded** call (decline on Airtel) also arrives with the caller's number.
- [ ] The SMS arrives.

## 7. Firmware onto the ESP32 (1 hour)

```bash
# Install ESP-IDF 5.3 once: https://docs.espressif.com/projects/esp-idf/en/v5.3.2/esp32/get-started/
cd firmware
cp main/config.example.h main/config.h
# Edit config.h: Wi-Fi, PC_HOST (the PC's fixed IP), ESP_TOKEN, OWNER_NUMBER, pins.
idf.py set-target esp32
idf.py build flash monitor
```

Wire the UART and power first (per the design document's Connections table), and copy
the `sdcard/greet` folder onto a FAT32 microSD card.

- [ ] Serial log shows `module ready`, `card mounted`, `Wi-Fi connected`, and
      `connected to <PC>`.
- [ ] App **Status** shows *ESP32 link: connected* and the module's signal.
- [ ] Stop the server and call: the voicemail greeting plays (once audio works), you
      get the "Voicemail from ..." SMS, and after restarting the server the recording
      appears in the app.

## 8. Phases 3–4: call audio (the risky part)

Wire PCM_CLK, PCM_SYNC, PCM_OUT and PCM_IN. Check the module's AT manual for the PCM
commands and put them in `MODEM_PCM_SETUP` in `config.h`. A logic analyser on
PCM_CLK and PCM_SYNC tells you the clock rate and frame format.

- [ ] Phase 3: a call's audio reaches the PC (the recording in the app has the
      caller's voice, clear).
- [ ] Phase 4: the caller hears the bot.
- [ ] Phase 6: a real forwarded call gets a full conversation and a saved record.

If PCM won't work, the fallback is the module's analog audio through an ES8388 codec
board; tell me and I'll change `audio_bridge.c`.

## 9. Away from home (optional)

First compare your router's WAN IP with a "what is my IP" site. Only if they match:

```bash
sudo ./deploy/wireguard/setup_wireguard.sh <your-public-ip>
```

Forward UDP 51820 on the router to the PC, scan the QR code with the WireGuard app,
and enter `http://10.8.0.1:8765` as the app's WireGuard address.

- [ ] With Wi-Fi off on the phone, the app still reaches the PC.

## 10. PC settings

- [ ] BIOS: power on after a power cut (often called "Restore on AC power loss").
- [ ] The PC doesn't sleep; the display can turn off.
- [ ] Wired Ethernet, with a DHCP reservation in the router.

## What to send me

For any failed box, the command and its output (or the `journalctl -u call-assistant`
log, or the ESP32 serial log). After a day of use, also send the output of
`tools/timing_report.py` and `tools/severity_report.py` (after correcting severities
in the app), so the prompts and timings can be tuned.
