"""Barge-in with real-time pacing: the caller talks over the bot."""
import asyncio

from app.fakes import FakeLLM, FakeSTT, FakeTTS
from app.rules import CallRule
from app.session import Engines, Session
from app.tts import PhraseCache, load_phrases
from app.vad import EnergyVad

from helpers import silence, speech

LONG = "This is a very long first sentence that keeps going on and on for quite a while. And more."


class FakeLink:
    def __init__(self):
        self.json = []
        self.audio = 0

    async def send_json(self, msg):
        self.json.append(msg)

    async def send_audio(self, frame):
        self.audio += 1


def short_phrases():
    phrases = load_phrases()
    return {k: {l: "Hi." for l in v} for k, v in phrases.items()}


def test_barge_in_clears_audio(config, tmp_path):
    config.realtime = True
    config.call.reply_mode = "llm"
    tts = FakeTTS()
    engines = Engines(stt=FakeSTT(), llm=FakeLLM([LONG]), tts=tts,
                      phrases=PhraseCache(tts, tmp_path / "ph", "Ani", short_phrases()),
                      vad_factory=EnergyVad)
    rule = CallRule("answer", "unknown", None, None, None, "", 180, None)
    link = FakeLink()
    finished = []

    async def on_finished(s):
        finished.append(s)

    async def scenario():
        s = Session("c-b", "+919812345678", rule, link, engines, config, "Ani", on_finished)
        s.start()

        async def feed(frames):
            for f in frames:
                s.feed_caller(f)
                await asyncio.sleep(0.02)

        await feed(silence(400))
        await feed(speech(500) + silence(700))        # turn 1 -> long reply starts
        await feed(silence(300))
        assert s.player.speaking
        await feed(speech(500))                        # talk over the bot
        assert {"type": "clear_audio", "call_id": "c-b"} in link.json
        assert not s.player.speaking
        await feed(silence(700))                       # turn 2 -> goodbye [END]
        for _ in range(200):
            if any(m["type"] == "hangup" for m in link.json):
                break
            await feed(silence(20))
        await s.on_call_end("pc_hangup")
        return s

    s = asyncio.run(scenario())
    assert finished == [s]
    assert s.end_reason == "pc_hangup"
    bot_texts = [t.text for t in s.turns if t.speaker == "bot"]
    # The interrupted reply is kept only as far as it was heard: its first sentence.
    assert "This is a very long first sentence that keeps going on and on for quite a while." in bot_texts
    assert not any("And more." in t for t in bot_texts)
    assert bot_texts[-1].startswith("Thank you")
    assert link.audio > 100   # paced frames, silence included


def test_slow_model_reply_is_not_cut_off(config, tmp_path):
    """The 'one moment' filler must not cancel a reply that is still being written."""
    config.call.reply_mode = "llm"
    config.call.filler_after_ms = 10    # before the first chunk (30 ms) arrives
    tts = FakeTTS()
    reply = "Thank you for calling, I will make sure the message reaches Ani today. Goodbye. [END]"
    engines = Engines(stt=FakeSTT(), llm=FakeLLM([reply], chunk=4, delay=0.03), tts=tts,
                      phrases=PhraseCache(tts, tmp_path / "ph", "Ani", short_phrases()),
                      vad_factory=EnergyVad)
    rule = CallRule("answer", "unknown", None, None, None, "", 180, None)
    link = FakeLink()

    async def on_finished(s):
        pass

    async def scenario():
        s = Session("c-s", "+919812345678", rule, link, engines, config, "Ani", on_finished)
        s.start()
        await asyncio.sleep(0.05)          # the greeting is playing
        for f in speech(500) + silence(700):
            s.feed_caller(f)
        for _ in range(300):
            if any(m["type"] == "hangup" for m in link.json):
                break
            await asyncio.sleep(0.02)
        await s.on_call_end("pc_hangup")
        return s

    s = asyncio.run(scenario())
    bot = [t.text for t in s.turns if t.speaker == "bot"]
    assert "Thank you for calling, I will make sure the message reaches Ani today. Goodbye." in bot
    reply_turn = next(t for t in s.turns if t.text.startswith("Thank you for calling"))
    assert reply_turn.timing.get("slow")   # the filler played while the model was slow
    assert "Hi." in bot                    # ... and is in the transcript (short_phrases)
