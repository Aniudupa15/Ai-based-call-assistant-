"""The ESP32 link: one WebSocket for control messages and call audio, plus the
voicemail upload endpoint. See docs/protocol.md."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import protocol as P
from .calls import CallService
from .config import Config
from .notify import Notifier
from .rules import RulesEngine, normalise_number
from .session import Engines, Session
from .settings import Settings

log = logging.getLogger(__name__)


class EspLink:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._lock = asyncio.Lock()

    async def send_json(self, msg: dict) -> None:
        async with self._lock:
            await self.ws.send_text(json.dumps(msg, ensure_ascii=False))

    async def send_audio(self, frame: bytes) -> None:
        async with self._lock:
            await self.ws.send_bytes(frame)


class Gateway:
    def __init__(self, config: Config, settings: Settings, rules: RulesEngine,
                 calls: CallService, notifier: Notifier, engines: Engines):
        self.config = config
        self.settings = settings
        self.rules = rules
        self.calls = calls
        self.notifier = notifier
        self.engines = engines
        self.link: EspLink | None = None
        self.device: dict = {}
        self.connected_since: int | None = None
        self.session: Session | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def connected(self) -> bool:
        return self.link is not None

    def router(self) -> APIRouter:
        router = APIRouter()
        router.add_api_websocket_route("/esp", self.handle_ws)
        router.add_api_route("/esp/voicemail", self.voicemail, methods=["POST"])
        return router

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- WebSocket -----------------------------------------------------------
    async def handle_ws(self, ws: WebSocket) -> None:
        await ws.accept()
        if ws.headers.get("x-token") != self.config.network.esp_token:
            log.warning("ESP32 connection without a valid token from %s", ws.client)
            await ws.close(code=P.CLOSE_UNAUTHORISED)
            return
        if self.link is not None:
            log.info("new ESP32 connection replaces the old one")
            try:
                await self.link.ws.close(code=P.CLOSE_REPLACED)
            except Exception:
                pass
        link = EspLink(ws)
        self.link = link
        self.connected_since = int(time.time())
        self.notifier.sms_sender = self._send_sms
        log.info("ESP32 connected from %s", ws.client)
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    self._on_audio(msg["bytes"])
                elif msg.get("text") is not None:
                    try:
                        data = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        log.warning("bad JSON from ESP32: %r", msg["text"][:200])
                        continue
                    await self._on_message(link, data)
        except WebSocketDisconnect:
            pass
        except RuntimeError as e:  # receive after close
            log.debug("websocket closed: %s", e)
        finally:
            if self.link is link:
                self.link = None
                self.connected_since = None
                self.notifier.sms_sender = None
                log.warning("ESP32 link down")
                if self.session is not None and not self.session.finished:
                    session, self.session = self.session, None
                    self._spawn(session.on_call_end("link_lost"))

    def _on_audio(self, frame: bytes) -> None:
        if self.session is not None and len(frame) == P.FRAME_BYTES:
            self.session.feed_caller(frame)

    async def _on_message(self, link: EspLink, msg: dict) -> None:
        kind = msg.get("type")
        if kind == P.HELLO:
            await self._on_hello(link, msg)
        elif kind == P.CALL_START:
            await self._on_call_start(link, msg)
        elif kind == P.CALL_ANSWERED:
            s = self.session
            if s is not None and s.call_id == msg.get("call_id") and s.answered_at is None:
                s.start()
        elif kind == P.CALL_END:
            s = self.session
            if s is not None and s.call_id == msg.get("call_id"):
                self.session = None
                reason = msg.get("reason") if msg.get("reason") in P.END_REASONS else "pc_hangup"
                self._spawn(s.on_call_end(reason))
        elif kind == P.HEALTH:
            self.notifier.health(bool(msg.get("registered")), int(msg.get("rssi", 99)),
                                 int(msg.get("sd_free_mb", 0)), bool(msg.get("on_battery")))
        elif kind == P.SMS_RESULT:
            try:
                self.notifier.sms_result(int(msg["id"]), bool(msg.get("ok")))
            except (KeyError, ValueError):
                log.warning("bad sms_result: %s", msg)
        else:
            log.warning("unknown message from ESP32: %s", msg)

    async def _on_hello(self, link: EspLink, msg: dict) -> None:
        proto = str(msg.get("proto") or msg.get("fw_version") or "")
        if P.major(proto) != P.major(P.PROTOCOL_VERSION):
            detail = (f"firmware protocol {proto!r} does not match server "
                      f"{P.PROTOCOL_VERSION}; reflash the firmware")
            log.error(detail)
            await link.send_json({"type": P.ERROR, "code": "version", "detail": detail})
            await link.ws.close(code=P.CLOSE_VERSION)
            return
        self.device = {k: msg.get(k) for k in ("device_id", "fw_version", "proto", "vm_pending")}
        await link.send_json({"type": P.TIME, "epoch": int(time.time())})
        await self.notifier.flush_sms()

    async def _on_call_start(self, link: EspLink, msg: dict) -> None:
        call_id = str(msg.get("call_id") or f"c-{time.strftime('%Y%m%d-%H%M%S')}")
        number = normalise_number(msg.get("number"))
        started = int(msg.get("ts") or time.time())
        if self.session is not None and not self.session.finished:
            # One SIM means one call; a stale session is closed out.
            old, self.session = self.session, None
            self._spawn(old.on_call_end("link_lost"))
        rule = self.rules.lookup(number)
        log.info("[%s] call from %s -> %s (%s)", call_id, number or "withheld",
                 rule.action, rule.group)
        if rule.action == "answer":
            self.calls.create_live(call_id, number, rule, started)
            self.session = Session(call_id, number, rule, link, self.engines, self.config,
                                   self.settings.owner_name, self.calls.session_finished)
        else:
            self.calls.log_unanswered(call_id, number, rule, started)
        await link.send_json({"type": P.DECISION, "call_id": call_id, "action": rule.action,
                              "max_seconds": rule.max_seconds})

    async def _send_sms(self, sms_id: int, to: str, text: str) -> bool:
        link = self.link
        if link is None:
            return False
        try:
            await link.send_json({"type": P.SEND_SMS, "id": sms_id, "to": to,
                                  "text": text[:P.SMS_MAX_CHARS]})
            return True
        except Exception as e:
            log.warning("could not hand SMS to ESP32: %s", e)
            return False

    # ---- voicemail upload ----------------------------------------------------
    async def voicemail(self, request: Request) -> JSONResponse:
        if request.headers.get("x-token") != self.config.network.esp_token:
            return JSONResponse({"error": "unauthorised", "detail": "bad X-Token"}, 401)
        try:
            start = int(request.headers.get("x-start", "0"))
            seconds = int(request.headers.get("x-seconds", "0"))
        except ValueError:
            return JSONResponse({"error": "bad_request", "detail": "bad X-Start/X-Seconds"}, 400)
        body = await request.body()
        if len(body) < 44 or body[:4] != b"RIFF" or body[8:12] != b"WAVE":
            return JSONResponse({"error": "bad_request", "detail": "body is not a WAV file"}, 400)
        result = await self.calls.voicemail_upload(body, request.headers.get("x-caller", ""),
                                                   start or int(time.time()), seconds)
        return JSONResponse(result)
