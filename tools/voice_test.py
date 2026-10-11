#!/usr/bin/env python3
"""Listen to voices side by side, at phone quality, before choosing one.

    cd server
    .venv\\Scripts\\python.exe ..\\tools\\voice_test.py --config config.yaml
    .venv\\Scripts\\python.exe ..\\tools\\voice_test.py --voices sarvam:priya,sarvam:shubh --kannada

Each voice says a typical bot line with your name in it. You hear it as the caller
would (8 kHz), and a copy is saved in data/voice_test/. Put the one you like in
config.yaml as models.tts_english (or models.tts_hindi for a Hindi voice).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from app.audio import write_wav  # noqa: E402
from app.config import load_config  # noqa: E402
from app.protocol import SAMPLE_RATE  # noqa: E402

# Indian-accented English first (Kokoro's Hindi speakers reading English), then others.
DEFAULT_VOICES = ["kokoro:hf_alpha", "kokoro:hf_beta", "kokoro:hm_omega", "kokoro:hm_psi",
                  "kokoro:af_heart", "kokoro:bf_emma", "piper"]
LINE_EN = ("Hello, this is {owner}'s assistant. {owner} can't take your call right now. "
           "Thank you, Ananya. I'll make sure {owner} gets your message. Goodbye.")
LINE_HI = "नमस्ते, मैं {owner} का सहायक हूँ। धन्यवाद, मैं आपका संदेश पहुँचा दूँगा।"
LINE_KN = "ನಮಸ್ಕಾರ, ನಾನು {owner} ಅವರ ಸಹಾಯಕ. ಧನ್ಯವಾದಗಳು, ನಿಮ್ಮ ಸಂದೇಶವನ್ನು ತಲುಪಿಸುತ್ತೇನೆ."
SARVAM_VOICES = ["sarvam:priya", "sarvam:ritu", "sarvam:shubh", "sarvam:aditya"]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--voices", default=None,
                   help="comma-separated: sarvam:<speaker>, kokoro:<voice> or piper")
    p.add_argument("--say-as", default=None, help="respelling for your name, to try it out")
    p.add_argument("--no-play", action="store_true", help="only save the files")
    p.add_argument("--hindi", action="store_true", help="a Hindi line instead of English")
    p.add_argument("--kannada", action="store_true", help="a Kannada line (Sarvam voices)")
    args = p.parse_args()

    import os

    config = load_config(args.config)
    sarvam_key = config.sarvam.api_key or os.environ.get("SARVAM_API_KEY", "")
    if args.voices is None:
        args.voices = ",".join((SARVAM_VOICES if sarvam_key else []) + DEFAULT_VOICES)
    owner = config.owner.name
    spoken = args.say_as if args.say_as is not None else (config.owner.say_as or owner)
    out = Path(config.storage.audio_dir).parent / "voice_test"
    out.mkdir(parents=True, exist_ok=True)
    try:
        import sounddevice as sd
    except ImportError:
        sd = None

    from app.tts import KokoroTTS, PiperTTS, SarvamTTS

    kokoro_model = None
    for name in [v.strip() for v in args.voices.split(",") if v.strip()]:
        hindi = args.hindi
        lang = "kn" if args.kannada else "hi" if hindi else "en"
        line = {"en": LINE_EN, "hi": LINE_HI, "kn": LINE_KN}[lang]
        text = line.format(owner=spoken if lang == "en" else owner)
        try:
            if name.startswith("sarvam"):
                if not sarvam_key:
                    print(f"{name:20s} skipped: set sarvam.api_key in config.yaml")
                    continue
                speaker = name.split(":", 1)[1] if ":" in name else config.sarvam.speaker
                tts = SarvamTTS(sarvam_key, config.sarvam.model, speaker, config.sarvam.pace)
                audio = tts.synthesize(text, lang)
            elif args.kannada:
                print(f"{name:20s} skipped: only Sarvam voices speak Kannada here")
                continue
            elif name == "piper":
                tts = PiperTTS(config.models.tts_english
                               if not config.models.tts_english.startswith("kokoro:")
                               else "voices/en_US-lessac-medium.onnx")
                audio = tts.synthesize(text, "en")
            else:
                voice = name.split(":", 1)[1]
                tts = KokoroTTS({lang: voice}, config.models.tts_device)
                kokoro_model = kokoro_model or tts
                audio = tts.synthesize(text, lang)
        except Exception as e:
            print(f"{name:20s} could not load: {e}")
            continue
        path = out / f"{name.replace(':', '_')}_{lang}.wav"
        write_wav(path, audio)
        print(f"{name:20s} {len(audio) / SAMPLE_RATE:4.1f} s  -> {path}")
        if sd is not None and not args.no_play:
            sd.play(audio, SAMPLE_RATE)
            sd.wait()
    print("\nSet your choice in config.yaml, for example:\n"
          '  sarvam:\n    api_key: "..."\n    speaker: "priya"\n'
          'or, for local voices only:\n'
          '  models:\n    tts_english: "kokoro:hf_alpha"   # Indian-accented English\n'
          '    tts_hindi: "kokoro:hf_alpha"\n'
          '  owner:\n    say_as: "Uh-nee-rood-ha"     # only if your name sounds wrong')
    return 0


if __name__ == "__main__":
    sys.exit(main())
