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
