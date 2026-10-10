#!/usr/bin/env python3
"""Builds the phrase cache and the microSD card greeting files.

    cd server && python ../tools/make_greetings.py --config config.yaml --card ../sdcard

Writes, at 8 kHz 16-bit mono:
    <card>/greet/greeting.wav   voicemail greeting (English, Kannada, Hindi)
    <card>/greet/apology.wav    played if the PC link drops mid-call
    <card>/greet/beep.wav
Copy the <card>/greet folder onto the FAT32-formatted card.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from app.audio import tone, write_wav  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import build_engines  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.tts import MULTI  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Build greetings and the phrase cache")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--card", default="sdcard", help="output folder for the microSD files")
    p.add_argument("--skip-cache", action="store_true", help="only write the card files")
    args = p.parse_args()

    config = load_config(args.config)
    settings = Settings(config, Database(config.storage.database))
    engines = build_engines(config, settings.owner_name)
    phrases = engines.phrases

    if not args.skip_cache:
        print("building phrase cache ...")
        phrases.build(config.call.languages)

    greet = Path(args.card) / "greet"
    write_wav(greet / "greeting.wav", phrases.get("voicemail_greeting", MULTI))
    write_wav(greet / "apology.wav", phrases.get("apology", MULTI))
    write_wav(greet / "beep.wav", tone(1000, 400, level=0.4))
    print(f"card files written to {greet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
