"""Taking a message: what the bot says next, decided by rules, not by the model.

A small local model writes unreliable replies (it forgets what the caller said,
invents things, rambles), but it reads a short call well. So after each caller
turn the model only reports, as JSON, what it has heard: the caller's name, the
reason, whether it is urgent, whether they asked something, whether they are done.
The bot then says one of a few professional lines:

    no name, no reason  -> "May I have your name and the reason for your call, please?"
    name only           -> "Thank you, Ravi. What is the call regarding?"
    reason only         -> "Thank you. May I have your name, please?"
    both, or done       -> "Thank you, Ravi. I'll make sure Anil gets your message. Goodbye."

Each question is asked at most twice; a caller who will not say is not pestered.
English lines are synthesised on the spot (Piper is fast) so they can use the
caller's name; Hindi and Kannada use the prepared lines in phrases.yaml.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .tts import PROMPTS_DIR

MAX_ASKS = 2          # per question
TRANSCRIPT_LINES = 14


@dataclass
class Slots:
    name: str | None = None
    reason: str | None = None
    urgent: bool = False
    question: bool = False
    finished: bool = False


def _clean(v) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.strip().strip(".").strip()
    return None if v.lower() in ("", "null", "none", "unknown", "n/a", "not given") else v


def _flag(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes"))


def build_prompt(owner_name: str, lines: list[tuple[str, str]]) -> str:
    """lines: (speaker, text) with speaker 'bot' or 'caller'."""
    shown = lines[-TRANSCRIPT_LINES:]
    transcript = "\n".join(f"{'Caller' if who == 'caller' else 'Assistant'}: {text}"
                           for who, text in shown if text)
    template = (PROMPTS_DIR / "slots.txt").read_text(encoding="utf-8")
    return template.format(owner_name=owner_name, transcript=transcript or "(nothing yet)")


def parse_slots(raw: str, owner_name: str = "") -> Slots:
    data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    name = _clean(data.get("name"))
    if name and owner_name and _same_person(name, owner_name):
        name = None            # the model mistook the person being called for the caller
    return Slots(name=name, reason=_clean(data.get("reason")), urgent=_flag(data.get("urgent")),
                 question=_flag(data.get("question")), finished=_flag(data.get("finished")))


def _same_person(a: str, b: str) -> bool:
    a, b = a.lower().split(), b.lower().split()
    return bool(a and b) and (a[0] == b[0] or a[0][:5] == b[0][:5])


_NAME = re.compile(r"\b(?i:my name is|this is|i am|i'm|name's|it's)\s+([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)")
_NAME_INDIC = re.compile(r"(?:ನಾನು|ನನ್ನ ಹೆಸರು|मैं|मेरा नाम)\s+(\S+)")
_URGENT = re.compile(r"urgent|emergency|immediately|asap|hospital|accident|ತುರ್ತು|अर्जेंट|ज़रूरी|जरूरी", re.I)
_DONE = re.compile(r"\b(that'?s all|that is all|nothing else|bye|goodbye)\b", re.I)


def fallback_slots(caller_texts: list[str], owner_name: str = "") -> Slots:
    """Used when the model is down or too slow: plain pattern matching."""
    text = " ".join(caller_texts)
    m = _NAME.search(text) or _NAME_INDIC.search(text)
    name = m.group(1).strip(".,") if m else None
    if name and owner_name and _same_person(name, owner_name):
        name = None
    words = len(text.split())
    return Slots(name=name, reason="(given)" if words >= 6 else None,
                 urgent=bool(_URGENT.search(text)), question=text.rstrip().endswith("?"),
                 finished=bool(_DONE.search(caller_texts[-1] if caller_texts else "")))


@dataclass
class Step:
    key: str              # ask_name_reason | ask_name | ask_reason | goodbye
    end: bool
    name: str | None = None
    urgent: bool = False
    question: bool = False
    again: bool = False   # second time this question is asked: word it differently


@dataclass
class MessageTaker:
    """Remembers what is known across turns, so a later vague turn loses nothing."""
    name: str | None = None
    reason: str | None = None
    urgent: bool = False
    asks: dict = field(default_factory=lambda: {"name": 0, "reason": 0})

    def update(self, s: Slots) -> Step:
        self.name = s.name or self.name
        self.reason = s.reason or self.reason
        self.urgent = self.urgent or s.urgent
        need_name = not self.name and self.asks["name"] < MAX_ASKS
        need_reason = not self.reason and self.asks["reason"] < MAX_ASKS
        if s.finished or not (need_name or need_reason):
            return Step("goodbye", True, self.name, self.urgent, s.question)
        if need_name and need_reason:
            self.asks["name"] += 1
            self.asks["reason"] += 1
            return Step("ask_name_reason", False, None, self.urgent, s.question,
                        self.asks["name"] > 1)
        if need_reason:
            self.asks["reason"] += 1
            return Step("ask_reason", False, self.name, self.urgent, s.question,
                        self.asks["reason"] > 1)
        self.asks["name"] += 1
        return Step("ask_name", False, None, self.urgent, s.question, self.asks["name"] > 1)


def english_line(step: Step, owner_name: str) -> str:
    """What the bot says in English for this step."""
    first = step.name.split()[0] if step.name else None
    thanks = f"Thank you, {first}." if first else "Thank you."
    lead = ""
    if step.question:
        lead = f"I'm sorry, I don't have that information, but I'll pass your message to {owner_name}. "
    elif step.urgent and step.key != "goodbye":
        lead = "I understand it's urgent. "
    if step.key == "ask_name_reason":
        if step.again:
            return lead + f"Could you tell me who is calling, so I can pass your message to {owner_name}?"
        return lead + "May I have your name and the reason for your call, please?"
    if step.key == "ask_reason":
        if step.again:
            return lead + "Could you briefly tell me what it's about?"
        return lead + f"{thanks} What is the call regarding?"
    if step.key == "ask_name":
        if step.again:
            return lead + f"Could I have your name, so {owner_name} knows who called?"
        return lead + "Thank you. May I have your name, please?"
    if step.urgent:
        return (f"{thanks} I understand it's urgent, and I'll make sure {owner_name} "
                f"gets your message right away. Goodbye.")
    if step.question:
        return f"{thanks} I'll pass your message to {owner_name}. Goodbye."
    return f"{thanks} I'll make sure {owner_name} gets your message. Goodbye."


def all_english_lines(owner_name: str) -> list[str]:
    """Every English line the bot can say when lines do not use the caller's name,
    so they can all be recorded ahead of time (tools/make_voice.py)."""
    lines = []
    for key in ("ask_name_reason", "ask_reason", "ask_name", "goodbye"):
        for again in (False, True):
            for urgent in (False, True):
                for question in (False, True):
                    line = english_line(Step(key, key == "goodbye", None, urgent, question,
                                             again), owner_name)
                    if line not in lines:
                        lines.append(line)
    return lines
