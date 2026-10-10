from app.langid import choose_language, summary_language
from app.llm import SentenceSplitter, strip_end_token

ALLOWED = ["en", "hi", "kn"]


def test_contact_hint_wins_first_turn():
    c = choose_language({"en": 0.9}, ALLOWED, "en", "kn", 0)
    assert c.language == "kn" and c.confident


def test_hint_does_not_block_switching_later():
    c = choose_language({"hi": 0.95}, ALLOWED, "kn", "kn", 3)
    assert c.language == "hi"


def test_detection_limited_to_allowed():
    c = choose_language({"ta": 0.8, "kn": 0.15, "en": 0.05}, ALLOWED, "en", None, 0)
    assert c.language == "kn"   # Tamil is not offered; Kannada wins among allowed


def test_low_confidence_keeps_current():
    c = choose_language({"en": 0.4, "hi": 0.35, "kn": 0.25}, ALLOWED, "kn", None, 1)
    assert c.language == "kn" and not c.confident


def test_summary_language():
    assert summary_language(["kn", "kn"]) == "kn"
    assert summary_language(["kn", "en"]) == "mixed"
    assert summary_language([]) == "en"


def test_sentence_splitter_streams():
    s = SentenceSplitter()
    out = []
    for chunk in ["Hello th", "ere. How ", "can I help", "? The price is 3.5 lakh", ". Bye"]:
        out += s.feed(chunk)
    assert out == ["Hello there.", "How can I help?", "The price is 3.5 lakh."]
    assert s.flush() == "Bye"


def test_sentence_splitter_danda():
    s = SentenceSplitter()
    assert s.feed("नमस्ते। आप कौन हैं? ") == ["नमस्ते।", "आप कौन हैं?"]


def test_end_token():
    assert strip_end_token("Goodbye. [END]") == ("Goodbye.", True)
    assert strip_end_token("Hello.") == ("Hello.", False)
