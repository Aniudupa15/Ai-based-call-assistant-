"""Caller scripts for the call simulator (app/sim.py).

Each one reproduces something that goes wrong on real calls: slow models, people
who pause mid-sentence, coughs, echo of the bot's voice, misheard languages,
callers who interrupt or go quiet.
"""
from __future__ import annotations

from .sim import Hangup, Noise, Pause, Say, Scenario, WaitBot, unknown_caller

SCENARIOS: list[Scenario] = [
    Scenario("happy_path", "name and reason in two turns, bot ends the call", [
        WaitBot(),
        Say("Hi, this is Ramesh."),
        WaitBot(),
        Say("I am calling about the parcel delivery today."),
        WaitBot(),
    ]),
    Scenario("all_at_once", "everything in the first turn", [
        WaitBot(),
        Say("Hello, my name is Priya, I am calling about tomorrow's meeting."),
        WaitBot(),
    ]),
    Scenario("slow_model", "first token after 3 s: one filler, the full reply after it", [
        WaitBot(),
        Say("Hi, this is Ramesh."),
        WaitBot(),
        Say("It is about the electricity bill."),
        WaitBot(),
    ], llm={"first_token_s": 3.0, "tokens_per_s": 12}, call={"reply_mode": "llm"}),
    Scenario("slow_stt", "transcription takes 3 s: no 'are you still there' meanwhile", [
        WaitBot(),
        Say("Hi, this is Ramesh, calling about the parcel."),
        WaitBot(),
    ], stt={"detect_s": 3.0, "english_s": 3.0}),
    Scenario("very_slow_indic", "a 7 s Kannada transcription must not trigger prompts", [
        WaitBot(),
        Say("ನಮಸ್ಕಾರ, ನಾನು ರಮೇಶ್, ಪಾರ್ಸೆಲ್ ಬಗ್ಗೆ ಕರೆ ಮಾಡಿದ್ದೇನೆ.", "kn"),
        WaitBot(timeout_s=30),
    ], stt={"indic_s": 7.0}),
    Scenario("mid_sentence_pause", "a 0.8 s thinking pause inside one sentence", [
        WaitBot(),
        Say("Hi, this is Ramesh and I am calling about", ms=2400, gaps_ms=((1200, 800),)),
        Say("the water bill."),
        WaitBot(),
    ]),
    Scenario("cough_while_thinking", "a cough just after speaking must not lose the reply", [
        WaitBot(),
        Say("Hi, this is Ramesh, calling about the parcel."),
        Pause(900),
        Noise(300),
        WaitBot(),
    ]),
    Scenario("noise_only", "line noise bursts: no 'please send a text, goodbye'", [
        WaitBot(),
        Noise(400),
        Pause(1500),
        Noise(500),
        Pause(1500),
        Say("Sorry, this is Ramesh, about the parcel."),
        WaitBot(),
    ]),
    Scenario("echo_line", "echo of the bot's voice: it must not answer itself", [
        WaitBot(),
        Say("Hi, this is Ramesh."),
        WaitBot(),
        Say("It is about the meeting tomorrow."),
        WaitBot(),
    ], echo_level=0.4, echo_delay_ms=250),
    Scenario("echo_loud", "echo as loud as the bot (laptop speakers): still no loop", [
        WaitBot(),
        Say("Hi, this is Ramesh."),
        WaitBot(),
        Say("It is about the meeting tomorrow."),
        WaitBot(),
    ], echo_level=1.2, echo_delay_ms=300),
    Scenario("barge_in", "caller talks over a long reply", [
        WaitBot(),
        Say("Hi."),
        Pause(2200),               # the reply has started playing
        Say("Sorry, this is Ramesh, about the parcel."),
        WaitBot(),
    ], llm={"replies": ["Hello there. I am the automated assistant for Anil, who is busy "
                        "at the moment. Could you please tell me your name and the reason "
                        "for your call so that I can pass it on?"]}, call={"reply_mode": "llm"}),
    Scenario("false_barge_in", "a cough over the reply: the bot carries on", [
        WaitBot(),
        Say("Hi, this is Ramesh."),
        Pause(1700),
        Noise(400),
        WaitBot(),
        Say("It is about the parcel."),
        WaitBot(),
    ], llm={"replies": ["Thank you, Ramesh. Anil is not available right now. What is the "
                        "call about?"]}, call={"reply_mode": "llm"}),
    Scenario("talks_over_greeting", "caller says hello during the greeting", [
        Pause(1200),
        Say("Hello? Hello?"),
        WaitBot(),
        Say("This is Ramesh, calling about the parcel."),
        WaitBot(),
    ]),
    Scenario("silent_caller", "says nothing: one prompt, then goodbye", [
    ], max_s=40),
    Scenario("quiet_after_question", "answers after 4 s of thought: no prompt", [
        WaitBot(),
        Say("Hi."),
        WaitBot(),
        Pause(4000),
        Say("This is Ramesh, calling about the parcel."),
        WaitBot(),
    ]),
    Scenario("caller_hangs_up", "hangs up while the bot is replying", [
        WaitBot(),
        Say("Hi, this is Ramesh, about the parcel."),
        Pause(1000),
        Hangup(),
    ]),
    Scenario("misheard_kannada", "Kannada heard as Sinhala: no wrong switch, asks language once", [
        WaitBot(),
        Say("ನಮಸ್ಕಾರ, ನಾನು ರಮೇಶ್.", "kn", detected={"si": 0.5, "kn": 0.3, "en": 0.1}),
        WaitBot(),
        Say("Kannada.", "kn", detected={"kn": 0.6, "en": 0.3}),
        WaitBot(),
        Say("ಪಾರ್ಸೆಲ್ ಬಗ್ಗೆ ಕರೆ ಮಾಡಿದ್ದೇನೆ.", "kn", detected={"kn": 0.55, "si": 0.4}),
        WaitBot(),
    ]),
    Scenario("kannada_heard_as_nothing", "4 s of Kannada decoded as English gives no words", [
        WaitBot(),
        Say("ನಾನು ರಮೇಶ್, ನನಗೆ ಪಾರ್ಸೆಲ್ ಬಗ್ಗೆ ಸ್ವಲ್ಪ ಮಾತಾಡ್ಲಿಕ್ಕಿತ್ತು.", "kn", ms=4000,
            detected={"en": 0.3, "kn": 0.25, "ta": 0.2}, garbled=""),
        WaitBot(),
        Say("Kannada.", detected={"en": 0.5, "kn": 0.4}),
        WaitBot(),
        Say("ನಾನು ರಮೇಶ್.", "kn", detected={"kn": 0.8}),
        WaitBot(),
        Say("ಪಾರ್ಸೆಲ್ ಬಗ್ಗೆ.", "kn", detected={"kn": 0.8}),
        WaitBot(),
    ]),
    Scenario("desk_test_call", "the owner's own desk test: urgent, name late, private reason", [
        WaitBot(),
        Say("Yeah, I want to speak to Anil."),
        WaitBot(),
        Say("It is very urgent. I am Ananya, his elder sister."),
        WaitBot(),
        Say("It is something private."),
        WaitBot(),
    ]),
    Scenario("caller_declines", "will not give a name or reason: asked twice, then goodbye", [
        WaitBot(),
        Say("Just tell him to call me back."),
        WaitBot(),
        Say("No, I won't say."),
        WaitBot(),
        Say("No."),
        WaitBot(),
    ]),
    Scenario("caller_asks_question", "asks when the owner is back", [
        WaitBot(),
        Say("This is Ramesh. When will he be back?"),
        WaitBot(),
        Say("It is about the parcel."),
        WaitBot(),
    ]),
    Scenario("accented_english_low_conf", "unsure English (conf 0.42, heard as Finnish): no language question", [
        WaitBot(),
        Say("Amazon delivery, there is a parcel for him.", confidence=0.42,
            detected={"fi": 0.38, "en": 0.3}),
        WaitBot(),
        Say("This is Ravi."),
        WaitBot(),
    ]),
    Scenario("language_answer_canada", "asked the language, 'Kannada' heard as 'Canada'", [
        WaitBot(),
        Say("Mumble mumble parcel.", confidence=0.2, detected={"fi": 0.4, "en": 0.3}),
        WaitBot(),
        Say("Canada.", "kn", detected={"en": 0.6, "kn": 0.3}, garbled="Canada."),
        WaitBot(),
        Say("ನಾನು ರವಿ, ಪಾರ್ಸೆಲ್ ಬಗ್ಗೆ.", "kn", detected={"kn": 0.8}),
        WaitBot(),
    ]),
    Scenario("hindi_caller", "Hindi detected confidently: replies in Hindi", [
        WaitBot(),
        Say("नमस्ते, मैं रमेश बोल रहा हूँ, पार्सल के बारे में।", "hi",
            detected={"hi": 0.9, "en": 0.05}),
        WaitBot(),
    ]),
    Scenario("accent_english", "English heard as Hindi at 0.55: stays English, no question", [
        WaitBot(),
        Say("Hello, this is Ramesh calling about the parcel.",
            detected={"hi": 0.55, "en": 0.4}),
        WaitBot(),
    ]),
    Scenario("rambling_model", "model writes four sentences and markdown", [
        WaitBot(),
        Say("Hi, this is Ramesh."),
        WaitBot(),
        Say("About the parcel."),
        WaitBot(),
    ], llm={"replies": ["**Hello Ramesh!** Thanks for calling. Anil is busy. I can take a "
                        "message. What is it about? 😊",
                        "Got it. I will tell Anil about the parcel. Goodbye! [END]"]}, call={"reply_mode": "llm"}),
    Scenario("model_never_ends", "model never says [END]: the call still finishes", [
        WaitBot(),
        Say("Hi, this is Ramesh, about the parcel."),
        WaitBot(),
        Say("No, that is all."),
        WaitBot(),
    ], llm={"policy": lambda m: "Okay. Is there anything else?"}, max_s=45, call={"reply_mode": "llm"}),
    Scenario("noisy_line", "constant background hiss", [
        WaitBot(),
        Say("Hi, this is Ramesh, calling about the parcel."),
        WaitBot(),
    ], background=0.01),
    Scenario("family_contact", "contact with a fixed Hindi language", [
        WaitBot(),
        Say("हाँ, मैं माँ बोल रही हूँ, घर पर बात करनी है।", "hi", detected={"hi": 0.8}),
        WaitBot(),
    ], rule=unknown_caller("hi")),
]


def by_name(name: str) -> Scenario:
    return next(s for s in SCENARIOS if s.name == name)
