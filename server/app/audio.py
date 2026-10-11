"""PCM helpers: conversion, resampling, framing and WAV files."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from .protocol import FRAME_BYTES, SAMPLE_RATE

try:  # soxr gives the best quality; numpy interpolation is the fallback.
    import soxr  # type: ignore
except ImportError:  # pragma: no cover - depends on the install
    soxr = None


def bytes_to_int16(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2")


def int16_to_float(x: np.ndarray) -> np.ndarray:
    return x.astype(np.float32) / 32768.0


def float_to_int16(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")


def resample(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resamples int16 or float32 mono audio, returning the same dtype."""
    if src_rate == dst_rate or len(x) == 0:
        return x
    is_int = x.dtype == np.int16 or x.dtype == np.dtype("<i2")
    f = int16_to_float(x) if is_int else x.astype(np.float32)
    if soxr is not None:
        y = soxr.resample(f, src_rate, dst_rate)
    else:
        n_out = int(round(len(f) * dst_rate / src_rate))
        t_in = np.arange(len(f)) / src_rate
        t_out = np.arange(n_out) / dst_rate
        y = np.interp(t_out, t_in, f).astype(np.float32)
    return float_to_int16(y) if is_int else y.astype(np.float32)


def frames(pcm: bytes, pad: bool = True) -> list[bytes]:
    """Splits 8 kHz int16 PCM into 320-byte frames, padding the last with silence."""
    out = [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
    if out and len(out[-1]) < FRAME_BYTES:
        if pad:
            out[-1] = out[-1] + b"\x00" * (FRAME_BYTES - len(out[-1]))
        else:
            out.pop()
    return out


SILENCE_FRAME = b"\x00" * FRAME_BYTES


def rms_dbfs(frame: np.ndarray) -> float:
    if len(frame) == 0:
        return -120.0
    f = frame.astype(np.float64) / 32768.0
    rms = float(np.sqrt(np.mean(f * f)))
    return 20.0 * np.log10(max(rms, 1e-6))


def tone(freq: float, ms: int, rate: int = SAMPLE_RATE, level: float = 0.3) -> np.ndarray:
    t = np.arange(int(rate * ms / 1000)) / rate
    return float_to_int16(level * np.sin(2 * np.pi * freq * t))


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Reads a 16-bit WAV as mono int16 and returns (samples, rate)."""
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: only 16-bit WAV is supported")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1).astype("<i2")
    return data, rate


def read_wav_8k(path: str | Path) -> np.ndarray:
    data, rate = read_wav(path)
    return resample(data, rate, SAMPLE_RATE)


def write_wav(path: str | Path, samples: np.ndarray, rate: int = SAMPLE_RATE,
              channels: int = 1) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(samples, dtype="<i2").tobytes())
