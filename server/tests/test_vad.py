import numpy as np

from app.audio import tone
from app.vad import EnergyVad, TurnDetector


def run(detector, pattern, bot=False):
    events = []
    for speech in pattern:
        ev = detector.feed(speech, bot)
        if ev:
            events.append(ev)
    return events


def test_speech_start_needs_200ms():
    d = TurnDetector(200, 600, 300)
    assert run(d, [True] * 9) == []
    ev = run(d, [True])
    assert ev[0].kind == "speech_start" and ev[0].start_frame == 0


def test_end_of_turn_after_600ms_silence():
    d = TurnDetector(200, 600, 300)
    run(d, [True] * 20)
    assert run(d, [False] * 29) == []
    ev = run(d, [False])
    assert ev[0].kind == "end_of_turn"


def test_short_pause_does_not_end_turn():
    d = TurnDetector(200, 600, 300)
    evs = run(d, [True] * 20 + [False] * 20 + [True] * 10 + [False] * 30)
    assert [e.kind for e in evs] == ["speech_start", "end_of_turn"]


def test_blip_is_not_speech():
    d = TurnDetector(200, 600, 300)
    assert run(d, [True] * 5 + [False] * 5 + [True] * 5) == []


def test_barge_in_needs_300ms_while_bot_speaks():
    d = TurnDetector(200, 600, 300)
    assert run(d, [True] * 14, bot=True) == []
    ev = run(d, [True], bot=True)
    assert ev[0].kind == "barge_in"


def test_silence_counter():
    d = TurnDetector(200, 600, 300)
    run(d, [False] * 50)
    assert d.silent_frames() == 50
    d.reset_silence()
    assert d.silent_frames() == 0


def test_energy_vad_has_higher_bar_while_bot_speaks():
    vad = EnergyVad(threshold_dbfs=-35, echo_margin_db=8)
    loud = tone(300, 20, level=0.3)
    quiet = tone(300, 20, level=0.03)          # about -33 dBFS
    assert vad.is_speech(loud, False)
    assert vad.is_speech(quiet, False)
    assert not vad.is_speech(quiet, True)
    assert not vad.is_speech(np.zeros(160, dtype="<i2"), False)
