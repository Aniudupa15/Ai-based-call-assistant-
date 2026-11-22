#!/usr/bin/env python3
"""Feeds a recorded call into the server as if it came from the ESP32.

Most server work is developed and tested this way, with no hardware:

    python tools/replay_call.py samples/courier.wav --number 9812345678 \
        --esp-token change-me --app-token change-me-too

The caller's audio is streamed in 20 ms frames (real time unless --fast). The bot's
audio is saved to --out. When the server hangs up, or the file ends, the call is
closed and the finished record is printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

FRAME = 320


def load_8k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            sys.exit("only 16-bit WAV files are supported")
        rate, ch = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if ch > 1:
        data = data.reshape(-1, ch)[:, 0]          # left channel = caller
    if rate != 8000:
        t_in = np.arange(len(data)) / rate
        t_out = np.arange(int(len(data) * 8000 / rate)) / 8000
        data = np.interp(t_out, t_in, data.astype(np.float32)).astype("<i2")
    return data.tobytes()


async def replay(args) -> int:
    import websockets

    pcm = load_8k(Path(args.wav))
    pcm += b"\x00" * (8000 * 2 * args.tail)          # silence after the caller stops
    frames = [pcm[i:i + FRAME].ljust(FRAME, b"\x00") for i in range(0, len(pcm), FRAME)]
    call_id = time.strftime("c-%Y%m%d-%H%M%S")
    bot_audio = bytearray()
    url = f"ws://{args.host}:{args.port}/esp"

    connect_kw = {"additional_headers": {"X-Token": args.esp_token}}
    try:
        ws = await websockets.connect(url, **connect_kw)
    except TypeError:  # websockets < 14
        ws = await websockets.connect(url, extra_headers={"X-Token": args.esp_token})

    async with ws:
        await ws.send(json.dumps({"type": "hello", "device_id": "replay", "fw_version": "replay",
                                  "proto": "1.0", "vm_pending": 0}))
        await ws.send(json.dumps({"type": "call_start", "call_id": call_id,
                                  "number": args.number, "ts": int(time.time())}))
        decision = None
        while decision is None:
            msg = json.loads(await ws.recv())
            if msg["type"] == "decision":
                decision = msg
        print(f"decision: {decision['action']} (max {decision['max_seconds']} s)")
        if decision["action"] != "answer":
            return 0
        await ws.send(json.dumps({"type": "call_answered", "call_id": call_id}))

        hung_up = asyncio.Event()

        async def receiver():
            async for msg in ws:
                if isinstance(msg, bytes):
                    bot_audio.extend(msg)
                    continue
                data = json.loads(msg)
                if data["type"] in ("clear_audio", "hangup", "send_sms", "error"):
                    print(f"<- {data}")
                if data["type"] == "hangup":
                    hung_up.set()
                    return

        recv_task = asyncio.create_task(receiver())
        reason = "caller_hangup"
        for frame in frames:
            if hung_up.is_set():
                reason = "pc_hangup"
                break
            await ws.send(frame)
            await asyncio.sleep(0 if args.fast else 0.02)
        else:
            try:
                await asyncio.wait_for(hung_up.wait(), 10)
                reason = "pc_hangup"
            except asyncio.TimeoutError:
                pass
        await ws.send(json.dumps({"type": "call_end", "call_id": call_id, "reason": reason}))
        recv_task.cancel()

    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes(bot_audio))
    print(f"call {call_id} ended ({reason}); bot audio saved to {args.out}")

    if args.app_token:
        for _ in range(120):
            req = urllib.request.Request(f"http://{args.host}:{args.port}/api/calls/{call_id}",
                                         headers={"Authorization": f"Bearer {args.app_token}"})
            with urllib.request.urlopen(req) as resp:
                record = json.load(resp)
            if record["status"] in ("ready", "failed"):
                print(json.dumps(record, indent=2, ensure_ascii=False))
                break
            await asyncio.sleep(1)
    return 0


def main() -> int:
    # Windows consoles default to a code page that cannot print Hindi or Kannada.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("wav", help="caller audio, 16-bit WAV (stereo: left channel used)")
    p.add_argument("--number", default="9812345678", help="caller ID to present")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--esp-token", default="change-me")
    p.add_argument("--app-token", default="", help="if set, print the finished record")
    p.add_argument("--out", default="bot_audio.wav")
    p.add_argument("--tail", type=int, default=8, help="seconds of silence after the file")
    p.add_argument("--fast", action="store_true", help="send frames as fast as possible")
    return asyncio.run(replay(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
