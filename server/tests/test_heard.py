from app.heard import clean_reply, is_echo, is_noise, language_request


def test_noise_and_hallucinations():
    assert is_noise("", None)
    assert is_noise("...", 0.9)
    assert is_noise("Thanks for watching!", 0.9)
    assert is_noise("you", 0.8)
    assert is_noise("Thank you.", 0.3)           # weak, and barely heard
    assert not is_noise("Thank you.", 0.8)       # weak, but clearly said
    assert not is_noise("Hello?", 0.3)
    assert not is_noise("ನಮಸ್ಕಾರ", None)


def test_echo_of_the_bot():
    bot = ["Thank you, I will pass your message on.", "Goodbye."]
    assert is_echo("Goodbye.", bot)
    assert is_echo("thank you I will pass your message on", bot)
    assert is_echo("pass your message", bot)     # a piece of it
    assert not is_echo("This is Ramesh about the parcel.", bot)
    assert not is_echo("Thank you.", bot)


def test_language_request():
    assert language_request("Kannada.") == "kn"
    assert language_request("Hindi please") == "hi"
    assert language_request("ಕನ್ನಡ") == "kn"
    assert language_request("English") == "en"
    assert language_request("I want to talk about the Kannada film festival next week") is None
    assert language_request("Hindi or English") is None


def test_clean_reply():
    assert clean_reply("**Hello Ramesh!** 😊") == "Hello Ramesh!"
    assert clean_reply("Assistant: Thank you.") == "Thank you."
    assert clean_reply("😊") == ""
    assert clean_reply("ನಮಸ್ಕಾರ, ರಮೇಶ್.") == "ನಮಸ್ಕಾರ, ರಮೇಶ್."


def test_message_taker_flow():
    from app.message import MessageTaker, Slots, english_line, fallback_slots, parse_slots

    m = MessageTaker()
    step = m.update(Slots())
    assert step.key == "ask_name_reason"
    step = m.update(Slots(name="Ananya Udupa", urgent=True))
    assert step.key == "ask_reason" and english_line(step, "Anil") == \
        "I understand it's urgent. Could you briefly tell me what it's about?"   # 2nd ask
    step = m.update(Slots(reason="a private matter"))      # name is remembered
    assert step.end and "Thank you, Ananya." in english_line(step, "Anil")
    # The owner's name is never taken as the caller's.
    assert parse_slots('{"name": "Anirudha", "reason": null}', "Anirudha Udupa").name is None
    assert parse_slots('{"name": "Ananya", "urgent": "true"}', "Anirudha").urgent
    assert fallback_slots(["Hi, this is Ramesh about the parcel today"]).name == "Ramesh"
