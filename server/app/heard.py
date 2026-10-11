"""Judging what the caller said, and tidying what the bot is about to say.

Phone lines are noisy, Whisper invents words for coughs and hiss, and the bot's
own voice can come back down the line. These checks keep such turns from being
treated as the caller speaking.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# What Whisper tends to write for noise, breathing or silence.
_ALWAYS_NOISE = re.compile(
    r"thanks? (you )?for watching|subscribe|subtitles? by|amara\.org|www\.|"
    r"^\W*(you|uh+|um+|hmm+|mm+|ah+|oh)\W*$", re.I)
# Real words that are also common hallucinations: kept only when clearly heard.
_WEAK = {"thank you", "thank you very much", "thanks", "bye", "okay"}
WEAK_MIN_CONFIDENCE = 0.45

LANGUAGE_WORDS = {
    "kn": ("kannada", "kanada", "ಕನ್ನಡ", "कन्नड़", "कन्नड"),
    "hi": ("hindi", "ಹಿಂದಿ", "हिंदी", "हिन्दी"),
    "en": ("english", "ಇಂಗ್ಲಿಷ್", "इंग्लिश", "अंग्रेज़ी", "अंग्रेजी", "angrezi"),
}


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFC", text).lower()
    text = "".join(ch if (ch.isalnum() or ch.isspace() or unicodedata.category(ch)[0] == "M")
                   else " " for ch in text)
    return " ".join(text.split())


def is_noise(text: str, confidence: float | None) -> bool:
    """True for empty or invented transcripts."""
    norm = normalise(text)
    if not norm:
        return True
    if _ALWAYS_NOISE.search(text) or _ALWAYS_NOISE.search(norm):
        return True
    if norm in _WEAK and confidence is not None and confidence < WEAK_MIN_CONFIDENCE:
        return True
    return False


def is_echo(text: str, recent_bot: list[str]) -> bool:
    """True when the 'caller' words are the bot's own recent words coming back."""
    norm = normalise(text)
    if not norm:
        return False
    words = norm.split()
    for line in recent_bot:
        bot = normalise(line)
        if not bot:
            continue
        if norm == bot or SequenceMatcher(None, norm, bot).ratio() >= 0.8:
            return True
        if len(words) >= 3 and f" {norm} " in f" {bot} ":
            return True
    return False


def language_request(text: str) -> str | None:
    """'Kannada', 'Hindi please', 'ಕನ್ನಡ' -> a language code; only for short answers."""
    norm = normalise(text)
    if not norm or len(norm.split()) > 5:
        return None
    found = [code for code, words in LANGUAGE_WORDS.items()
             if any(w in norm for w in words)]
    return found[0] if len(found) == 1 else None


_MARKUP = re.compile(r"[*_#`~>|\[\]{}]")
_LABEL = re.compile(r"^\s*(assistant|bot|ai|agent)\s*:\s*", re.I)
_SPACES = re.compile(r"\s+")


def clean_reply(text: str) -> str:
    """Strips what a speech engine should not read out: markdown, emoji, role labels."""
    text = _LABEL.sub("", text)
    text = _MARKUP.sub("", text)
    text = "".join(ch for ch in text
                   if not (unicodedata.category(ch) in ("So", "Cs", "Co") or ord(ch) > 0x1F000))
    text = _SPACES.sub(" ", text).strip()
    return text if any(ch.isalnum() for ch in text) else ""
