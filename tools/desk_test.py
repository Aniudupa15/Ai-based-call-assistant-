#!/usr/bin/env python3
"""Build phase 1: talk to the bot at your desk with the PC's microphone and speakers.

The real pipeline runs (VAD, speech-to-text, language model, speech synthesis,
recording, post-call summary) with the PC's audio standing in for the phone line,
and the call is saved like any other record. The microphone is muted while the
bot talks so laptop speakers do not make it hear itself; with headphones, add
--barge-in to be able to talk over it.

    cd server && .venv/bin/python ../tools/desk_test.py --config config.yaml [--number +9198...]

Press Ctrl-C to hang up as the caller.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import queue
import sys
import time
from pathlib import Path

import numpy as np

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from app.audio import rms_dbfs, write_wav  # noqa: E402
from app.config import load_config  # noqa: E402
from app.main import _warm, create_app  # noqa: E402
from app.protocol import FRAME_BYTES  # noqa: E402
from app.session import Session  # noqa: E402

DEVICE_RATE = 16000          # most sound cards accept 16 kHz; audio is converted to 8 kHz
BLOCK = DEVICE_RATE // 50    # 20 ms


def down_to_8k(block16: np.ndarray) -> np.ndarray:
    """16 kHz -> 8 kHz for one 20 ms block. Averaging sample pairs is a simple low-pass
    with no state, so blocks join without clicks (resampling each tiny block on its own
    adds an edge artefact every 20 ms, which ruins speech recognition)."""
    x = block16.astype(np.int32)[: len(block16) // 2 * 2]
    return (x.reshape(-1, 2).mean(axis=1)).astype("<i2")


def up_to_16k(frame8: np.ndarray, last: list) -> np.ndarray:
    """8 kHz -> 16 kHz with linear interpolation, carrying the previous sample over."""
    x = frame8.astype(np.float32)
    prev = np.concatenate([[last[0]], x[:-1]])
    out = np.empty(len(x) * 2, dtype=np.float32)
    out[0::2] = (prev + x) / 2
    out[1::2] = x
    last[0] = x[-1] if len(x) else last[0]
    return out.astype("<i2")


class SpeakerLink:
    """Plays what the bot would send to the ESP32."""

    def __init__(self):
        self.out: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self.hangup = asyncio.Event()
        self._last = [0.0]

    async def send_audio(self, frame: bytes) -> None:
        pcm = up_to_16k(np.frombuffer(frame, dtype="<i2"), self._last)
        try:
            self.out.put_nowait(pcm)
        except queue.Full:
            pass

    async def send_json(self, msg: dict) -> None:
        if msg["type"] == "clear_audio":
            # Like the ESP32: drop what is buffered so the bot stops at once.
            while not self.out.empty():
                try:
                    self.out.get_nowait()
                except queue.Empty:
                    break
            return
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
    print("Loading and warming the models (the first run of each is slow) ...")
    t0 = time.monotonic()
    await _warm(s)
    print(f"  ready in {time.monotonic() - t0:.1f} s"
          + (f"  {s.engines.stt.timings}" if getattr(s.engines.stt, "timings", None) else ""))
    share = getattr(s.engines.llm, "gpu_share", None)
    if share is not None:
        print(f"  language model on the GPU: {share:.0%}"
              + ("" if share >= 0.95 else "  <- part of it runs on the CPU: replies will be slow"))
    s.calls.create_live(call_id, number, rule, int(time.time()))

    link = SpeakerLink()
    session = Session(call_id, number, rule, link, s.engines, config,
                      s.settings.owner_name, s.calls.session_finished)
    loop = asyncio.get_running_loop()
    heard: list[np.ndarray] = []      # exactly what the bot received, saved for checking
    levels: list[float] = []

    gain = args.gain

    def mic(indata, frames, t, status):
        block = indata[:, 0]
        if gain != 1.0:
            block = np.clip(block.astype(np.float32) * gain, -32768, 32767).astype("<i2")
        pcm8 = down_to_8k(block)
        if not args.barge_in and (session.player.speaking or session._echo_tail > 0):
            # Laptop speakers leak into the mic; ignore it while the bot talks (and for
            # the sound card's buffer and the room's echo just after).
            pcm8 = np.zeros_like(pcm8)
        heard.append(pcm8)
        levels.append(rms_dbfs(pcm8))
        data = pcm8.tobytes()[:FRAME_BYTES].ljust(FRAME_BYTES, b"\x00")
        loop.call_soon_threadsafe(session.feed_caller, data)

    def speaker(outdata, frames, t, status):
        try:
            block = link.out.get_nowait()
        except queue.Empty:
            block = np.zeros(frames, dtype="<i2")
        block = block[:frames]
        outdata[:len(block), 0] = block
        outdata[len(block):, 0] = 0

    try:
        mic_name = sd.query_devices(args.input, kind='input')['name']
        spk_name = sd.query_devices(args.output, kind='output')['name']
        print(f"Microphone: {mic_name}" + (f"  (gain x{args.gain:g})" if args.gain != 1 else ""))
        print(f"Speaker:    {spk_name}")
    except Exception:
        pass
    session.start()
    with sd.InputStream(samplerate=DEVICE_RATE, channels=1, dtype="int16", blocksize=BLOCK,
                        callback=mic, device=args.input), \
         sd.OutputStream(samplerate=DEVICE_RATE, channels=1, dtype="int16", blocksize=BLOCK,
                         callback=speaker, device=args.output):
        print("Speak now. Ctrl-C hangs up.")
        reason = "pc_hangup"
        try:
            await link.hangup.wait()
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            reason = "caller_hangup"
        await session.on_call_end(reason)

    mic_file = Path(config.storage.audio_dir) / f"{call_id}-mic.wav"
    if heard:
        write_wav(mic_file, np.concatenate(heard))
        loud = sorted(levels)[int(len(levels) * 0.95)] if levels else -120
        print(f"Mic: loudest moments about {loud:.0f} dBFS (speech should reach -30 or higher). "
              f"What the bot heard: {mic_file}")
        if loud < -40:
            print("  Your microphone is very quiet: raise its level in Windows Sound settings, "
                  "or pick another input device.")
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
    p.add_argument("--list-devices", action="store_true", help="show audio devices and exit")
    p.add_argument("--input", type=int, default=None, help="microphone device number")
    p.add_argument("--output", type=int, default=None, help="speaker device number")
    p.add_argument("--gain", type=float, default=1.0, help="boost a quiet microphone, e.g. 4")
    p.add_argument("--barge-in", action="store_true",
                   help="let you talk over the bot (use headphones, or it hears itself)")
    args = p.parse_args()
    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "urllib3", "huggingface_hub", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
