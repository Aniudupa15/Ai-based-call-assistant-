"""Every line the bot can say, for recording them ahead of time (tools/make_voice.py)."""
from __future__ import annotations

import logging
from pathlib import Path

from .message import all_english_lines
from .tts import SCRIPTED_KEYS, line_path, load_phrases

log = logging.getLogger(__name__)


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


def check_recorded(config, owner: str) -> tuple[int, int]:
    """How many of the bot's lines are recorded. Warns when some are missing, e.g.
    after the owner's name or a phrase changed."""
    lines = lines_to_record(config, owner)
    have = sum(1 for lang, text in lines
               if line_path(config.storage.voice_dir, text, lang).exists())
    if have < len(lines):
        log.warning("only %d of %d bot lines are recorded in %s (the owner's name or a phrase "
                    "changed since?). Record them again: tools/make_voice.py --force",
                    have, len(lines), Path(config.storage.voice_dir))
    else:
        log.info("all %d bot lines are recorded", have)
    if config.call.use_caller_name:
        log.warning("call.use_caller_name is true, so English replies with the caller's name "
                    "use the live voice; set it to false to use only the recorded voice")
    return have, len(lines)
