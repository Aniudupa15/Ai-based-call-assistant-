"""Pretends to be the ESP32 in tests."""
import io
import json
import time
import wave

from app.audio import tone
from app.protocol import FRAME_BYTES

SPEECH = tone(300, 20, level=0.3).tobytes()
SILENCE = b"\x00" * FRAME_BYTES


def speech(ms: int) -> list[bytes]:
    return [SPEECH] * (ms // 20)


def silence(ms: int) -> list[bytes]:
    return [SILENCE] * (ms // 20)


def wav_bytes(frames: list[bytes]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(frames))
    return buf.getvalue()


class FakeEsp:
    def __init__(self, ws):
        self.ws = ws
        self.audio_frames = 0
        self.messages: list[dict] = []

    def send(self, **msg):
        self.ws.send_text(json.dumps(msg))

    def send_frames(self, frames):
        for f in frames:
            self.ws.send_bytes(f)

    def recv_until(self, kind: str) -> dict:
        """Receives until a JSON message of this type, counting audio on the way."""
        while True:
            msg = self.ws.receive()
            if msg.get("bytes") is not None:
                assert len(msg["bytes"]) == FRAME_BYTES
                self.audio_frames += 1
                continue
            if msg.get("text") is not None:
                data = json.loads(msg["text"])
                self.messages.append(data)
                if data.get("type") == kind:
                    return data
                continue
            raise AssertionError(f"socket closed while waiting for {kind}: {msg}")

    def hello(self):
        self.send(type="hello", device_id="esp-test", fw_version="1.0.0", proto="1.0",
                  vm_pending=0)
        return self.recv_until("time")

    def start_call(self, call_id: str, number: str) -> dict:
        self.send(type="call_start", call_id=call_id, number=number, ts=int(time.time()))
        return self.recv_until("decision")


def wait_for(fn, timeout=10.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError("condition not met in time")
