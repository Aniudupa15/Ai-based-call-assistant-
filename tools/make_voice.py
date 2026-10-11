#!/usr/bin/env python3
"""Records every line the bot says, once, in a free natural Indian voice.

Uses AI4Bharat's Indic Parler-TTS (free, open source): Indian English, Hindi and
Kannada speakers. It is too heavy to run during calls on a 4 GB GPU, but the bot
only says fixed lines, so they are all recorded ahead of time and played instantly.

It needs its own Python environment (parler-tts wants different library versions
from the server). One-time setup, from the server folder:

    py -3.12 -m venv .venv-voice
    .venv-voice\\Scripts\\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu126
    .venv-voice\\Scripts\\python.exe -m pip install git+https://github.com/huggingface/parler-tts.git soundfile pyyaml pydantic sounddevice soxr

1. Pick speakers (plays a sample of each):
    .venv-voice\\Scripts\\python.exe ..\\tools\\make_voice.py --config config.yaml --try-speakers
2. Record all lines (stop Ollama first if using the GPU, or add --device cpu):
    .venv-voice\\Scripts\\python.exe ..\\tools\\make_voice.py --config config.yaml --en Thoma --hi Divya --kn Anu
3. In config.yaml set call.use_caller_name: false, then start the server as usual.

Lines go to data/voice_lines/. Re-run with --force after changing speakers.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from app.audio import write_wav  # noqa: E402
from app.config import load_config  # noqa: E402
from app.message import all_english_lines  # noqa: E402
from app.protocol import SAMPLE_RATE  # noqa: E402
from app.tts import SCRIPTED_KEYS, IndicParlerTTS, line_path, load_phrases  # noqa: E402

CANDIDATES = {
    "en": ["Thoma", "Mary", "Swapna", "Dinesh", "Meera", "Jatin"],
    "hi": ["Divya", "Rohit", "Rani", "Aman"],
    "kn": ["Anu", "Suresh", "Vidya", "Chetan"],
}
SAMPLE = {
    "en": "Thank you for calling. I'll make sure {owner} gets your message. Goodbye.",
    "hi": "धन्यवाद, मैं आपका संदेश {owner} तक पहुँचा दूँगा। नमस्ते।",
    "kn": "ಧನ್ಯವಾದಗಳು, ನಿಮ್ಮ ಸಂದೇಶವನ್ನು {owner} ಅವರಿಗೆ ತಲುಪಿಸುತ್ತೇನೆ. ನಮಸ್ಕಾರ.",
}


def owner_name(config) -> str:
    """The name the server uses (the app can change it; it is kept in the database)."""
    from app.db import Database
    from app.settings import Settings

    return Settings(config, Database(config.storage.database)).owner_name


def lines_to_record(config, owner: str) -> list[tuple[str, str]]:
    """(language, text) for everything the bot can say, exactly as the server says it."""
    phrases = load_phrases()
    langs = [l for l in ("en", "kn", "hi") if l in config.call.languages]
    fixed = [l for l in config.call.phrase_languages if l in langs] or ["en"]
    scripted = [l for l in config.call.scripted_languages if l in langs]
    out: list[tuple[str, str]] = []

    def add(lang: str, text: str) -> None:
        if (lang, text) not in out:
            out.append((lang, text))

    for key, entry in phrases.items():
        for lang in fixed:
            add(lang, entry.get(lang, entry["en"]).format(owner_name=owner))
        if key in SCRIPTED_KEYS:
            for lang in scripted:
                if lang in entry:
                    add(lang, entry[lang].format(owner_name=owner))
    for text in all_english_lines(owner):
        add("en", text)
    return out


def play(audio) -> None:
    try:
        import sounddevice as sd

        sd.play(audio, SAMPLE_RATE)
        sd.wait()
    except Exception:
        pass


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--device", default="cuda", help="cuda (stop Ollama first) or cpu (slow)")
    p.add_argument("--en", default="Thoma", help="English speaker")
    p.add_argument("--hi", default="Divya", help="Hindi speaker")
    p.add_argument("--kn", default="Anu", help="Kannada speaker")
    p.add_argument("--try-speakers", action="store_true", help="play samples to choose from")
    p.add_argument("--force", action="store_true", help="record again lines that exist")
    p.add_argument("--list", action="store_true", help="only list the lines")
    args = p.parse_args()

    config = load_config(args.config)
    owner = owner_name(config)
    spoken_owner = config.owner.say_as or owner
    folder = Path(config.storage.voice_dir)
    todo = lines_to_record(config, owner)
    if args.list:
        for lang, text in todo:
            print(f"[{lang}] {text}")
        print(f"{len(todo)} lines")
        return 0

    print(f"Loading Indic Parler-TTS on {args.device} (first time: about 4 GB download) ...")
    tts = IndicParlerTTS(device=args.device, speakers={"en": args.en, "hi": args.hi, "kn": args.kn})

    if args.try_speakers:
        out = folder.parent / "voice_samples"
        for lang in [l for l in ("en", "hi", "kn") if l in config.call.languages]:
            for speaker in CANDIDATES[lang]:
                text = SAMPLE[lang].format(owner=spoken_owner if lang == "en" else owner)
                audio = tts.synthesize(text, lang, speaker)
                path = out / f"{lang}_{speaker}.wav"
                write_wav(path, audio)
                print(f"[{lang}] {speaker:8s} -> {path}")
                play(audio)
        print("\nRecord with your choices, e.g.:  --en Thoma --hi Divya --kn Anu")
        return 0

    done = 0
    t0 = time.monotonic()
    for i, (lang, text) in enumerate(todo, 1):
        path = line_path(folder, text, lang)
        if path.exists() and not args.force:
            continue
        # The voice says the owner's name the way say_as spells it; the file is
        # still found by the real text, which is what the server looks up.
        spoken = text.replace(owner, spoken_owner) if lang == "en" else text
        audio = tts.synthesize(spoken, lang)
        write_wav(path, audio)
        done += 1
        print(f"{i:3d}/{len(todo)} [{lang}] {len(audio) / SAMPLE_RATE:4.1f}s  {text}")
    print(f"\nRecorded {done} new lines in {time.monotonic() - t0:.0f} s into {folder}.")
    print("Now set call.use_caller_name: false in config.yaml and start the server.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
