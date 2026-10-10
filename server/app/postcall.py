"""Builds the after-call record: summary, reason, severity, commitments and flags.

The language model is called once with the full transcript and must return JSON
in a fixed shape. One retry is allowed; if that also fails, the record is saved
with severity `normal` and the flag `summary_failed`, keeping recording and
transcript available.
"""
from __future__ import annotations

import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from .langid import summary_language
from .llm import LanguageModel
from .rules import SEVERITIES, max_severity
from .tts import PROMPTS_DIR

log = logging.getLogger(__name__)

FLAGS = {"spam_suspected", "asked_personal_info", "bot_unsure", "low_transcript_confidence",
         "summary_failed", "tulu_unreliable"}


class PostCallResult(BaseModel):
    summary: str
    reason: str
    severity: Literal["urgent", "high", "normal", "low"]
    severity_why: str = ""
    caller_name: Optional[str] = None
    action_needed: Literal["call_back", "send_info", "none"] = "none"
    deadline: Optional[str] = None
    callback_number: Optional[str] = None
    bot_commitments: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    language: Literal["en", "hi", "kn", "mixed"] = "en"

    @field_validator("severity", "action_needed", "language", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("caller_name", "deadline", "callback_number", mode="before")
    @classmethod
    def _nullish(cls, v):
        if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "unknown", "n/a"}:
            return None
        return v

    @field_validator("flags", mode="before")
    @classmethod
    def _flags(cls, v):
        if not isinstance(v, list):
            return []
        return [f for f in v if isinstance(f, str) and f in FLAGS]


def format_transcript(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        who = "Caller" if t["speaker"] == "caller" else "Bot"
        lines.append(f"{who}: {t['text']}")
    return "\n".join(lines) if lines else "(no speech)"


def parse_result(text: str) -> PostCallResult:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    return PostCallResult.model_validate(json.loads(text[start:end + 1]))


async def summarise(llm: LanguageModel, turns: list[dict], owner_name: str,
                    caller_context: str) -> PostCallResult:
    template = (PROMPTS_DIR / "postcall.txt").read_text(encoding="utf-8")
    prompt = template.format(owner_name=owner_name, caller_context=caller_context,
                             transcript=format_transcript(turns))
    messages = [{"role": "user", "content": prompt}]
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            return parse_result(await llm.complete_json(messages))
        except (ValueError, ValidationError, json.JSONDecodeError) as e:
            last_error = e
            log.warning("post-call JSON invalid (attempt %d): %s", attempt + 1, e)
        except Exception as e:  # model server down, timeout
            last_error = e
            log.error("post-call model call failed (attempt %d): %s", attempt + 1, e)
    return fallback_result(turns, str(last_error))


def fallback_result(turns: list[dict], error: str = "") -> PostCallResult:
    langs = [t.get("language") or "" for t in turns if t["speaker"] == "caller"]
    return PostCallResult(
        summary="Summary could not be generated; listen to the recording.",
        reason="Unknown",
        severity="normal",
        severity_why="Automatic summary failed" + (f": {error[:80]}" if error else ""),
        flags=["summary_failed"],
        language=summary_language(langs) if langs else "en",
    )


def apply_rule_floor(result: PostCallResult, severity_floor: str | None) -> PostCallResult:
    """Family calls, for example, are never rated below `normal`."""
    if severity_floor in SEVERITIES:
        result.severity = max_severity(result.severity, severity_floor)  # type: ignore[assignment]
    return result


async def translate_turns(llm: LanguageModel, turns: list[dict]) -> dict[int, str]:
    """English versions of caller lines that are not in English. Best effort."""
    todo = [t for t in turns
            if t["speaker"] == "caller" and t.get("language") not in (None, "en") and t["text"]]
    if not todo:
        return {}
    template = (PROMPTS_DIR / "translate.txt").read_text(encoding="utf-8")
    lines = "\n".join(f"{t['seq']}: {t['text']}" for t in todo)
    try:
        raw = await llm.complete_json([{"role": "user", "content": template.format(lines=lines)}])
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        return {int(item["seq"]): str(item["en"]) for item in data.get("lines", [])
                if "seq" in item and "en" in item}
    except Exception as e:
        log.warning("translation failed: %s", e)
        return {}
