"""Call records from start to finish: rows, recordings, post-call processing."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path

import numpy as np

from .audio import int16_to_float, read_wav_8k, resample, tone, write_wav
from .config import Config
from .db import Database, now
from .langid import choose_language, summary_language
from .notify import Notifier
from .postcall import apply_rule_floor, summarise, translate_turns
from .protocol import SAMPLE_RATE
from .recorder import Recorder, compress_to_opus, sha256_file
from .rules import CallRule, RulesEngine, normalise_number
from .session import Engines, Session
from .settings import Settings
from .tts import MULTI

log = logging.getLogger(__name__)


class CallService:
    def __init__(self, db: Database, settings: Settings, config: Config, engines: Engines,
                 rules: RulesEngine, notifier: Notifier):
        self.db = db
        self.settings = settings
        self.config = config
        self.engines = engines
        self.rules = rules
        self.notifier = notifier
        self.audio_dir = Path(config.storage.audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: set[asyncio.Task] = set()
        self._heard: dict[str, str | None] = {}   # caller name the live call established

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_idle(self) -> None:
        """Waits for background post-call work (used by tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ---- call start ----------------------------------------------------------
    def log_unanswered(self, call_id: str, number: str, rule: CallRule, started_at: int) -> None:
        handled = "rejected" if rule.action == "reject" else "ignored"
        self.db.create_call(
            call_id, number=number, contact_id=rule.contact_id, started_at=started_at,
            ended_at=started_at, handled_by=handled, status="ready", severity="low",
            reason=f"{handled.capitalize()} by rule ({rule.group})",
            summary=f"Call {handled} by the {rule.group} rule; not answered.")
        self.notifier.bus.publish("call_ready", call_id)

    def create_live(self, call_id: str, number: str, rule: CallRule, started_at: int) -> None:
        if self.db.get_call(call_id):
            self.db.update_call(call_id, status="live")
            return
        self.db.create_call(call_id, number=number, contact_id=rule.contact_id,
                            started_at=started_at, handled_by="bot", status="live")

    # ---- bot call end --------------------------------------------------------
    async def session_finished(self, session: Session) -> None:
        call_id = session.call_id
        ended = now()
        answered = int(session.answered_at) if session.answered_at else None
        langs = [t.language for t in session.turns if t.speaker == "caller"]
        self.db.update_call(
            call_id, answered_at=answered, ended_at=ended,
            duration_s=(ended - answered) if answered else 0,
            end_reason=session.end_reason or "pc_hangup", status="processing",
            language=summary_language(langs) if langs else session.language)
        for seq, t in enumerate(session.turns, start=1):
            self.db.add_turn(call_id, seq, t.speaker, t.start_ms, t.end_ms, t.text,
                             language=t.language, confidence=t.confidence, timing=t.timing)
        if answered:
            path, digest = await asyncio.to_thread(self._save_recording, call_id,
                                                   session.recorder)
            self.db.update_call(call_id, audio_path=str(path), audio_sha256=digest)
        taker = getattr(session, "slots", None)
        if taker is not None:
            self._heard[call_id] = taker.name
        self._spawn(self.process(call_id, session.rule))

    def _save_recording(self, call_id: str, recorder: Recorder) -> tuple[Path, str]:
        return self._save_stereo(call_id, recorder.stereo())

    def _save_stereo(self, call_id: str, stereo: np.ndarray) -> tuple[Path, str]:
        wav = self.audio_dir / f"{call_id}.wav"
        write_wav(wav, stereo.reshape(-1), SAMPLE_RATE, channels=2)
        final = compress_to_opus(wav)
        return final, sha256_file(final)

    async def process(self, call_id: str, rule: CallRule | None = None) -> None:
        """Summary, reason, severity, commitments, translations; then notify."""
        try:
            call = self.db.get_call(call_id)
            if not call:
                return
            if rule is None:
                rule = self.rules.lookup(call["number"])
            turns = self.db.get_turns(call_id)
            context = (f"number {call['number'] or 'withheld'}; "
                       f"saved contact: {rule.contact_name or 'not in contacts'}; "
                       f"group: {rule.group}")
            result = await summarise(self.engines.llm, turns, self.settings.owner_name, context)
            result = apply_rule_floor(result, rule.severity_floor)
            name = self._heard.pop(call_id, None)
            if not result.caller_name and name and "summary_failed" not in result.flags:
                result.caller_name = name         # the call itself established it

            flags = list(dict.fromkeys(result.flags))
            confs = [t["confidence"] for t in turns
                     if t["speaker"] == "caller" and t["confidence"] is not None]
            if confs and min(confs) < 0.4 and "low_transcript_confidence" not in flags:
                flags.append("low_transcript_confidence")

            for seq, text_en in (await translate_turns(self.engines.llm, turns)).items():
                self.db.set_turn_translation(call_id, seq, text_en)

            caller_langs = [t["language"] for t in turns if t["speaker"] == "caller"]
            self.db.set_commitments(call_id, result.bot_commitments)
            self.db.update_call(
                call_id, summary=result.summary, reason=result.reason, severity=result.severity,
                severity_why=result.severity_why, caller_name=result.caller_name,
                action_needed=result.action_needed, deadline=result.deadline,
                callback_number=result.callback_number, flags=flags,
                language=summary_language(caller_langs) if caller_langs else result.language,
                status="ready")
            await self.notifier.record_ready(call_id)
        except Exception:
            log.exception("[%s] post-call processing failed", call_id)
            self.db.update_call(call_id, status="failed")
            self.notifier.bus.publish("call_ready", call_id)

    # ---- voicemail -----------------------------------------------------------
    async def voicemail_upload(self, body: bytes, caller_raw: str, start: int,
                               seconds: int) -> dict:
        number = normalise_number(caller_raw)
        existing = self.db.find_call(number, start, "voicemail")
        digest = hashlib.sha256(body).hexdigest()
        if existing:  # the ESP32 retried; the file is already here
            return {"ok": True, "sha256": digest, "call_id": existing["id"]}
        call_id = "vm-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(start))
        if self.db.get_call(call_id):
            call_id += "-" + (number[-4:] or "x")
        raw = self.audio_dir / f"{call_id}-upload.wav"
        raw.write_bytes(body)
        rule = self.rules.lookup(number)
        self.db.create_call(call_id, number=number, contact_id=rule.contact_id,
                            started_at=start, answered_at=start, ended_at=start + seconds,
                            duration_s=seconds, handled_by="voicemail", status="processing",
                            end_reason="caller_hangup")
        self.notifier.bus.publish("voicemail_received", call_id)
        self._spawn(self.process_voicemail(call_id, raw, rule))
        return {"ok": True, "sha256": digest, "call_id": call_id}

    async def process_voicemail(self, call_id: str, raw: Path, rule: CallRule) -> None:
        try:
            caller = await asyncio.to_thread(read_wav_8k, raw)
            phrases = self.engines.phrases
            greeting = np.concatenate([phrases.get("voicemail_greeting", MULTI),
                                       np.zeros(int(0.2 * SAMPLE_RATE), dtype="<i2"),
                                       tone(1000, 400)])
            # Bot channel: the greeting and beep; caller channel: the message after it.
            stereo = np.zeros((len(greeting) + len(caller), 2), dtype="<i2")
            stereo[:len(greeting), 1] = greeting
            stereo[len(greeting):, 0] = caller
            greet_ms = len(greeting) * 1000 // SAMPLE_RATE
            end_ms = greet_ms + len(caller) * 1000 // SAMPLE_RATE

            audio16 = resample(int16_to_float(caller), SAMPLE_RATE, 16000)
            stt = self.engines.stt
            probs = await asyncio.to_thread(stt.detect_language, audio16) if len(audio16) else None
            choice = choose_language(probs, self.config.call.languages, self.config.call.default_language,
                                     rule.language_hint, 0)
            transcript = await asyncio.to_thread(stt.transcribe, audio16, choice.language) \
                if len(audio16) else None
            self.db.add_turn(call_id, 1, "bot", 0, greet_ms,
                             phrases.text("voicemail_greeting", "en"), language="en")
            self.db.add_turn(call_id, 2, "caller", greet_ms, end_ms,
                             transcript.text if transcript else "", language=choice.language,
                             confidence=transcript.confidence if transcript else None)

            path, digest = await asyncio.to_thread(self._save_stereo, call_id, stereo)
            raw.unlink(missing_ok=True)
            self.db.update_call(call_id, audio_path=str(path), audio_sha256=digest)
        except Exception:
            log.exception("[%s] voicemail processing failed", call_id)
            self.db.update_call(call_id, audio_path=str(raw), status="failed")
            return
        await self.process(call_id, rule)

    # ---- housekeeping --------------------------------------------------------
    def delete_call(self, call_id: str) -> bool:
        call = self.db.delete_call(call_id)
        if not call:
            return False
        if call.get("audio_path"):
            Path(call["audio_path"]).unlink(missing_ok=True)
        return True

    def apply_retention(self) -> int:
        """Deletes audio older than the retention setting; the text stays."""
        cutoff = now() - self.settings.retention_days * 86400
        count = 0
        for row in self.db.calls_with_audio_before(cutoff):
            Path(row["audio_path"]).unlink(missing_ok=True)
            self.db.update_call(row["id"], audio_path=None)
            count += 1
        if count:
            log.info("retention: removed audio of %d calls", count)
        return count

    def recover(self) -> None:
        """After a restart: calls left live are failed; unfinished ones are reprocessed."""
        for call in self.db.list_calls(limit=500):
            if call["status"] == "live":
                self.db.update_call(call["id"], status="failed", end_reason="link_lost")
            elif call["status"] == "processing" and call["handled_by"] == "bot":
                self._spawn(self.process(call["id"]))
