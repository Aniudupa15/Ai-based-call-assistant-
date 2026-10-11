from app.langid import choose_language, summary_language
from app.llm import SentenceSplitter, strip_end_token

ALLOWED = ["en", "hi", "kn"]


def test_contact_hint_wins_first_turn():
    c = choose_language({"en": 0.9}, ALLOWED, "en", "kn", 0)
    assert c.language == "kn" and c.confident


def test_hint_does_not_block_switching_later():
    c = choose_language({"hi": 0.95}, ALLOWED, "kn", "kn", 3)
    assert c.language == "hi"


def test_unoffered_language_keeps_current():
    c = choose_language({"si": 0.53, "kn": 0.3, "en": 0.17}, ALLOWED, "en", None, 2)
    assert c.language == "en" and not c.confident   # misheard as Sinhala: stay in English


def test_switch_needs_confidence():
    assert choose_language({"hi": 0.6, "en": 0.4}, ALLOWED, "en", None, 1).language == "en"
    assert choose_language({"hi": 0.9, "en": 0.1}, ALLOWED, "en", None, 1).language == "hi"
    assert choose_language({"en": 0.4, "hi": 0.35}, ALLOWED, "en", None, 1).confident


def test_low_confidence_keeps_current():
    c = choose_language({"hi": 0.4, "en": 0.35, "kn": 0.25}, ALLOWED, "kn", None, 1)
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


def test_end_marker_variants():
    assert strip_end_token("Goodbye [end]") == ("Goodbye", True)
    assert strip_end_token("Bye. <END>") == ("Bye.", True)
    assert strip_end_token("Bye. END") == ("Bye.", True)
    assert strip_end_token("Call by the end of the month.") == ("Call by the end of the month.", False)


def test_language_detector_decides_and_whisper_transcribes_once():
    import numpy as np

    from app.stt import RoutingSTT, Transcript

    class Whisper:
        def __init__(self):
            self.calls = []

        def detect_language(self, audio):
            self.calls.append("detect")
            return {"el": 0.4, "en": 0.3}

        def transcribe(self, audio, language):
            self.calls.append(f"transcribe-{language}")
            return Transcript("hello", language, 0.8)

    class Indic:
        def transcribe(self, audio, language):
            return Transcript("ನಾನು ರವಿ", language, None)

    class Lid:
        def probs(self, audio, languages):
            return {"kn": 0.9, "en": 0.05, "hi": 0.05}

    w = Whisper()
    stt = RoutingSTT(w, Indic(), Lid(), ["en", "hi", "kn"])
    audio = np.zeros(16000, dtype=np.float32)
    assert stt.detect_language(audio)["kn"] == 0.9
    assert stt.transcribe(audio, "kn").text == "ನಾನು ರವಿ"
    assert w.calls == []                     # Whisper not needed for Kannada at all

    class Broken:
        def probs(self, audio, languages):
            raise RuntimeError("x")

    stt = RoutingSTT(w, Indic(), Broken(), ["en", "hi", "kn"])
    assert stt.detect_language(audio) == {"el": 0.4, "en": 0.3}   # falls back to Whisper
