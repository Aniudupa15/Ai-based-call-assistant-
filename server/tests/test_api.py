from fastapi.testclient import TestClient

from app.db import now
from app.main import create_app

APP = {"Authorization": "Bearer app-secret"}


def client_for(config):
    return TestClient(create_app(config))


def seed_call(client, call_id="c-1", severity="normal", audio=None):
    db = client.app.state.s.db
    db.create_call(call_id, number="+919812345678", started_at=now(), handled_by="bot",
                   status="ready", severity=severity, reason="r", audio_path=audio)
    db.add_turn(call_id, 1, "bot", 0, 1000, "Hello")
    db.set_commitments(call_id, ["Said you will call back"])


def test_auth_and_error_shape(config):
    with client_for(config) as c:
        r = c.get("/api/status")
        assert r.status_code == 401 and r.json() == {"error": "unauthorised",
                                                     "detail": "missing or wrong app token"}
        assert c.get("/api/status", headers={"Authorization": "app-secret"}).status_code == 200
        r = c.get("/api/calls/nope", headers=APP)
        assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_calls_list_filters_and_patch(config):
    with client_for(config) as c:
        seed_call(c, "c-1", "high")
        seed_call(c, "c-2", "low")
        assert [x["id"] for x in c.get("/api/calls?severity=high", headers=APP).json()["calls"]] == ["c-1"]
        assert len(c.get("/api/calls?unseen=true", headers=APP).json()["calls"]) == 2
        r = c.patch("/api/calls/c-2", headers=APP, json={"seen": True, "severity_corrected": "urgent"})
        assert r.json()["severity"] == "urgent" and r.json()["severity_model"] == "low"
        assert r.json()["seen"] is True
        assert len(c.get("/api/calls?unseen=true", headers=APP).json()["calls"]) == 1
        assert c.patch("/api/calls/c-2", headers=APP, json={"severity_corrected": "bad"}).status_code == 422
        full = c.get("/api/calls/c-1", headers=APP).json()
        assert full["bot_commitments"] == ["Said you will call back"]
        assert full["turns"][0]["text"] == "Hello"


def test_delete_call_removes_audio(config, tmp_path):
    audio = tmp_path / "x.wav"
    audio.write_bytes(b"RIFF....")
    with client_for(config) as c:
        seed_call(c, "c-1", audio=str(audio))
        assert c.delete("/api/calls/c-1", headers=APP).json() == {"ok": True}
        assert not audio.exists()
        assert c.get("/api/calls/c-1", headers=APP).status_code == 404
        assert c.delete("/api/calls/c-1", headers=APP).status_code == 404


def test_contacts_crud(config):
    with client_for(config) as c:
        r = c.post("/api/contacts", headers=APP, json={"name": "Ravi", "number": "098 3333 3333"})
        assert r.status_code == 201
        contact = r.json()
        assert contact["number"] == "+919833333333" and contact["group_name"] == "known"
        assert c.post("/api/contacts", headers=APP,
                      json={"name": "Dup", "number": "9833333333"}).status_code == 409
        assert c.post("/api/contacts", headers=APP,
                      json={"name": "X", "number": "9811", "group": "nope"}).status_code == 400
        r = c.put(f"/api/contacts/{contact['id']}", headers=APP,
                  json={"name": "Ravi K", "number": "9833333333", "group": "family",
                        "language_hint": "hi"})
        assert r.json()["group_name"] == "family" and r.json()["language_hint"] == "hi"
        assert len(c.get("/api/contacts", headers=APP).json()["contacts"]) == 1
        assert c.delete(f"/api/contacts/{contact['id']}", headers=APP).json() == {"ok": True}


def test_groups_and_rules(config):
    with client_for(config) as c:
        groups = {g["name"]: g for g in c.get("/api/groups", headers=APP).json()["groups"]}
        assert set(groups) == {"family", "known", "unknown", "blocked"}
        rules = {r["name"]: r for r in c.get("/api/rules", headers=APP).json()["rules"]}
        r = c.put(f"/api/rules/{rules['unknown']['id']}", headers=APP,
                  json={"action": "ignore", "max_seconds": 90})
        assert r.json()["action"] == "ignore" and r.json()["max_seconds"] == 90
        r = c.put(f"/api/groups/{groups['known']['id']}", headers=APP,
                  json={"rule_id": rules["family"]["id"]})
        assert r.json()["rule_id"] == rules["family"]["id"]
        assert c.put(f"/api/rules/{rules['unknown']['id']}", headers=APP,
                     json={"action": "dance"}).status_code == 422
        fam = rules["family"]["id"]
        assert c.put(f"/api/rules/{fam}", headers=APP,
                     json={"severity_floor": ""}).json()["severity_floor"] is None


def test_settings(config):
    with client_for(config) as c:
        assert c.get("/api/settings", headers=APP).json()["owner_name"] == "Ani"
        r = c.put("/api/settings", headers=APP,
                  json={"owner_name": "Anirudh", "owner_number": "9800000001", "retention_days": 7})
        assert r.json() == {"owner_name": "Anirudh", "owner_number": "+919800000001",
                            "retention_days": 7}
        assert c.app.state.s.engines.phrases.owner_name == "Anirudh"


def test_events_long_poll_marks_notified(config):
    with client_for(config) as c:
        seed_call(c, "c-1", "high")
        c.app.state.s.bus.publish("call_ready", "c-1")
        r = c.get("/api/events?after=0&timeout=1", headers=APP).json()
        assert r["events"][0]["kind"] == "call_ready" and r["events"][0]["severity"] == "high"
        assert c.app.state.s.db.get_call("c-1")["notified_app_at"]
        empty = c.get(f"/api/events?after={r['last_seq']}&timeout=0", headers=APP).json()
        assert empty["events"] == [] and empty["last_seq"] == r["last_seq"]


def test_test_sms_needs_esp(config):
    with client_for(config) as c:
        r = c.post("/api/test/sms", headers=APP, json={})
        assert r.status_code == 503 and r.json()["error"] == "esp_offline"


def test_retention_and_recover(config, tmp_path):
    audio = tmp_path / "old.wav"
    audio.write_bytes(b"x")
    with client_for(config) as c:
        s = c.app.state.s
        s.db.create_call("old", number="", started_at=now() - 40 * 86400, handled_by="bot",
                         status="ready", audio_path=str(audio), summary="keep me")
        assert s.calls.apply_retention() == 1
        assert not audio.exists()
        row = s.db.get_call("old")
        assert row["audio_path"] is None and row["summary"] == "keep me"
        s.db.create_call("stuck", number="", started_at=now(), handled_by="bot", status="live")
        s.calls.recover()
        assert s.db.get_call("stuck")["status"] == "failed"
