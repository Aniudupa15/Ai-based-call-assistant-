"""Two-channel call recording: left is the caller, right is the bot, one timeline.

The caller's frames arrive every 20 ms for the whole call, so they set the clock.
Bot audio is placed at the caller position when it is sent, and never overlaps
earlier bot audio.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from pathlib import Path

import numpy as np

from .audio import write_wav
from .protocol import FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger(__name__)


class Recorder:
    def __init__(self) -> None:
        self.caller = bytearray()
        self.bot = bytearray()
        self._bot_pos = 0  # in samples

    @property
    def position_ms(self) -> int:
        return len(self.caller) // 2 * 1000 // SAMPLE_RATE

    def add_caller(self, frame: bytes) -> None:
        self.caller += frame

    def add_bot(self, frame: bytes) -> int:
        """Adds a bot frame and returns its start position in ms."""
        caller_pos = len(self.caller) // 2
        start = max(self._bot_pos, caller_pos)
        end = start + len(frame) // 2
        if len(self.bot) < end * 2:
            self.bot += b"\x00" * (end * 2 - len(self.bot))
        self.bot[start * 2:end * 2] = frame
        self._bot_pos = end
        return start * 1000 // SAMPLE_RATE

    @property
    def bot_position_ms(self) -> int:
        return max(self._bot_pos, len(self.caller) // 2) * 1000 // SAMPLE_RATE

    def stereo(self) -> np.ndarray:
        left = np.frombuffer(bytes(self.caller), dtype="<i2")
        right = np.frombuffer(bytes(self.bot), dtype="<i2")
        n = max(len(left), len(right), FRAME_SAMPLES)
        out = np.zeros((n, 2), dtype="<i2")
        out[:len(left), 0] = left
        out[:len(right), 1] = right
        return out

    def save_wav(self, path: Path) -> Path:
        write_wav(path, self.stereo().reshape(-1), SAMPLE_RATE, channels=2)
        return path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def compress_to_opus(wav_path: Path) -> Path:
    """Compresses to Opus with ffmpeg and deletes the WAV once the Opus file checks out.

    If ffmpeg is missing or fails, the WAV is kept and returned.
    """
    if shutil.which("ffmpeg") is None:
        log.warning("ffmpeg not found; keeping %s as WAV", wav_path)
        return wav_path
    opus = wav_path.with_suffix(".opus")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
           "-c:a", "libopus", "-b:a", "24k", "-application", "voip", str(opus)]
    try:
        subprocess.run(cmd, check=True, timeout=120)
        probe = subprocess.run(["ffmpeg", "-v", "error", "-i", str(opus), "-f", "null", "-"],
                               capture_output=True, timeout=120)
        if probe.returncode != 0 or opus.stat().st_size == 0:
            raise RuntimeError(probe.stderr.decode(errors="replace"))
    except Exception as e:  # keep the WAV if anything went wrong
        log.error("opus compression failed for %s: %s", wav_path, e)
        opus.unlink(missing_ok=True)
        return wav_path
    wav_path.unlink(missing_ok=True)
    return opus
