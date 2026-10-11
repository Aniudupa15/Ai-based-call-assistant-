"""Request and response bodies for the app API."""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

Severity = Literal["urgent", "high", "normal", "low"]
Action = Literal["answer", "reject", "ignore"]
Language = Literal["en", "hi", "kn"]


class CallPatch(BaseModel):
    seen: Optional[bool] = None
    severity_corrected: Optional[Severity] = None


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    number: str = Field(min_length=3, max_length=20)
    group_id: Optional[int] = None
    group: Optional[str] = None          # group name, as an alternative to group_id
    language_hint: Optional[Language] = None
    notes: str = ""


class GroupPatch(BaseModel):
    rule_id: int


class RuleIn(BaseModel):
    name: Optional[str] = None
    action: Optional[Action] = None
    language_hint: Optional[Language] = None
    extra_prompt: Optional[str] = Field(default=None, max_length=1000)
    max_seconds: Optional[int] = Field(default=None, ge=30, le=600)
    severity_floor: Optional[Union[Severity, Literal[""]]] = None  # "" clears it


class SettingsIn(BaseModel):
    owner_name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    owner_number: Optional[str] = None
    retention_days: Optional[int] = Field(default=None, ge=1, le=3650)


class TestSmsIn(BaseModel):
    to: Optional[str] = None
    text: str = "Test message from your call assistant."
