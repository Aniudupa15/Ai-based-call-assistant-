"""Stand-in engines that need no GPU or model files.

`models.backend: fake` uses them, so the whole server, the replay tool and the
tests run on any machine. They behave plausibly, not intelligently.
"""
from __future__ import annotations

import json
import re
from collections import deque
from typing import AsyncIterator

import numpy as np

from .audio import tone
from .stt import Transcript


class FakeSTT:
    """Returns scripted transcripts in order: (text, language) or (text, language,
    confidence)."""

    def __init__(self, script: list[tuple] | None = None):
        self.script: deque[tuple] = deque(script or [])
        self.default = ("Hello, this is a test caller.", "en")

    def _peek(self) -> tuple:
        return self.script[0] if self.script else self.default

    def detect_language(self, audio16k: np.ndarray) -> dict[str, float]:
        lang = self._peek()[1]
        return {lang: 0.9, **{l: 0.05 for l in ("en", "hi", "kn") if l != lang}}

    def transcribe(self, audio16k: np.ndarray, language: str) -> Transcript:
        item = self.script.popleft() if self.script else self.default
        text, lang = item[0], item[1]
        conf = item[2] if len(item) > 2 else (0.9 if text else None)
        return Transcript(text, language or lang, conf)


class FakeLLM:
    """Asks for name and purpose, then says goodbye; summarises by keywords."""

    def __init__(self, replies: list[str] | None = None, chunk: int = 7, delay: float = 0.0):
        self.replies: deque[str] = deque(replies or [])
        self.chunk = chunk
        self.delay = delay
        self.json_calls: list[str] = []

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        if self.replies:
            reply = self.replies.popleft()
        else:
            user_turns = sum(1 for m in messages if m["role"] == "user")
            reply = ("Thank you. Could you tell me your name and why you are calling?"
                     if user_turns < 2 else
                     "Thank you, I will pass your message on. Goodbye. [END]")
        for i in range(0, len(reply), self.chunk):
            if self.delay:
                import asyncio
                await asyncio.sleep(self.delay)
            yield reply[i:i + self.chunk]

    async def complete_json(self, messages: list[dict]) -> str:
        prompt = messages[-1]["content"]
        self.json_calls.append(prompt)
        if prompt.startswith("A caller is leaving a phone message"):
            caller = prompt.split("so far", 1)[-1].split("Return only JSON", 1)[0]
            name = re.search(r"(?:this is|i am|my name is|मैं|ನಾನು) (\S+)", caller, re.I)
            reason = re.search(r"about|because|parcel|पार्सल|बारे|ಬಗ್ಗೆ|ಪಾರ್ಸೆಲ್|घर", caller, re.I)
            return json.dumps({"name": name.group(1) if name else None,
                               "reason": "stated" if reason else None,
                               "finished": bool(re.search(r"that is all|bye", caller, re.I))})
        if prompt.startswith("Translate"):
            lines = re.findall(r"^(\d+): (.*)$", prompt, re.M)
            return json.dumps({"lines": [{"seq": int(s), "en": f"(English) {t}"}
                                         for s, t in lines]})
        transcript = prompt.split("Transcript:", 1)[-1]
        caller = " ".join(l[7:] for l in transcript.splitlines() if l.startswith("Caller:"))
        low = caller.lower()
        if any(w in low for w in ("hospital", "emergency", "accident", "immediately")):
            sev, why = "urgent", "Health or emergency"
        elif any(w in low for w in ("parcel", "delivery", "courier", "bank", "today")):
            sev, why = "high", "Time-bound today"
        elif any(w in low for w in ("offer", "credit card", "survey", "loan")):
            sev, why = "low", "Sales call"
        else:
            sev, why = "normal", "Wants a callback"
        m = re.search(r"(?:this is|i am|my name is) ([A-Z][a-z]+)", caller)
        return json.dumps({
            "summary": (caller[:150] or "The caller said nothing.").strip(),
            "reason": (caller[:100] or "Unknown").strip(),
            "severity": sev,
            "severity_why": why,
            "caller_name": m.group(1) if m else None,
            "action_needed": "none" if sev == "low" else "call_back",
            "deadline": None,
            "callback_number": None,
            "bot_commitments": ["Said the owner is unavailable", "Said the message will be passed on"],
            "flags": ["spam_suspected"] if sev == "low" else [],
            "language": "en",
        })


class FakeTTS:
    """A soft tone whose length follows the text, about 15 ms per character."""

    def synthesize(self, text: str, language: str) -> np.ndarray:
        ms = max(100, min(3000, 15 * len(text)))
        return tone(440 if language == "en" else 520, ms, level=0.1)
