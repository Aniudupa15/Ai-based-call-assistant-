from app.fakes import FakeTTS
from app.tts import MULTI, PhraseCache


def test_english_only_phrases(tmp_path):
    p = PhraseCache(FakeTTS(), tmp_path, "Ani", languages=["en"])
    assert p.text("goodbye", "kn") == p.text("goodbye", "en")
    assert p.text("voicemail_greeting", MULTI) == p.text("voicemail_greeting", "en")


def test_all_languages(tmp_path):
    p = PhraseCache(FakeTTS(), tmp_path, "Ani", languages=["en", "hi", "kn"])
    assert p.text("goodbye", "kn") != p.text("goodbye", "en")
    multi = p.text("voicemail_greeting", MULTI)
    assert multi.startswith(p.text("voicemail_greeting", "en"))
    assert p.text("voicemail_greeting", "kn") in multi


def test_routing_tts_say_as_and_per_language():
    import numpy as np

    from app.tts import RoutingTTS

    class Rec:
        def __init__(self):
            self.calls = []

        def synthesize(self, text, language):
            self.calls.append((text, language))
            return np.zeros(10, dtype="<i2")

    en, indic, hi = Rec(), Rec(), Rec()
    tts = RoutingTTS(en, indic, {"hi": hi}, {"Anirudha": "Uh-nee-rood-ha"})
    tts.synthesize("Anirudha is busy.", "en")
    tts.synthesize("नमस्ते", "hi")
    tts.synthesize("ನಮಸ್ಕಾರ", "kn")
    assert en.calls == [("Uh-nee-rood-ha is busy.", "en")]
    assert hi.calls == [("नमस्ते", "hi")] and indic.calls == [("ನಮಸ್ಕಾರ", "kn")]


def test_voice_change_uses_a_new_cache_folder(tmp_path):
    from app.config import Config
    from app.main import voice_tag

    a, b = Config(), Config()
    b.models.tts_english = "kokoro:af_heart"
    assert voice_tag(a) != voice_tag(b)


def test_kokoro_hindi_speaker_reads_english_with_english_rules(monkeypatch):
    import sys
    import types

    made = []

    class FakePipe:
        def __init__(self, lang_code, model=True, device=None, repo_id=None):
            made.append(lang_code)
            self.model = object()
            self.g2p = types.SimpleNamespace(fallback=object())

    monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=FakePipe))
    from app.tts import KokoroTTS

    KokoroTTS({"en": "hf_alpha", "hi": "hf_beta"})
    KokoroTTS({"en": "bf_emma"})
    assert made == ["a", "h", "b"]


def test_sarvam_request_and_fallback():
    import base64
    import io
    import json
    import wave

    import httpx
    import numpy as np

    from app.tts import FallbackTTS, SarvamTTS

    def wav_b64(n):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(np.full(n, 100, dtype="<i2").tobytes())
        return base64.b64encode(buf.getvalue()).decode()

    seen = []

    def handler(request):
        seen.append((request.headers["api-subscription-key"], json.loads(request.content)))
        return httpx.Response(200, json={"request_id": "x", "audios": [wav_b64(800)]})

    tts = SarvamTTS("k123", speaker="priya", client=httpx.Client(transport=httpx.MockTransport(handler)))
    audio = tts.synthesize("ನಮಸ್ಕಾರ", "kn")
    assert len(audio) == 800
    key, body = seen[0]
    assert key == "k123" and body["target_language_code"] == "kn-IN"
    assert body["speaker"] == "priya" and body["speech_sample_rate"] == 8000
    tts.synthesize("ನಮಸ್ಕಾರ", "kn")
    assert len(seen) == 1                 # repeated line: not sent again

    down = SarvamTTS("k", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(500, text="boom"))))

    class Local:
        def synthesize(self, text, language):
            return np.zeros(5, dtype="<i2")

    assert len(FallbackTTS(down, Local()).synthesize("Hello", "en")) == 5


def test_every_english_line_can_be_prerecorded():
    import itertools

    from app.message import Step, all_english_lines, english_line

    lines = set(all_english_lines("Anil"))
    for key, again, urgent, question in itertools.product(
            ("ask_name_reason", "ask_reason", "ask_name", "goodbye"),
            (False, True), (False, True), (False, True)):
        assert english_line(Step(key, key == "goodbye", None, urgent, question, again),
                            "Anil") in lines


def test_prerecorded_lines_play_and_others_fall_back(tmp_path):
    import numpy as np

    from app.audio import write_wav
    from app.tts import PrerecordedTTS, line_path

    write_wav(line_path(tmp_path, "Thank you.  Goodbye.", "en"), np.full(400, 7, dtype="<i2"))

    class Live:
        def synthesize(self, text, language):
            return np.zeros(3, dtype="<i2")

    tts = PrerecordedTTS(tmp_path, Live())
    assert len(tts.synthesize("Thank you. Goodbye.", "en")) == 400   # spacing ignored
    assert len(tts.synthesize("Something new.", "en")) == 3
    assert (tts.hits, tts.misses) == (1, 1)


def test_make_voice_lists_what_the_server_says(tmp_path):
    import importlib.util
    from pathlib import Path

    from app.config import Config
    from app.tts import PhraseCache

    spec = importlib.util.spec_from_file_location(
        "make_voice", Path(__file__).resolve().parents[2] / "tools" / "make_voice.py")
    mv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mv)
    cfg = Config()
    todo = set(mv.lines_to_record(cfg, "Anil"))

    class Rec:
        def __init__(self):
            self.said = []

        def synthesize(self, text, language):
            import numpy as np
            self.said.append((language, text))
            return np.zeros(10, dtype="<i2")

    rec = Rec()
    cache = PhraseCache(rec, tmp_path, "Anil", languages=cfg.call.phrase_languages,
                        reply_languages=cfg.call.scripted_languages)
    cache.build(cfg.call.languages)
    cache.get("greeting_short", "multi")
    assert set(rec.said) <= todo
