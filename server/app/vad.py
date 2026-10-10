"""Voice activity detection and turn taking.

The VAD model scores each 20 ms caller frame. TurnDetector turns those scores into
speech start, barge-in and end-of-turn events. It counts frames rather than wall
time, so a recorded call replays the same way however fast it is fed in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .audio import int16_to_float, rms_dbfs
from .protocol import FRAME_MS


class VadModel(Protocol):
    def is_speech(self, frame: np.ndarray, bot_speaking: bool) -> bool:
        """frame: 160 int16 samples at 8 kHz."""


class EnergyVad:
    """Loudness threshold. Good enough for tests and quiet lines; Silero is better."""

    def __init__(self, threshold_dbfs: float = -35.0, echo_margin_db: float = 8.0):
        self.threshold = threshold_dbfs
        self.echo_margin = echo_margin_db

    def is_speech(self, frame: np.ndarray, bot_speaking: bool) -> bool:
        limit = self.threshold + (self.echo_margin if bot_speaking else 0.0)
        return rms_dbfs(frame) > limit


class SileroVad:
    """Silero VAD at 8 kHz. It takes 256-sample windows, so frames are buffered."""

    WINDOW = 256

    def __init__(self, threshold: float = 0.5, echo_threshold: float = 0.8):
        import torch  # noqa: F401  (heavy import kept local)
        from silero_vad import load_silero_vad

        self._torch = __import__("torch")
        self.model = load_silero_vad()
        self.threshold = threshold
        # The phone network can echo the bot's own voice back, so the bar is
        # higher while the bot talks. Tune on real calls.
        self.echo_threshold = echo_threshold
        self._buf = np.zeros(0, dtype=np.float32)
        self._prob = 0.0

    def reset(self) -> None:
        self.model.reset_states()
        self._buf = np.zeros(0, dtype=np.float32)
        self._prob = 0.0

    def is_speech(self, frame: np.ndarray, bot_speaking: bool) -> bool:
        self._buf = np.concatenate([self._buf, int16_to_float(frame)])
        while len(self._buf) >= self.WINDOW:
            chunk, self._buf = self._buf[:self.WINDOW], self._buf[self.WINDOW:]
            with self._torch.no_grad():
                self._prob = float(self.model(self._torch.from_numpy(chunk), 8000).item())
        return self._prob >= (self.echo_threshold if bot_speaking else self.threshold)


@dataclass
class TurnEvent:
    kind: str           # speech_start | barge_in | end_of_turn
    frame: int          # frame index where the event happened
    start_frame: int    # first frame of the speech segment


class TurnDetector:
    def __init__(self, speech_start_ms: int = 200, end_of_turn_ms: int = 600,
                 barge_in_ms: int = 300):
        self.start_frames = max(1, speech_start_ms // FRAME_MS)
        self.end_frames = max(1, end_of_turn_ms // FRAME_MS)
        self.barge_frames = max(1, barge_in_ms // FRAME_MS)
        self.frame = 0
        self.in_speech = False
        self._run = 0           # consecutive speech frames before start
        self._silence = 0       # consecutive silence frames during speech
        self._seg_start = 0
        self.last_activity = 0  # frame of the last speech, for silence timeouts

    def reset_silence(self) -> None:
        """Called when the bot finishes speaking: silence counting restarts."""
        self.last_activity = self.frame

    def silent_frames(self) -> int:
        return 0 if self.in_speech else self.frame - self.last_activity

    def feed(self, speech: bool, bot_speaking: bool) -> TurnEvent | None:
        idx = self.frame
        self.frame += 1
        if speech:
            self.last_activity = self.frame
        if not self.in_speech:
            if speech:
                if self._run == 0:
                    self._seg_start = idx
                self._run += 1
                need = self.barge_frames if bot_speaking else self.start_frames
                if self._run >= need:
                    self.in_speech = True
                    self._silence = 0
                    kind = "barge_in" if bot_speaking else "speech_start"
                    return TurnEvent(kind, idx, self._seg_start)
            else:
                self._run = 0
            return None
        # In speech: wait for enough trailing silence.
        if speech:
            self._silence = 0
            return None
        self._silence += 1
        if self._silence >= self.end_frames:
            self.in_speech = False
            self._run = 0
            self.last_activity = self.frame
            return TurnEvent("end_of_turn", idx, self._seg_start)
        return None


def make_vad(kind: str) -> VadModel:
    if kind == "silero":
        return SileroVad()
    return EnergyVad()
