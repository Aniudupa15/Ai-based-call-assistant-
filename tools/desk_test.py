#!/usr/bin/env python3
"""Build phase 1: talk to the bot at your desk with the PC's microphone and speakers.

The real pipeline runs (VAD, speech-to-text, language model, speech synthesis,
recording, post-call summary) with the PC's audio standing in for the phone line,
and the call is saved like any other record. Use headphones, or the bot hears
itself and keeps stopping.

    cd server && .venv/bin/python ../tools/desk_test.py --config config.yaml [--number +9198...]

Press Ctrl-C to hang up as the caller.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import queue
import sys
import time
from pathlib import Path

import numpy as np

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from app.audio import resample  # noqa: E402
from app.config import load_config  # noqa: E402
from app.main import create_app  # noqa: E402
from app.protocol import FRAME_BYTES  # noqa: E402
from app.session import Session  # noqa: E402

DEVICE_RATE = 16000          # most sound cards accept 16 kHz; audio is converted to 8 kHz
BLOCK = DEVICE_RATE // 50    # 20 ms


class SpeakerLink:
    """Plays what the bot would send to the ESP32."""

    def __init__(self):
        self.out: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self.hangup = asyncio.Event()

    async def send_audio(self, frame: bytes) -> None:
        pcm = resample(np.frombuffer(frame, dtype="<i2"), 8000, DEVICE_RATE)
        try:
            self.out.put_nowait(pcm)
        except queue.Full:
            pass

    async def send_json(self, msg: dict) -> None:
        print(f"   <- {msg['type']}")
        if msg["type"] == "hangup":
            self.hangup.set()


async def run(args) -> int:
    import sounddevice as sd

    config = load_config(args.config)
    config.realtime = True
    app = create_app(config)
    s = app.state.s
    number = args.number
    rule = s.rules.lookup(number)
    call_id = time.strftime("desk-%Y%m%d-%H%M%S")
    print(f"Calling as {number or 'withheld'}: rule '{rule.group}' -> {rule.action}")
    if rule.action != "answer":
        print("This number's rule does not answer; pick another --number.")
        return 1
    s.calls.create_live(call_id, number, rule, int(time.time()))

    link = SpeakerLink()
    session = Session(call_id, number, rule, link, s.engines, config,
                      s.settings.owner_name, s.calls.session_finished)
    loop = asyncio.get_running_loop()

    def mic(indata, frames, t, status):
        pcm8 = resample(indata[:, 0].copy(), DEVICE_RATE, 8000)
        data = pcm8.astype("<i2").tobytes()[:FRAME_BYTES].ljust(FRAME_BYTES, b"\x00")
        loop.call_soon_threadsafe(session.feed_caller, data)

    def speaker(outdata, frames, t, status):
        try:
            block = link.out.get_nowait()
        except queue.Empty:
            block = np.zeros(frames, dtype="<i2")
        block = block[:frames]
        outdata[:len(block), 0] = block
        outdata[len(block):, 0] = 0

    session.start()
    with sd.InputStream(samplerate=DEVICE_RATE, channels=1, dtype="int16", blocksize=BLOCK, callback=mic), \
         sd.OutputStream(samplerate=DEVICE_RATE, channels=1, dtype="int16", blocksize=BLOCK, callback=speaker):
        print("Speak now. Ctrl-C hangs up.")
        reason = "pc_hangup"
        try:
            await link.hangup.wait()
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            reason = "caller_hangup"
        await session.on_call_end(reason)

    print("Building the record ...")
    await s.calls.wait_idle()
    call = s.db.get_call(call_id)
    turns = s.db.get_turns(call_id)
    for t in turns:
        who = "Bot   " if t["speaker"] == "bot" else "Caller"
        timing = f"  {t['timing']}" if t.get("timing") else ""
        print(f"{who} [{t['language']}] {t['text']}{timing}")
    print(json.dumps({k: call[k] for k in ("severity", "severity_why", "reason", "summary",
                                           "action_needed", "caller_name", "flags", "audio_path")},
                     indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    # Windows consoles default to a code page that cannot print Hindi or Kannada.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--number", default="+919800000001", help="caller ID to pretend, for rules")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
