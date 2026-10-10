"""Whole calls through the WebSocket, with fake speech models."""
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.fakes import FakeLLM, FakeSTT, FakeTTS
from app.main import create_app
from app.session import Engines
from app.tts import PhraseCache
from app.vad import EnergyVad

from helpers import FakeEsp, silence, speech, wait_for, wav_bytes

ESP = {"X-Token": "esp-secret"}
APP = {"Authorization": "Bearer app-secret"}


def make_client(config, stt_script=None, replies=None):
    tts = FakeTTS()
    engines = Engines(stt=FakeSTT(stt_script), llm=FakeLLM(replies), tts=tts,
                      phrases=PhraseCache(tts, config.storage.phrase_dir, config.owner.name),
                      vad_factory=EnergyVad, loaded=["fake"])
    return TestClient(create_app(config, engines))


def ready(client, call_id):
    def check():
        r = client.get(f"/api/calls/{call_id}", headers=APP)
        return r.json() if r.status_code == 200 and r.json()["status"] in ("ready", "failed") \
            else None
    return wait_for(check)


def caller_turn(esp, ms=800):
    esp.send_frames(speech(ms) + silence(700))
    time.sleep(0.3)   # let the server answer before the next turn


def test_full_bot_call(config):
    script = [("Hello, this is Ramesh from the courier office.", "en"),
              ("I am at your gate with a parcel that needs a signature.", "en")]
    with make_client(config, script) as client:
        with client.websocket_connect("/esp", headers=ESP) as ws:
            esp = FakeEsp(ws)
            esp.hello()
            d = esp.start_call("c-1", "9812345678")
            assert d == {"type": "decision", "call_id": "c-1", "action": "answer",
                         "max_seconds": 180}
            esp.send(type="call_answered", call_id="c-1")
            caller_turn(esp)
            caller_turn(esp)
            esp.recv_until("hangup")
            assert esp.audio_frames > 50     # greeting and replies were spoken
            esp.send(type="call_end", call_id="c-1", reason="pc_hangup")

        rec = ready(client, "c-1")
        assert rec["status"] == "ready"
        assert rec["number"] == "+919812345678"
        assert rec["handled_by"] == "bot" and rec["end_reason"] == "pc_hangup"
        assert rec["severity"] == "high" and rec["action_needed"] == "call_back"
        assert rec["caller_name"] == "Ramesh"
        assert rec["bot_commitments"]
        speakers = [t["speaker"] for t in rec["turns"]]
        assert speakers[0] == "bot" and speakers.count("caller") == 2
        assert rec["turns"][0]["text"].startswith("Hello, this is Ani's automated assistant")
        caller_turns = [t for t in rec["turns"] if t["speaker"] == "caller"]
        assert caller_turns[0]["start_ms"] < caller_turns[0]["end_ms"] <= caller_turns[1]["start_ms"]

        audio = client.get(rec["audio_url"], headers=APP)
        assert audio.status_code == 200 and len(audio.content) > 100
        part = client.get(rec["audio_url"], headers={**APP, "Range": "bytes=0-99"})
        assert part.status_code == 206 and len(part.content) == 100


def test_language_hint_and_translation(config):
    with make_client(config, [("ನಮಸ್ಕಾರ, ನಾನು ಅಮ್ಮ.", "kn"), ("ಬೇಗ ಕರೆ ಮಾಡು.", "kn")]) as client:
        r = client.post("/api/contacts", headers=APP,
                        json={"name": "Amma", "number": "+919811111111", "group": "family",
                              "language_hint": "kn"})
        assert r.status_code == 201
        with client.websocket_connect("/esp", headers=ESP) as ws:
            esp = FakeEsp(ws)
            esp.hello()
            esp.start_call("c-2", "+919811111111")
            esp.send(type="call_answered", call_id="c-2")
            caller_turn(esp)
            caller_turn(esp)
            esp.recv_until("hangup")
            esp.send(type="call_end", call_id="c-2", reason="pc_hangup")
        rec = ready(client, "c-2")
        assert rec["contact"] == "Amma"
        assert rec["language"] == "kn"
        assert rec["turns"][0]["language"] == "kn"          # full greeting in Kannada
        assert rec["severity"] == "normal"                  # family floor is normal
        caller = [t for t in rec["turns"] if t["speaker"] == "caller"]
        assert all(t["text_en"].startswith("(English)") for t in caller)


def test_blocked_number_rejected(config):
    with make_client(config) as client:
        client.post("/api/contacts", headers=APP,
                    json={"name": "Spam", "number": "9822222222", "group": "blocked"})
        with client.websocket_connect("/esp", headers=ESP) as ws:
            esp = FakeEsp(ws)
            esp.hello()
            assert esp.start_call("c-3", "+919822222222")["action"] == "reject"
        rec = client.get("/api/calls/c-3", headers=APP).json()
        assert rec["handled_by"] == "rejected" and rec["severity"] == "low"


def test_caller_hangs_up_mid_call(config):
    with make_client(config) as client:
        with client.websocket_connect("/esp", headers=ESP) as ws:
            esp = FakeEsp(ws)
            esp.hello()
            esp.start_call("c-4", "")
            esp.send(type="call_answered", call_id="c-4")
            caller_turn(esp)
            esp.send(type="call_end", call_id="c-4", reason="caller_hangup")
            time.sleep(0.2)
        rec = ready(client, "c-4")
        assert rec["end_reason"] == "caller_hangup" and rec["number"] == ""


def test_link_lost_mid_call(config):
    with make_client(config) as client:
        with client.websocket_connect("/esp", headers=ESP) as ws:
            esp = FakeEsp(ws)
            esp.hello()
            esp.start_call("c-5", "9812345678")
            esp.send(type="call_answered", call_id="c-5")
            esp.send_frames(speech(400))
        rec = ready(client, "c-5")
        assert rec["end_reason"] == "link_lost"


def test_two_silences_end_the_call(config):
    with make_client(config) as client:
        with client.websocket_connect("/esp", headers=ESP) as ws:
            esp = FakeEsp(ws)
            esp.hello()
            esp.start_call("c-6", "9812345678")
            esp.send(type="call_answered", call_id="c-6")
            time.sleep(0.2)
            esp.send_frames(silence(6200))
            time.sleep(0.3)
            esp.send_frames(silence(6200))
            esp.recv_until("hangup")
            esp.send(type="call_end", call_id="c-6", reason="pc_hangup")
        rec = ready(client, "c-6")
        bot = [t["text"] for t in rec["turns"] if t["speaker"] == "bot"]
        assert "Are you still there?" in bot and bot[-1].startswith("Thank you")


def test_not_understood_twice(config):
    with make_client(config, [("", "en"), ("", "en")]) as client:
        with client.websocket_connect("/esp", headers=ESP) as ws:
            esp = FakeEsp(ws)
            esp.hello()
            esp.start_call("c-7", "9812345678")
            esp.send(type="call_answered", call_id="c-7")
            caller_turn(esp)
            caller_turn(esp)
            esp.recv_until("hangup")
            esp.send(type="call_end", call_id="c-7", reason="pc_hangup")
        rec = ready(client, "c-7")
        bot = [t["text"] for t in rec["turns"] if t["speaker"] == "bot"]
        assert bot[1] == "Sorry, could you say that again?"
        assert bot[-1].startswith("Sorry, I could not understand")


def test_time_limit(config):
    config.call.max_seconds = 5
    config.call.silence_prompt_s = 100
    with make_client(config) as client:
        with client.websocket_connect("/esp", headers=ESP) as ws:
            esp = FakeEsp(ws)
            esp.hello()
            assert esp.start_call("c-8", "9812345678")["max_seconds"] == 5
            esp.send(type="call_answered", call_id="c-8")
            time.sleep(0.2)
            esp.send_frames(silence(5200))
            esp.recv_until("hangup")
            esp.send(type="call_end", call_id="c-8", reason="pc_hangup")
        rec = ready(client, "c-8")
        assert rec["end_reason"] == "time_limit"
        assert rec["turns"][-1]["text"].startswith("I'm sorry, we are out of time")


def test_bad_token_closed(config):
    with make_client(config) as client:
        with client.websocket_connect("/esp", headers={"X-Token": "wrong"}) as ws:
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_text()
            assert e.value.code == 4401


def test_protocol_major_mismatch(config):
    with make_client(config) as client:
        with client.websocket_connect("/esp", headers=ESP) as ws:
            esp = FakeEsp(ws)
            esp.send(type="hello", device_id="x", fw_version="2.0.0", proto="2.0", vm_pending=0)
            assert esp.recv_until("error")["code"] == "version"


def test_sms_goes_through_esp(config):
    with make_client(config) as client:
        with client.websocket_connect("/esp", headers=ESP) as ws:
            esp = FakeEsp(ws)
            esp.hello()
            r = client.post("/api/test/sms", headers=APP, json={"text": "ping"})
            assert r.status_code == 200
            msg = esp.recv_until("send_sms")
            assert msg["to"] == "+919800000000" and msg["text"] == "ping"
            esp.send(type="sms_result", id=msg["id"], ok=True)
            wait_for(lambda: client.get("/api/status", headers=APP).json()["pending_sms"] == 0)


def test_health_report(config):
    with make_client(config) as client:
        with client.websocket_connect("/esp", headers=ESP) as ws:
            esp = FakeEsp(ws)
            esp.hello()
            esp.send(type="health", registered=True, rssi=18, sd_free_mb=900, on_battery=False)
            st = wait_for(lambda: client.get("/api/status", headers=APP).json()["health"])
            assert st["rssi"] == 18
            assert client.get("/api/status", headers=APP).json()["esp_connected"]


def test_voicemail_upload(config):
    body = wav_bytes(speech(2000))
    headers = {**ESP, "X-Caller": "9812345678", "X-Start": "1791815525", "X-Seconds": "2"}
    with make_client(config, [("This is Priya, call me back when free.", "en")]) as client:
        r = client.post("/esp/voicemail", headers=headers, content=body)
        assert r.status_code == 200 and r.json()["ok"]
        call_id = r.json()["call_id"]
        again = client.post("/esp/voicemail", headers=headers, content=body)
        assert again.json()["call_id"] == call_id            # retry is safe
        rec = ready(client, call_id)
        assert rec["handled_by"] == "voicemail" and rec["status"] == "ready"
        assert [t["speaker"] for t in rec["turns"]] == ["bot", "caller"]
        assert rec["turns"][1]["text"].startswith("This is Priya")
        assert rec["audio_url"]

        assert client.post("/esp/voicemail", headers={**headers, "X-Token": "no"},
                           content=body).status_code == 401
        assert client.post("/esp/voicemail", headers=headers, content=b"junk").status_code == 400


def test_rapid_turns_are_all_kept(config):
    """A caller who keeps talking while the bot is thinking loses no words."""
    script = [("This is Meera.", "en"), ("I am calling about the rent.", "en"),
              ("That is all.", "en")]
    replies = ["Okay."] * 5
    with make_client(config, script, replies) as client:
        with client.websocket_connect("/esp", headers=ESP) as ws:
            esp = FakeEsp(ws)
            esp.hello()
            esp.start_call("c-9", "9812345678")
            esp.send(type="call_answered", call_id="c-9")
            esp.send_frames(speech(600) + silence(700) + speech(600) + silence(700))
            caller_turn(esp)
            esp.send(type="call_end", call_id="c-9", reason="caller_hangup")
            time.sleep(0.2)
        rec = ready(client, "c-9")
        caller = [t["text"] for t in rec["turns"] if t["speaker"] == "caller"]
        assert caller == ["This is Meera.", "I am calling about the rent.", "That is all."]
