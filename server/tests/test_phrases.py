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
