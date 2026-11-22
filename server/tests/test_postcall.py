import asyncio
import json

from app.postcall import PostCallResult, apply_rule_floor, parse_result, summarise

GOOD = {
    "summary": "Courier at the gate.", "reason": "Parcel needs a signature",
    "severity": "High", "severity_why": "today", "caller_name": "null",
    "action_needed": "call_back", "deadline": None, "callback_number": "",
    "bot_commitments": ["Said you will call back"],
    "flags": ["spam_suspected", "made_up_flag"], "language": "kn",
}
TURNS = [{"seq": 1, "speaker": "bot", "text": "Hello", "language": "en"},
         {"seq": 2, "speaker": "caller", "text": "Parcel for you", "language": "kn"}]


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def complete_json(self, messages):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_parse_normalises_fields():
    r = parse_result("```json\n" + json.dumps(GOOD) + "\n```")
    assert r.severity == "high"
    assert r.caller_name is None and r.callback_number is None
    assert r.flags == ["spam_suspected"]


def test_retry_then_success():
    llm = ScriptedLLM(["not json", json.dumps(GOOD)])
    r = asyncio.run(summarise(llm, TURNS, "Ani", "unknown"))
    assert llm.calls == 2 and r.severity == "high"


def test_two_failures_fall_back():
    llm = ScriptedLLM(['{"summary": 1}', RuntimeError("ollama down")])
    r = asyncio.run(summarise(llm, TURNS, "Ani", "unknown"))
    assert r.severity == "normal" and r.flags == ["summary_failed"]
    assert r.language == "kn"


def test_severity_floor():
    r = PostCallResult(summary="s", reason="r", severity="low")
    assert apply_rule_floor(r, "normal").severity == "normal"
    r = PostCallResult(summary="s", reason="r", severity="urgent")
    assert apply_rule_floor(r, "normal").severity == "urgent"
