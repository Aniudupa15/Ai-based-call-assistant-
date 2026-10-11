"""Contact lookup and per-call behaviour.

A number is normalised to international format, matched to a contact, then to the
contact's group, whose rule decides what the bot does. A withheld or unknown number
falls into the `unknown` group.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .db import Database
from .settings import Settings

SEVERITIES = ["low", "normal", "high", "urgent"]


def normalise_number(raw: str | None, country_code: str = "91") -> str:
    """Returns +<cc><number>, or '' for withheld/empty numbers.

    Handles the forms Indian carriers send in +CLIP: '+9198...', '9198...' (12 digits),
    '098...' (trunk prefix), '98...' (10 digits) and '0091...'.
    """
    if not raw:
        return ""
    raw = raw.strip()
    if raw.lower() in {"unknown", "withheld", "private", "restricted", "anonymous"}:
        return ""
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if plus:
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10:
        return f"+{country_code}{digits}"
    if len(digits) == 12 and digits.startswith(country_code):
        return "+" + digits
    # Short codes and service numbers: keep as they are.
    return digits


def local_display(number: str, country_code: str = "91") -> str:
    """'+919812345678' -> '9812345678' for SMS text."""
    prefix = "+" + country_code
    return number[len(prefix):] if number.startswith(prefix) else number


def max_severity(a: str, b: str | None) -> str:
    if not b or b not in SEVERITIES:
        return a
    if a not in SEVERITIES:
        return b
    return a if SEVERITIES.index(a) >= SEVERITIES.index(b) else b


@dataclass
class CallRule:
    action: str                     # answer | reject | ignore
    group: str
    contact_id: int | None
    contact_name: str | None
    language_hint: str | None
    extra_prompt: str
    max_seconds: int
    severity_floor: str | None


class RulesEngine:
    def __init__(self, db: Database, settings: Settings, default_max_seconds: int):
        self.db = db
        self.settings = settings
        self.default_max_seconds = default_max_seconds

    def lookup(self, number: str) -> CallRule:
        contact = self.db.contact_by_number(number) if number else None
        group = None
        if contact and contact.get("group_id"):
            group = self.db.get_group(contact["group_id"])
        if group is None:
            group = self.db.group_by_name("known" if contact else "unknown")
        rule = self.db.get_rule(group["rule_id"]) if group and group.get("rule_id") else None

        action = (rule or {}).get("action") or "answer"
        if action not in {"answer", "reject", "ignore"}:
            action = "answer"
        extra = ((rule or {}).get("extra_prompt") or "").replace("{owner_name}", self.settings.owner_name)
        language_hint = (contact or {}).get("language_hint") or (rule or {}).get("language_hint")
        max_seconds = (rule or {}).get("max_seconds") or self.default_max_seconds
        return CallRule(
            action=action,
            group=(group or {}).get("name", "unknown"),
            contact_id=(contact or {}).get("id"),
            contact_name=(contact or {}).get("name"),
            language_hint=language_hint or None,
            extra_prompt=extra,
            max_seconds=int(max_seconds),
            severity_floor=(rule or {}).get("severity_floor"),
        )
