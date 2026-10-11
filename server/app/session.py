"""One live call: the conversation loop from the greeting to the hang-up.

Caller frames arrive from the gateway every 20 ms. Voice activity detection marks
turns; each finished turn is transcribed, answered by the language model, spoken
sentence by sentence, and paced back to the ESP32. If the caller talks over the
bot, playback stops at once (`clear_audio`) and the loop restarts on the new turn.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

import numpy as np

from . import protocol as P
from .audio import SILENCE_FRAME, bytes_to_int16, frames, int16_to_float, resample
from .config import Config
from .langid import ASK_LANGUAGE_PHRASE, LANGUAGE_NAMES, choose_language
from .llm import SentenceSplitter, strip_end_token
from .recorder import Recorder
from .rules import CallRule
from .stt import SpeechToText
from .tts import MULTI, PROMPTS_DIR, PhraseCache, TextToSpeech
from .vad import TurnDetector, VadModel

log = logging.getLogger(__name__)

PRE_ROLL_FRAMES = 15        # 300 ms kept before speech start so words are not clipped
HANGUP_WAIT_S = 5.0         # how long to wait for call_end after sending hangup
MIN_CONFIDENCE = 0.25       # below this a transcript counts as not understood


class Link(Protocol):
    async def send_json(self, msg: dict) -> None: ...
    async def send_audio(self, frame: bytes) -> None: ...


@dataclass
class Engines:
    stt: SpeechToText
    llm: object                      # LanguageModel
    tts: TextToSpeech
    phrases: PhraseCache
    vad_factory: Callable[[], VadModel]
    loaded: list[str] = field(default_factory=list)


@dataclass
class Segment:
    """A piece of bot audio; its position in the recording is set when it plays."""
    frames: list[bytes]
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    sent: int = 0


@dataclass
class Turn:
    speaker: str
    text: str
    start_ms: int
    end_ms: int
    language: Optional[str] = None
    confidence: Optional[float] = None
    timing: Optional[dict] = None
    parts: list[tuple[str, Segment]] = field(default_factory=list)  # bot sentences

    def heard(self) -> str:
        """The bot's words that started playing; a barge-in cuts off the rest."""
        return " ".join(text for text, seg in self.parts if seg.sent > 0)


@dataclass
class _Event:
    kind: str                         # turn | silence | time_limit | reply_done | ended
    audio: bytes = b""
    start_ms: int = 0
    end_ms: int = 0
    end_call: bool = False
    detected_at: float = 0.0


class Player:
    """Sends bot audio no faster than real time plus 200 ms.

    In real-time mode it sends a frame every 20 ms for the whole call, silence when
    the bot is quiet, so the ESP32 can tell a live link from a dead one.
    """

    LEAD_S = 0.2

    def __init__(self, link: Link, recorder: Recorder, realtime: bool):
        self.link = link
        self.recorder = recorder
        self.realtime = realtime
        self._queue: deque[tuple[Segment, int]] = deque()
        self._wake = asyncio.Event()
        self._audio_until = 0.0
        self._task: asyncio.Task | None = None
        self._closed = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="player")

    async def stop(self) -> None:
        self._closed = True
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def speaking(self) -> bool:
        return bool(self._queue) or time.monotonic() < self._audio_until

    def enqueue(self, pcm: np.ndarray) -> Segment:
        seg = Segment(frames(np.asarray(pcm, dtype="<i2").tobytes()))
        for i in range(len(seg.frames)):
            self._queue.append((seg, i))
        self._wake.set()
        return seg

    def clear(self) -> None:
        self._queue.clear()
        self._audio_until = 0.0

    async def drain(self) -> None:
        while self.speaking and not self._closed:
            await asyncio.sleep(0.02)

    async def _send(self, seg: Segment, idx: int) -> None:
        frame = seg.frames[idx]
        pos = self.recorder.add_bot(frame)
        if seg.start_ms is None:
            seg.start_ms = pos
        seg.end_ms = pos + P.FRAME_MS
        seg.sent += 1
        await self.link.send_audio(frame)

    async def _run(self) -> None:
        if not self.realtime:
            while not self._closed:
                while self._queue:
                    seg, idx = self._queue.popleft()
                    await self._send(seg, idx)
                self._wake.clear()
                await self._wake.wait()
            return
        clock = time.monotonic()
        while not self._closed:
            now = time.monotonic()
            if clock < now:
                clock = now
            if self._queue:
                if clock - now > self.LEAD_S:
                    await asyncio.sleep(clock - now - self.LEAD_S)
                    continue
                seg, idx = self._queue.popleft()
                await self._send(seg, idx)
                clock += P.FRAME_MS / 1000
                self._audio_until = clock
            else:
                if clock > now:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), clock - now)
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self.link.send_audio(SILENCE_FRAME)
                clock += P.FRAME_MS / 1000


class Session:
    def __init__(self, call_id: str, number: str, rule: CallRule, link: Link,
                 engines: Engines, config: Config, owner_name: str,
                 on_finished: Callable[["Session"], Awaitable[None]]):
        self.call_id = call_id
        self.number = number
        self.rule = rule
        self.link = link
        self.engines = engines
        self.config = config
        self.owner_name = owner_name
        self.on_finished = on_finished

        cc = config.call
        self.allowed = [l for l in cc.languages if l in LANGUAGE_NAMES] or ["en"]
        self.language = rule.language_hint if rule.language_hint in self.allowed \
            else cc.default_language
        self.max_seconds = min(rule.max_seconds or cc.max_seconds, cc.max_seconds)

        self.recorder = Recorder()
        self.player = Player(link, self.recorder, config.realtime)
        self.vad = engines.vad_factory()
        self.detector = TurnDetector(cc.speech_start_ms, cc.end_of_turn_ms, cc.barge_in_ms)
        self.turns: list[Turn] = []
        self.history: list[dict] = []
        self.events: asyncio.Queue[_Event] = asyncio.Queue()

        self.answered_at: float | None = None
        self.end_reason: str | None = None
        self.finished = False

        self._pre_roll: deque[bytes] = deque(maxlen=PRE_ROLL_FRAMES)
        self._collecting = False
        self._turn_buf = bytearray()
        self._turn_start_ms = 0
        self._was_speaking = False
        self._silence_reported = False
        self._time_limit_sent = False
        self._silences = 0
        self._misunderstood = 0
        self._asked_language = False
        self._caller_turns = 0
        self._pending_turns = 0
        self._reply_task: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None
        self._call_ended = asyncio.Event()
        self._finish_lock = asyncio.Lock()

    # ---- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        """Called on call_answered: the recorder starts and the greeting plays."""
        self.answered_at = time.time()
        self.player.start()
        self._run_task = asyncio.create_task(self._run(), name=f"session-{self.call_id}")

    async def on_call_end(self, reason: str) -> None:
        """call_end from the ESP32, or the link dropped."""
        # If the PC hung up first (goodbye, time limit) its own reason is kept.
        if self.end_reason is None:
            self.end_reason = reason
        self._call_ended.set()
        task = self._run_task
        if task is not None and not task.done():
            if reason != "pc_hangup":
                task.cancel()  # the caller is gone; nothing more to say
            try:
                await asyncio.wait_for(asyncio.shield(task), HANGUP_WAIT_S + 1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        await self._finish()

    # ---- caller audio --------------------------------------------------------
    def feed_caller(self, frame: bytes) -> None:
        if len(frame) != P.FRAME_BYTES or self.finished or self.answered_at is None:
            return
        self.recorder.add_caller(frame)
        speaking = self.player.speaking
        if self._was_speaking and not speaking:
            self.detector.reset_silence()
            self._silence_reported = False
        self._was_speaking = speaking

        samples = bytes_to_int16(frame)
        event = self.detector.feed(self.vad.is_speech(samples, speaking), speaking)

        if self._collecting:
            self._turn_buf += frame
        else:
            self._pre_roll.append(frame)

        if event is not None:
            if event.kind in ("speech_start", "barge_in"):
                if event.kind == "barge_in":
                    self._interrupt()
                self._collecting = True
                self._silence_reported = False
                # The pre-roll ends with the current frame, so it starts this far back.
                self._turn_buf = bytearray(b"".join(self._pre_roll))
                self._turn_start_ms = max(0, (event.frame - len(self._pre_roll) + 1)
                                          * P.FRAME_MS)
                self._pre_roll.clear()
            elif event.kind == "end_of_turn" and self._collecting:
                self._collecting = False
                self._pending_turns += 1
                self.events.put_nowait(_Event(
                    "turn", audio=bytes(self._turn_buf), start_ms=self._turn_start_ms,
                    end_ms=(event.frame + 1) * P.FRAME_MS, detected_at=time.monotonic()))
                self._turn_buf = bytearray()

        busy = self._collecting or speaking or (self._reply_task and not self._reply_task.done())
        silence_frames = self.config.call.silence_prompt_s * 1000 // P.FRAME_MS
        if not busy and not self._silence_reported and \
                self.detector.silent_frames() >= silence_frames:
            self._silence_reported = True
            self.events.put_nowait(_Event("silence"))

        if not self._time_limit_sent and self.recorder.position_ms >= self.max_seconds * 1000:
            self._time_limit_sent = True
            self.events.put_nowait(_Event("time_limit"))

    def _interrupt(self) -> None:
        """Barge-in: stop playback now and drop the reply being generated."""
        if self.player.speaking:
            self.player.clear()
            asyncio.create_task(self._send({"type": P.CLEAR_AUDIO, "call_id": self.call_id}))
            self._trim_last_bot_turn()
        if self._reply_task and not self._reply_task.done():
            self._reply_task.cancel()

    def _trim_last_bot_turn(self) -> None:
        """Keeps the transcript and the model's history to what the caller heard."""
        bot = [t for t in self.turns if t.speaker == "bot" and t.parts]
        if not bot:
            return
        turn = bot[-1]
        heard = turn.heard()
        if heard == turn.text:
            return
        for msg in reversed(self.history):
            if msg["role"] == "assistant" and msg["content"] == turn.text:
                msg["content"] = heard or "(interrupted)"
                break
        turn.text = heard

    # ---- conversation --------------------------------------------------------
    async def _run(self) -> None:
        try:
            greeting_lang = self.language if self.rule.language_hint in self.allowed else MULTI
            greeting_key = "greeting" if greeting_lang != MULTI else "greeting_short"
            await self._say_phrase(greeting_key, greeting_lang)
            self.history.append({"role": "assistant",
                                 "content": self.engines.phrases.text(greeting_key, greeting_lang)})
            while True:
                ev = await self.events.get()
                if ev.kind == "ended":
                    break
                if ev.kind == "time_limit":
                    await self._cancel_reply()
                    self.end_reason = "time_limit"
                    await self._say_phrase("wrap_up", self.language)
                    await self._hangup()
                    break
                if ev.kind == "silence":
                    if self._reply_task and not self._reply_task.done():
                        continue
                    self._silences += 1
                    if self._silences >= 2:
                        await self._say_phrase("goodbye", self.language)
                        await self._hangup()
                        break
                    await self._say_phrase("still_there", self.language)
                    continue
                if ev.kind == "turn":
                    self._pending_turns -= 1
                    self._silences = 0
                    await self._cancel_reply()
                    # Transcription is never cancelled, so no caller words are lost;
                    # only the reply is.
                    heard = await self._transcribe(ev)
                    if self._pending_turns > 0:
                        continue  # the caller kept talking: answer once, with everything
                    self._reply_task = asyncio.create_task(self._respond(*heard))
                    continue
                if ev.kind == "reply_done" and ev.end_call:
                    await self._hangup()
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("[%s] conversation loop failed", self.call_id)
            await self._hangup()
        finally:
            await self._cancel_reply()
            await self._finish()

    async def _transcribe(self, ev: _Event) -> tuple:
        t_eos = ev.detected_at
        timing = {"eos_wait_ms": self.config.call.end_of_turn_ms}
        audio16 = resample(int16_to_float(bytes_to_int16(ev.audio)), P.SAMPLE_RATE, 16000)
        stt = self.engines.stt

        t0 = time.monotonic()
        probs = None
        if not (self._caller_turns == 0 and self.rule.language_hint in self.allowed):
            probs = await asyncio.to_thread(stt.detect_language, audio16)
        choice = choose_language(probs, self.allowed, self.language, self.rule.language_hint,
                                 self._caller_turns)
        transcript = await asyncio.to_thread(stt.transcribe, audio16, choice.language)
        timing["stt_ms"] = int((time.monotonic() - t0) * 1000)
        self._caller_turns += 1
        self.language = choice.language

        text = transcript.text.strip()
        conf = transcript.confidence
        self.turns.append(Turn("caller", text, ev.start_ms, ev.end_ms, choice.language, conf))
        understood = bool(text) and (conf is None or conf >= MIN_CONFIDENCE)
        if understood:
            self.history.append({"role": "user", "content": text})
        return understood, choice.confident, t_eos, timing

    async def _respond(self, understood: bool, confident: bool, t_eos: float,
                       timing: dict) -> None:
        if not understood:
            self._misunderstood += 1
            if self._misunderstood >= 2:
                await self._say_phrase("send_text", self.language)
                self.events.put_nowait(_Event("reply_done", end_call=True))
            else:
                await self._say_phrase("repeat", self.language)
            return
        self._misunderstood = 0

        if not confident and not self._asked_language and \
                self.rule.language_hint not in self.allowed:
            self._asked_language = True
            await self._say_phrase(ASK_LANGUAGE_PHRASE, MULTI)
            return

        await self._reply(t_eos, timing)

    async def _reply(self, t_eos: float, timing: dict) -> None:
        messages = [{"role": "system", "content": self._system_prompt()}, *self.history]
        splitter = SentenceSplitter()
        spoken: list[str] = []
        turn = Turn("bot", "", 0, 0, self.language, timing=timing)
        ended = False
        filler_played = False
        t_llm = time.monotonic()
        first_token: float | None = None
        max_sentences = self.config.call.max_reply_sentences

        async def speak(sentence: str) -> None:
            nonlocal ended
            sentence, end = strip_end_token(sentence)
            ended = ended or end
            if not sentence or len(spoken) >= max_sentences:
                return
            audio = await asyncio.to_thread(self.engines.tts.synthesize, sentence, self.language)
            if "first_audio_ms" not in timing:
                timing["first_audio_ms"] = int((time.monotonic() - t_eos) * 1000)
            turn.parts.append((sentence, self.player.enqueue(audio)))
            spoken.append(sentence)

        stream = self.engines.llm.stream_chat(messages)
        it = stream.__aiter__()
        filler_s = self.config.call.filler_after_ms / 1000
        try:
            while True:
                wait = None
                if not spoken and not filler_played:
                    # Only a slow language model earns the filler, not slow transcription.
                    wait = max(0.0, filler_s - (time.monotonic() - t_llm))
                try:
                    chunk = await asyncio.wait_for(it.__anext__(), wait) if wait is not None \
                        else await it.__anext__()
                except asyncio.TimeoutError:
                    # The model is slow: a short filler covers the gap; logged for tuning.
                    filler_played = True
                    timing["slow"] = True
                    self.player.enqueue(self.engines.phrases.get("one_moment", self.language))
                    continue
                except StopAsyncIteration:
                    break
                if first_token is None:
                    first_token = time.monotonic()
                    timing["first_token_ms"] = int((first_token - t_llm) * 1000)
                for sentence in splitter.feed(chunk):
                    await speak(sentence)
            rest = splitter.flush()
            if rest:
                await speak(rest)
        except asyncio.CancelledError:
            # Barge-in: keep only what the caller actually heard.
            spoken[:] = [text for text, seg in turn.parts if seg.sent > 0]
            raise
        except Exception:
            log.exception("[%s] reply generation failed", self.call_id)
        finally:
            if hasattr(stream, "aclose"):
                try:
                    await stream.aclose()
                except Exception:
                    pass
            timing["total_ms"] = int((time.monotonic() - t_eos) * 1000)
            if spoken:
                turn.text = " ".join(spoken)
                self.turns.append(turn)
                self.history.append({"role": "assistant", "content": turn.text})
            log.info("[%s] turn timing %s", self.call_id, timing)

        if ended:
            await self.player.drain()
        self.events.put_nowait(_Event("reply_done", end_call=ended))

    def _system_prompt(self) -> str:
        template = (PROMPTS_DIR / "system.txt").read_text(encoding="utf-8")
        return template.format(owner_name=self.owner_name,
                               language=LANGUAGE_NAMES.get(self.language, "English"),
                               rule_specific_lines=self.rule.extra_prompt or "")

    async def _say_phrase(self, key: str, language: str) -> None:
        audio = self.engines.phrases.get(key, language)
        seg = self.player.enqueue(audio)
        self.turns.append(Turn("bot", self.engines.phrases.text(key, language), 0, 0,
                               language if language != MULTI else "en",
                               parts=[(self.engines.phrases.text(key, language), seg)]))
        await self.player.drain()

    async def _cancel_reply(self) -> None:
        task = self._reply_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _hangup(self) -> None:
        await self.player.drain()
        if self.end_reason is None:
            self.end_reason = "pc_hangup"
        await self._send({"type": P.HANGUP, "call_id": self.call_id})
        try:
            await asyncio.wait_for(self._call_ended.wait(), HANGUP_WAIT_S)
        except asyncio.TimeoutError:
            log.warning("[%s] no call_end after hangup", self.call_id)

    async def _send(self, msg: dict) -> None:
        try:
            await self.link.send_json(msg)
        except Exception as e:
            log.warning("[%s] send failed: %s", self.call_id, e)

    async def _finish(self) -> None:
        async with self._finish_lock:
            if self.finished:
                return
            self.finished = True
        await self.player.stop()
        # Bot turns get their position from the audio that actually played.
        for t in self.turns:
            if t.speaker == "bot" and t.parts:
                t.text = t.heard()
                played = [seg for _, seg in t.parts if seg.start_ms is not None]
                if played:
                    t.start_ms = played[0].start_ms or 0
                    t.end_ms = played[-1].end_ms or t.start_ms
        self.turns = [t for t in self.turns
                      if t.speaker == "caller" or t.text]
        self.turns.sort(key=lambda t: (t.start_ms, 0 if t.speaker == "bot" else 1))
        await self.on_finished(self)
