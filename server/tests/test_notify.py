import asyncio

from app.config import NotifyConfig
from app.db import now
from app.notify import EventBus, Notifier, format_combined_sms, format_summary_sms
from app.settings import Settings


def make(db, config):
    bus = EventBus(db)
    n = Notifier(db, Settings(config, db), NotifyConfig(), bus)
    sent = []

    async def sender(sms_id, to, text):
        sent.append((sms_id, to, text))
        return True

    n.sms_sender = sender
    return n, sent


def add_call(db, call_id, severity, ended, **kw):
    db.create_call(call_id, number="+919812345678", started_at=ended - 60, ended_at=ended,
                   handled_by="bot", status="ready", severity=severity,
                   reason="Parcel needs signature, at gate now", action_needed="call_back",
                   caller_name="Ramesh", **kw)


def test_sms_format_matches_spec():
    call = {"severity": "high", "caller_name": "Ramesh (courier)", "number": "+919812345678",
            "reason": "Parcel needs signature, at gate now", "action_needed": "call_back"}
    assert format_summary_sms(call) == \
        "HIGH | Ramesh (courier) 9812345678 | Parcel needs signature, at gate now | Call back"
    call["reason"] = "x" * 300
    assert len(format_summary_sms(call)) == 160


def test_urgent_sms_at_once(db, config):
    n, sent = make(db, config)
    add_call(db, "c1", "urgent", now())
    asyncio.run(n.record_ready("c1"))
    assert len(sent) == 1 and sent[0][1] == "+919800000000"
    assert sent[0][2].startswith("URGENT | Ramesh")
    assert db.get_call("c1")["notified_sms_at"]
    assert db.events_after(0)[0]["kind"] == "call_ready"


def test_high_sms_only_if_app_has_not_fetched(db, config):
    n, sent = make(db, config)
    t = now()
    add_call(db, "c1", "high", t - 11 * 60)
    add_call(db, "c2", "high", t - 11 * 60, notified_app_at=t - 600)
    add_call(db, "c3", "high", t - 5 * 60)
    asyncio.run(n.tick(t))
    assert [s[2].startswith("HIGH") for s in sent] == [True]
    assert db.get_call("c1")["notified_sms_at"] and not db.get_call("c3")["notified_sms_at"]
    asyncio.run(n.tick(t))
    assert len(sent) == 1   # not sent twice


def test_normal_combined_when_app_unreachable(db, config):
    n, sent = make(db, config)
    t = now()
    add_call(db, "c1", "normal", t - 2 * 3600)
    add_call(db, "c2", "normal", t - 3 * 3600)
    add_call(db, "c3", "low", t - 3 * 3600)
    n.app_last_seen = t - 30 * 60          # app seen recently: nothing
    asyncio.run(n.tick(t))
    assert sent == []
    n.app_last_seen = t - 2 * 3600
    asyncio.run(n.tick(t))
    assert len(sent) == 1 and sent[0][2].startswith("2 calls |")


def test_low_never_texts(db, config):
    n, sent = make(db, config)
    add_call(db, "c1", "low", now() - 5 * 3600)
    asyncio.run(n.record_ready("c1"))
    asyncio.run(n.tick(now()))
    assert sent == []


def test_sms_waits_for_link_then_flushes(db, config):
    n, sent = make(db, config)
    sender = n.sms_sender
    n.sms_sender = None
    asyncio.run(n.send_sms("hello"))
    assert sent == [] and len(db.pending_sms()) == 1
    n.sms_sender = sender
    asyncio.run(n.flush_sms())
    assert len(sent) == 1
    n.sms_result(sent[0][0], True)
    assert db.pending_sms() == []


def test_health_alert(db, config):
    n, _ = make(db, config)
    n.health(registered=False, rssi=99, sd_free_mb=500, on_battery=False)
    assert db.events_after(0)[-1]["kind"] == "health_alert"


def test_combined_format():
    calls = [{"caller_name": "A", "number": "+919811111111", "reason": "hi"},
             {"caller_name": None, "number": "", "reason": "hello"}]
    assert format_combined_sms(calls) == "2 calls | A 9811111111: hi; withheld: hello"


def test_event_bus_wakes_waiter(db):
    bus = EventBus(db)

    async def go():
        waiter = asyncio.create_task(bus.wait_after(0, 5))
        await asyncio.sleep(0.05)
        bus.publish("call_ready", "c1")
        return await asyncio.wait_for(waiter, 1)

    events = asyncio.run(go())
    assert events[0]["call_id"] == "c1"
