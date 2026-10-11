"""Whole simulated calls in real time (app/sim.py): the regressions found on real
calls, each as a scripted caller. All scenarios run side by side, about a minute."""
import asyncio

import pytest

from app.sim import run_all
from app.sim_scenarios import SCENARIOS

ONE_MOMENT = "One moment please."
STILL_THERE = "Are you still there?"
ASK_LANGUAGE = "Kannada, Hindi or English?"
SEND_TEXT = "Please send a text message"


@pytest.fixture(scope="module")
def calls():
    results = asyncio.run(run_all(SCENARIOS))
    for r in results:
        print(r.render())
    return {r.scenario.name: r for r in results}


def replies(r):
    """Bot lines after the greeting, without the automatic ones."""
    return [l for l in r.bot_lines()[1:] if l not in (ONE_MOMENT, STILL_THERE)]


def ends_with_goodbye(r):
    assert r.end_reason == "pc_hangup", r.render()
    assert "Goodbye" in r.bot_lines()[-1], r.render()


def test_happy_path(calls):
    r = calls["happy_path"]
    assert r.caller_lines() == ["Hi, this is Ramesh.", "I am calling about the parcel delivery today."]
    assert replies(r) == ["Thank you. What is the call about?",
                          "Thank you, I will pass your message on. Goodbye."]
    ends_with_goodbye(r)
    assert r.record["caller_name"] == "Ramesh"
    # Fast models: no filler, and the reply starts within about 2 s of turn end.
    assert r.count(ONE_MOMENT) == 0
    assert all(t["first_audio_ms"] < 2500 for t in r.timings if "first_audio_ms" in t)


def test_slow_model_gets_one_filler_and_the_whole_reply(calls):
    r = calls["slow_model"]
    assert replies(r) == ["Thank you. What is the call about?",
                          "Thank you, I will pass your message on. Goodbye."]
    assert r.count(ONE_MOMENT) == 2          # one per slow turn, never more
    ends_with_goodbye(r)


@pytest.mark.parametrize("name", ["slow_stt", "very_slow_indic"])
def test_slow_transcription_never_prompts(calls, name):
    r = calls[name]
    lines = r.bot_lines()
    first_reply = next(i for i, l in enumerate(lines[1:], 1) if l not in (ONE_MOMENT,))
    assert STILL_THERE not in lines[:first_reply], r.render()
    assert len(r.caller_lines()) == 1


def test_long_transcription_gets_a_filler(calls):
    r = calls["very_slow_indic"]
    assert r.bot_lines()[1] == ONE_MOMENT, r.render()


def test_mid_sentence_pause_is_one_answer(calls):
    r = calls["mid_sentence_pause"]
    assert len(r.caller_lines()) == 2
    # The bot never talks between the two halves, and answers once.
    caller = [t for t in r.turns if t["speaker"] == "caller"]
    between = [t for t in r.turns if t["speaker"] == "bot"
               and caller[0]["start_ms"] < t["start_ms"] < caller[1]["end_ms"]]
    assert between == [], r.render()
    assert len(replies(r)) == 1
    # The model saw it as one message.
    assert "calling about the water bill." in r.llm_prompts[0][-1]["content"]


@pytest.mark.parametrize("name", ["cough_while_thinking", "noise_only", "noisy_line"])
def test_noise_is_not_a_turn(calls, name):
    r = calls[name]
    assert len(r.caller_lines()) == 1, r.render()
    assert not any(SEND_TEXT in l or "say that again" in l for l in r.bot_lines())
    ends_with_goodbye(r)


@pytest.mark.parametrize("name", ["echo_line", "echo_loud"])
def test_bot_never_answers_its_own_echo(calls, name):
    r = calls[name]
    assert r.caller_lines() == ["Hi, this is Ramesh.", "It is about the meeting tomorrow."], r.render()
    assert len(r.llm_generated) == 2
    ends_with_goodbye(r)
    assert r.count(STILL_THERE) == 0


def test_barge_in_keeps_only_what_was_heard(calls):
    r = calls["barge_in"]
    first = replies(r)[0]
    assert first.startswith("Hello there.") and "pass it on" not in first, r.render()
    assert r.caller_lines()[-1] == "Sorry, this is Ramesh, about the parcel."
    ends_with_goodbye(r)


def test_cough_over_the_bot_does_not_cut_it_off(calls):
    r = calls["false_barge_in"]
    assert replies(r)[0] == "Thank you, Ramesh. Anil is not available right now. What is the call about?", r.render()
    assert r.caller_lines() == ["Hi, this is Ramesh.", "It is about the parcel."]


def test_caller_talking_over_the_greeting(calls):
    r = calls["talks_over_greeting"]
    assert r.caller_lines()[0] == "Hello? Hello?"
    ends_with_goodbye(r)


def test_silent_caller(calls):
    r = calls["silent_caller"]
    assert r.bot_lines()[1:] == [STILL_THERE, "Thank you, I will pass your message on. Goodbye."]
    assert r.record["severity"] == "low"


def test_thinking_caller_is_not_rushed(calls):
    r = calls["quiet_after_question"]
    assert r.count(STILL_THERE) == 0
    ends_with_goodbye(r)


def test_hang_up_during_transcription_keeps_the_words(calls):
    r = calls["caller_hangs_up"]
    assert r.end_reason == "caller_hangup"
    assert r.caller_lines() == ["Hi, this is Ramesh, about the parcel."]
    assert r.record["caller_name"] == "Ramesh"


def test_misheard_kannada_asks_once_and_switches(calls):
    r = calls["misheard_kannada"]
    assert r.count(ASK_LANGUAGE) == 1, r.render()
    assert not any(SEND_TEXT in l or "say that again" in l for l in r.bot_lines())
    later = [t for t in r.turns if t["speaker"] == "bot" and t.get("timing")]
    assert later and all(t["language"] == "kn" for t in later)


def test_confident_hindi_is_answered_in_hindi(calls):
    r = calls["hindi_caller"]
    assert [t["language"] for t in r.turns if t.get("timing")] == ["hi"]
    assert r.count(ASK_LANGUAGE) == 0


def test_accented_english_is_answered_not_questioned(calls):
    r = calls["accent_english"]
    assert r.count(ASK_LANGUAGE) == 0
    ends_with_goodbye(r)


def test_model_markup_is_not_spoken_and_goodbye_is(calls):
    r = calls["rambling_model"]
    assert not any(ch in l for l in r.bot_lines() for ch in "*#😊")
    assert r.bot_lines()[-1].endswith("Goodbye!"), r.render()
    assert r.end_reason == "pc_hangup"


def test_model_that_never_ends_still_hangs_up(calls):
    r = calls["model_never_ends"]
    ends_with_goodbye(r)


def test_contact_language_hint(calls):
    r = calls["family_contact"]
    assert [t["language"] for t in r.turns if t.get("timing")] == ["hi"]
    assert r.count(ASK_LANGUAGE) == 0
