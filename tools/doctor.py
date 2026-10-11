#!/usr/bin/env python3
"""Checks the PC is ready to run the assistant and says what to fix.

    cd server && .venv/bin/python ../tools/doctor.py --config config.yaml
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
import urllib.request
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from app.config import load_config  # noqa: E402

results: list[tuple[str, str, str]] = []


def report(level: str, what: str, detail: str = "") -> None:
    results.append((level, what, detail))
    mark = {"ok": "PASS", "warn": "WARN", "fail": "FAIL"}[level]
    print(f"[{mark}] {what}" + (f" — {detail}" if detail else ""))


def check_python() -> None:
    v = sys.version_info
    report("ok" if v >= (3, 11) else "fail", f"Python {v.major}.{v.minor}",
           "" if v >= (3, 11) else "3.11 or newer needed")


def check_gpu(local: bool) -> float | None:
    try:
        import torch
    except ImportError:
        report("fail" if local else "warn", "PyTorch", "not installed (pip install -r requirements.txt)")
        return None
    if not torch.cuda.is_available():
        report("fail" if local else "warn", "CUDA GPU", "not visible to PyTorch; check the driver and the torch build")
        return None
    props = torch.cuda.get_device_properties(0)
    gb = props.total_memory / 1024 ** 3
    report("ok", "GPU", f"{props.name}, {gb:.1f} GB")
    return gb


def suggest_models(gb: float | None) -> None:
    if gb is None:
        return
    if gb >= 20:
        tip = "faster-whisper 'medium', a 14B instruct model (e.g. qwen2.5:14b-instruct), Indic Parler-TTS"
    elif gb >= 11:
        tip = "faster-whisper 'small', a 7-8B instruct model at 4-bit (e.g. qwen2.5:7b-instruct), Indic Parler-TTS"
    elif gb >= 7:
        tip = "faster-whisper 'small', a 3-4B instruct model (e.g. qwen2.5:3b-instruct), tts_indic: mms"
    elif gb >= 3.5:
        tip = ("faster-whisper 'small' (int8_float16), qwen2.5:3b-instruct, tts_indic: mms, "
               "stt_indic_device: cpu (this is the default config)")
    else:
        tip = "faster-whisper 'base', qwen2.5:1.5b-instruct, tts_indic: mms, everything else on the CPU"
    report("ok", "Suggested model sizes", tip)


def check_packages(local: bool) -> None:
    for mod, why in [("faster_whisper", "speech-to-text"), ("silero_vad", "voice activity"),
                     ("piper", "English voice"), ("uroman", "Hindi/Kannada voice (MMS)"),
                     ("transformers", "IndicConformer"), ("soxr", "resampling")]:
        try:
            __import__(mod)
            report("ok", f"package {mod}", why)
        except ImportError:
            report("fail" if local and mod != "soxr" else "warn", f"package {mod}", f"missing ({why})")


def check_ffmpeg() -> None:
    report("ok" if shutil.which("ffmpeg") else "warn", "ffmpeg",
           "" if shutil.which("ffmpeg") else "missing: recordings stay as WAV (sudo apt install ffmpeg)")


def check_ollama(url: str, model: str, local: bool) -> None:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=3) as r:
            tags = [m["name"] for m in json.load(r).get("models", [])]
    except Exception as e:
        report("fail" if local else "warn", "Ollama", f"not reachable at {url} ({e})")
        return
    found = model in tags or f"{model}:latest" in tags
    report("ok" if found else "fail", f"Ollama model {model}",
           "pulled" if found else f"not pulled: ollama pull {model}  (have: {', '.join(tags) or 'none'})")


def check_config(config, path: str) -> None:
    n, m = config.network, config.models
    report("fail" if n.esp_token in ("change-me", "") else "ok", "esp_token",
           "still the example value" if n.esp_token == "change-me" else "set")
    report("fail" if n.app_token in ("change-me-too", "") else "ok", "app_token",
           "still the example value" if n.app_token == "change-me-too" else "set")
    report("ok" if config.owner.number.startswith("+") and "x" not in config.owner.number else "fail",
           "owner number", config.owner.number or "empty")
    report("ok" if config.owner.name not in ("Your name", "the owner") else "warn", "owner name",
           config.owner.name)
    if m.backend == "local":
        voice = Path(m.tts_english)
        if not voice.is_absolute():
            voice = Path(path).resolve().parent / voice
        report("ok" if voice.exists() else "fail", "Piper voice",
               str(voice) if voice.exists() else f"{voice} missing (download a voice .onnx and .onnx.json)")
    else:
        report("warn", "models.backend", "fake: fine for testing, set 'local' for real calls")


def check_port(port: int) -> None:
    with socket.socket() as s:
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    report("ok", f"port {port}", "in use (server already running?)" if busy else "free")


def main() -> int:
    # Windows consoles default to a code page that cannot print Hindi or Kannada.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args()
    if not Path(args.config).exists():
        print(f"{args.config} not found: cp config.example.yaml config.yaml")
        return 1
    config = load_config(args.config)
    local = config.models.backend == "local"
    check_python()
    check_config(config, args.config)
    gb = check_gpu(local)
    suggest_models(gb)
    check_packages(local)
    check_ffmpeg()
    check_ollama(config.models.ollama_url, config.models.llm, local)
    check_port(config.network.port)
    fails = sum(1 for r in results if r[0] == "fail")
    print(f"\n{fails} problem(s) to fix." if fails else "\nAll checks passed.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
