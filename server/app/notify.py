"""App notifications (an event feed the app long-polls) and SMS summaries.

| Severity | App notification      | SMS                                              |
|----------|-----------------------|--------------------------------------------------|
| urgent   | priority              | always, at once                                  |
| high     | marked high           | if the app has not fetched it within 10 minutes  |
| normal   | standard              | if the app has been unreachable for 1 hour, one combined SMS |
| low      | none; history only    | never                                            |
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .config import NotifyConfig
from .db import Database, now
from .protocol import SMS_MAX_CHARS
from .rules import local_display
from .settings import Settings

log = logging.getLogger(__name__)

ACTION_LABELS = {"call_back": "Call back", "send_info": "Send info", "none": "No action"}
HEALTH_STALE_S = 15 * 60


def severity_of(call: dict) -> str:
    return call.get("severity_corrected") or call.get("severity") or "normal"


def _clip(text: str, limit: int = SMS_MAX_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def caller_label(call: dict) -> str:
    name = call.get("contact_name") or call.get("caller_name")
    number = local_display(call.get("number") or "") or "withheld"
    return f"{name} {number}" if name else number


def format_summary_sms(call: dict) -> str:
    """'HIGH | Ramesh (courier) 98xxxxxxxx | Parcel needs signature | Call back'"""
    parts = [
        severity_of(call).upper(),
        caller_label(call),
        call.get("reason") or call.get("summary") or "Listen to the recording",
        ACTION_LABELS.get(call.get("action_needed") or "none", "No action"),
    ]
    return _clip(" | ".join(parts))


def format_combined_sms(calls: list[dict]) -> str:
    items = [f"{caller_label(c)}: {c.get('reason') or 'message'}" for c in calls]
    return _clip(f"{len(calls)} calls | " + "; ".join(items))


class EventBus:
    """Writes events to the database and wakes long-polling requests."""

    def __init__(self, db: Database):
        self.db = db
        self._waiters: set[asyncio.Event] = set()

    def publish(self, kind: str, call_id: str | None = None) -> int:
        seq = self.db.add_event(kind, call_id)
        for ev in list(self._waiters):
            ev.set()
        return seq

    async def wait_after(self, seq: int, timeout: float) -> list[dict]:
        deadline = time.monotonic() + timeout
        while True:
            events = self.db.events_after(seq)
            remaining = deadline - time.monotonic()
            if events or remaining <= 0:
                return events
            ev = asyncio.Event()
            self._waiters.add(ev)
            try:
                await asyncio.wait_for(ev.wait(), remaining)
            except asyncio.TimeoutError:
                pass
            finally:
                self._waiters.discard(ev)


SmsSender = Callable[[int, str, str], Awaitable[bool]]


class Notifier:
    def __init__(self, db: Database, settings: Settings, config: NotifyConfig, bus: EventBus):
        self.db = db
        self.settings = settings
        self.config = config
        self.bus = bus
        self.sms_sender: SmsSender | None = None  # set by the gateway while the ESP32 is linked
        self.app_last_seen = 0
        self._health_alerted = False

    def touch_app(self) -> None:
        self.app_last_seen = now()

    # ---- SMS -----------------------------------------------------------------
    async def send_sms(self, text: str, to: str | None = None) -> int | None:
        to = to or self.settings.owner_number
        if not to:
            log.warning("no owner number set; SMS dropped: %s", text)
            return None
        sms_id = self.db.queue_sms(to, _clip(text))
        await self.flush_sms()
        return sms_id

    async def flush_sms(self) -> None:
        """Hands queued texts to the ESP32. Called on enqueue and when the link comes up."""
        if self.sms_sender is None:
            return
        for sms in self.db.pending_sms():
            if sms["sent_at"] and now() - sms["sent_at"] < 60:
                continue  # waiting for sms_result
            ok = await self.sms_sender(sms["id"], sms["to_number"], sms["text"])
            if not ok:
                break
            self.db.mark_sms_attempt(sms["id"])

    def sms_result(self, sms_id: int, ok: bool) -> None:
        self.db.mark_sms(sms_id, ok)
        if not ok:
            log.error("module failed to send SMS %s", sms_id)

    # ---- records ---------------------------------------------------------------
    async def record_ready(self, call_id: str) -> None:
        call = self.db.get_call(call_id)
        if not call:
            return
        self.bus.publish("call_ready", call_id)
        if severity_of(call) == "urgent" and self.config.sms_urgent:
            await self.send_sms(format_summary_sms(call))
            self.db.update_call(call_id, notified_sms_at=now())

    async def tick(self, at: int | None = None) -> None:
        """Runs every 30 seconds: late SMS for high and normal calls, health watch."""
        at = at or now()
        recent = self.db.list_calls(since=at - 86400, limit=200)
        ready = [c for c in recent if c["status"] == "ready" and not c["notified_sms_at"]
                 and c["handled_by"] in ("bot", "voicemail")]

        high_after = self.config.sms_high_after_minutes * 60
        for call in ready:
            if (severity_of(call) == "high" and not call["notified_app_at"]
                    and (call["ended_at"] or call["started_at"]) <= at - high_after):
                await self.send_sms(format_summary_sms(call))
                self.db.update_call(call["id"], notified_sms_at=at)

        normal_after = self.config.sms_normal_after_minutes * 60
        if self.app_last_seen <= at - normal_after:
            pending = [c for c in ready if severity_of(c) == "normal" and not c["notified_app_at"]
                       and (c["ended_at"] or c["started_at"]) <= at - normal_after]
            if pending:
                text = (format_summary_sms(pending[0]) if len(pending) == 1
                        else format_combined_sms(pending))
                await self.send_sms(text)
                for c in pending:
                    self.db.update_call(c["id"], notified_sms_at=at)

        last = self.db.last_health()
        if last and last["ts"] < at - HEALTH_STALE_S and not self._health_alerted:
            self._health_alerted = True
            self.bus.publish("health_alert")

    def health(self, registered: bool, rssi: int, sd_free_mb: int, on_battery: bool) -> None:
        self.db.add_health(registered, rssi, sd_free_mb, on_battery)
        self._health_alerted = False
        if not registered or on_battery or sd_free_mb < 20:
            # A module that lost the network cannot text, so the PC raises the alert.
            self.bus.publish("health_alert")
