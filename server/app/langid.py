"""Picks the language for each caller turn.

1. A contact's fixed language wins on the first turn.
2. Otherwise speech-to-text language detection, limited to the allowed languages.
3. If detection is unsure, keep the current language and ask once which language
   the caller prefers.
Detection runs every turn, so a caller who switches language is followed.
"""
from __future__ import annotations

from dataclasses import dataclass

LANGUAGE_NAMES = {"en": "English", "hi": "Hindi", "kn": "Kannada"}

# Spoken once when detection is unsure, in all three languages.
ASK_LANGUAGE_PHRASE = "ask_language"


@dataclass
class LanguageChoice:
    language: str
    confident: bool


def choose_language(probs: dict[str, float] | None, allowed: list[str], current: str,
                    hint: str | None, turn_index: int,
                    threshold: float = 0.7) -> LanguageChoice:
    if turn_index == 0 and hint and hint in allowed:
        return LanguageChoice(hint, True)
    if not probs:
        return LanguageChoice(current, False)
    scoped = {k: v for k, v in probs.items() if k in allowed}
    if not scoped:
        return LanguageChoice(current, False)
    top = max(probs.items(), key=lambda kv: kv[1])[0]
    if top not in allowed:
        # e.g. Kannada misheard as Sinhala on a phone line: stay put rather than guess.
        return LanguageChoice(current, False)
    total = sum(scoped.values()) or 1.0
    best, p = max(scoped.items(), key=lambda kv: kv[1])
    p /= total
    if best == current:
        return LanguageChoice(current, True)
    if p >= threshold:            # switch only when fairly sure
        return LanguageChoice(best, True)
    return LanguageChoice(current, False)


def summary_language(turn_languages: list[str]) -> str:
    """Overall language for the call record: one code, or 'mixed'."""
    langs = {l for l in turn_languages if l}
    if not langs:
        return "en"
    if len(langs) == 1:
        return langs.pop()
    return "mixed"
