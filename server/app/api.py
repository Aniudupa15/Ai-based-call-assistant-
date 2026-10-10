"""REST API for the Android app. Reachable on the home network or through WireGuard.

Every request carries the app token in an `Authorization` header. Errors return
{"error": "code", "detail": "text"}.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .db import Database, now
from .models import CallPatch, ContactIn, GroupPatch, RuleIn, SettingsIn, TestSmsIn
from .notify import EventBus, Notifier
from .rules import normalise_number

AUDIO_TYPES = {".opus": "audio/ogg", ".wav": "audio/wav"}


class ApiError(HTTPException):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(status_code=status, detail=detail)
        self.code = code


def build_router(app_state) -> APIRouter:
    """app_state carries db, config, settings, gateway, calls, notifier, bus, engines."""
    s = app_state
    db: Database = s.db
    bus: EventBus = s.bus
    notifier: Notifier = s.notifier

    def auth(request: Request) -> None:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else header
        if token != s.config.network.app_token:
            raise ApiError(401, "unauthorised", "missing or wrong app token")
        notifier.touch_app()

    r = APIRouter(prefix="/api", dependencies=[Depends(auth)])

    def call_or_404(call_id: str) -> dict:
        call = db.get_call(call_id)
        if not call:
            raise ApiError(404, "not_found", f"no call {call_id}")
        return call

    def present(call: dict, full: bool = False) -> dict:
        out = {
            "id": call["id"],
            "number": call["number"],
            "contact": call.get("contact_name"),
            "caller_name": call.get("caller_name"),
            "started_at": call["started_at"],
            "duration_s": call.get("duration_s") or 0,
            "handled_by": call["handled_by"],
            "status": call["status"],
            "language": call.get("language"),
            "severity": call.get("severity_corrected") or call.get("severity"),
            "severity_model": call.get("severity"),
            "severity_corrected": call.get("severity_corrected"),
            "severity_why": call.get("severity_why"),
            "reason": call.get("reason"),
            "summary": call.get("summary"),
            "action_needed": call.get("action_needed"),
            "deadline": call.get("deadline"),
            "callback_number": call.get("callback_number"),
            "flags": call.get("flags") or [],
            "end_reason": call.get("end_reason"),
            "seen": call.get("seen_at") is not None,
            "audio_url": f"/api/calls/{call['id']}/audio" if call.get("audio_path") else None,
        }
        if full:
            out["bot_commitments"] = db.get_commitments(call["id"])
            out["turns"] = [{k: t[k] for k in ("seq", "speaker", "start_ms", "end_ms",
                                               "language", "text", "text_en")}
                            for t in db.get_turns(call["id"])]
        return out

    # ---- status and events ---------------------------------------------------
    @r.get("/status")
    def status():
        gw = s.gateway
        health = db.last_health()
        return {
            "server": "ok",
            "time": now(),
            "esp_connected": gw.connected,
            "esp_since": gw.connected_since,
            "device": gw.device,
            "call_live": gw.session is not None,
            "health": health,
            "models_loaded": s.engines.loaded,
            "pending_sms": len(db.pending_sms()),
        }

    @r.get("/events")
    async def events(after: int = 0, timeout: float = Query(60, ge=0, le=60)):
        items = await bus.wait_after(after, timeout)
        for e in items:
            if e["kind"] == "call_ready" and e["call_id"]:
                call = db.get_call(e["call_id"])
                if call and not call["notified_app_at"]:
                    db.update_call(e["call_id"], notified_app_at=now())
                if call:
                    e["severity"] = call.get("severity_corrected") or call.get("severity")
                    e["summary"] = call.get("reason") or call.get("summary")
                    e["caller"] = call.get("contact_name") or call.get("caller_name") \
                        or call.get("number")
        return {"events": items, "last_seq": items[-1]["seq"] if items else after}

    # ---- calls ---------------------------------------------------------------
    @r.get("/calls")
    def list_calls(since: Optional[int] = None, severity: Optional[str] = None,
                   handled_by: Optional[str] = None, unseen: bool = False,
                   limit: int = Query(100, ge=1, le=500)):
        return {"calls": [present(c) for c in db.list_calls(since, severity, handled_by,
                                                            unseen, limit)]}

    @r.get("/calls/{call_id}")
    def get_call(call_id: str):
        call = call_or_404(call_id)
        if not call["notified_app_at"]:
            db.update_call(call_id, notified_app_at=now())
        return present(call, full=True)

    @r.get("/calls/{call_id}/audio")
    def get_audio(call_id: str):
        call = call_or_404(call_id)
        path = Path(call["audio_path"]) if call.get("audio_path") else None
        if not path or not path.exists():
            raise ApiError(404, "no_audio", "recording deleted or not ready")
        # FileResponse answers Range requests, so the app can seek.
        return FileResponse(path, media_type=AUDIO_TYPES.get(path.suffix, "application/octet-stream"),
                            filename=path.name)

    @r.patch("/calls/{call_id}")
    def patch_call(call_id: str, body: CallPatch):
        call_or_404(call_id)
        fields = {}
        if body.seen is not None:
            fields["seen_at"] = now() if body.seen else None
        if body.severity_corrected is not None:
            fields["severity_corrected"] = body.severity_corrected
        db.update_call(call_id, **fields)
        return present(call_or_404(call_id), full=True)

    @r.delete("/calls/{call_id}")
    def delete_call(call_id: str):
        if not s.calls.delete_call(call_id):
            raise ApiError(404, "not_found", f"no call {call_id}")
        return {"ok": True}

    # ---- contacts, groups, rules ---------------------------------------------
    def resolve_group(body: ContactIn) -> Optional[int]:
        if body.group_id is not None:
            if not db.get_group(body.group_id):
                raise ApiError(400, "bad_group", f"no group {body.group_id}")
            return body.group_id
        if body.group:
            g = db.group_by_name(body.group)
            if not g:
                raise ApiError(400, "bad_group", f"no group {body.group}")
            return g["id"]
        g = db.group_by_name("known")
        return g["id"] if g else None

    @r.get("/contacts")
    def list_contacts():
        return {"contacts": db.list_contacts()}

    @r.post("/contacts", status_code=201)
    def add_contact(body: ContactIn):
        number = normalise_number(body.number)
        if not number:
            raise ApiError(400, "bad_number", "number is empty or withheld")
        try:
            cid = db.add_contact(body.name, number, resolve_group(body), body.language_hint,
                                 body.notes)
        except sqlite3.IntegrityError:
            raise ApiError(409, "duplicate", f"{number} is already a contact")
        return db.get_contact(cid)

    @r.put("/contacts/{contact_id}")
    def update_contact(contact_id: int, body: ContactIn):
        if not db.get_contact(contact_id):
            raise ApiError(404, "not_found", f"no contact {contact_id}")
        try:
            db.update_contact(contact_id, name=body.name, number=normalise_number(body.number),
                              group_id=resolve_group(body), language_hint=body.language_hint,
                              notes=body.notes)
        except sqlite3.IntegrityError:
            raise ApiError(409, "duplicate", "another contact has this number")
        return db.get_contact(contact_id)

    @r.delete("/contacts/{contact_id}")
    def delete_contact(contact_id: int):
        if not db.delete_contact(contact_id):
            raise ApiError(404, "not_found", f"no contact {contact_id}")
        return {"ok": True}

    @r.get("/groups")
    def list_groups():
        return {"groups": db.list_groups()}

    @r.put("/groups/{group_id}")
    def set_group_rule(group_id: int, body: GroupPatch):
        if not db.get_group(group_id):
            raise ApiError(404, "not_found", f"no group {group_id}")
        if not db.get_rule(body.rule_id):
            raise ApiError(400, "bad_rule", f"no rule {body.rule_id}")
        db.set_group_rule(group_id, body.rule_id)
        return db.get_group(group_id)

    @r.get("/rules")
    def list_rules():
        return {"rules": db.list_rules()}

    @r.put("/rules/{rule_id}")
    def update_rule(rule_id: int, body: RuleIn):
        if not db.get_rule(rule_id):
            raise ApiError(404, "not_found", f"no rule {rule_id}")
        fields = body.model_dump(exclude_unset=True)
        try:
            db.update_rule(rule_id, **fields)
        except sqlite3.IntegrityError:
            raise ApiError(409, "duplicate", "another rule has this name")
        return db.get_rule(rule_id)

    # ---- settings and checks -------------------------------------------------
    @r.get("/settings")
    def get_settings():
        return s.settings.all()

    @r.put("/settings")
    def put_settings(body: SettingsIn):
        values = body.model_dump(exclude_unset=True)
        if values.get("owner_number"):
            values["owner_number"] = normalise_number(values["owner_number"])
        result = s.settings.update(values)
        s.engines.phrases.set_owner(result["owner_name"])
        return result

    @r.post("/test/sms")
    async def test_sms(body: TestSmsIn):
        if not s.gateway.connected:
            raise ApiError(503, "esp_offline", "the ESP32 is not connected")
        sms_id = await notifier.send_sms(body.text, normalise_number(body.to) if body.to else None)
        if sms_id is None:
            raise ApiError(400, "no_number", "set the owner number first")
        return {"ok": True, "id": sms_id, "queued_at": int(time.time())}

    return r
