"""One live call: the conversation loop from the greeting to the hang-up.

Caller frames arrive from the gateway every 20 ms. Voice activity detection marks
turns; each finished turn is transcribed, answered by the language model, spoken
sentence by sentence, and paced back to the ESP32. If the caller talks over the
bot, playback stops at once (`clear_audio`) and the loop restarts on the new turn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

import numpy as np

from . import protocol as P
from .audio import SILENCE_FRAME, bytes_to_int16, frames, int16_to_float, resample
from .config import Config
from .heard import clean_reply, is_echo, is_noise, language_request
from .message import MessageTaker, build_prompt, english_line, fallback_slots, parse_slots
from .langid import ASK_LANGUAGE_PHRASE, LANGUAGE_NAMES, choose_language
from .llm import END_TOKEN, SentenceSplitter, strip_end_token
from .recorder import Recorder
from .rules import CallRule
from .stt import SpeechToText
from .tts import MULTI, PROMPTS_DIR, PhraseCache, TextToSpeech
from .vad import TurnDetector, VadModel

log = logging.getLogger(__name__)

PRE_ROLL_FRAMES = 15        # 300 ms kept before speech start so words are not clipped
HANGUP_WAIT_S = 5.0         # how long to wait for call_end after sending hangup
MIN_CONFIDENCE = 0.25       # below this a transcript counts as not understood
UNSURE_CONFIDENCE = 0.5     # below this, with the language unsure, ask for the language
ECHO_TURNS_NO_BARGE_IN = 2  # after this many echo turns, the caller cannot interrupt
ECHO_TAIL_FRAMES = 20       # the bot's voice can come back 400 ms after it stops
MISUNDERSTOOD_LIMIT = 3     # "could you repeat" twice, then "please send a text"
SLOT_TIMEOUT_S = 8.0        # longer than this for the message check: simple rules instead
LONG_SPEECH_MS = 1500       # an empty transcript of this much speech is not noise
FINAL_STT_WAIT_S = 30.0     # after a hang-up, time allowed to transcribe the last words


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
class Heard:
    """One transcribed caller turn and what to make of it."""
    text: str
    kind: str                         # words | unclear | noise | echo
    language: str
    confident: bool
    confidence: Optional[float]
    t_eos: float
    timing: dict
    chosen_language: Optional[str] = None   # "Kannada" said in answer to the question


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

    While the caller talks the player is paused: queued bot audio is held, not
    dropped, so a cough or a "hmm" does not cost the caller the bot's reply.
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
        self.paused = False

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
        """Bot audio is reaching the caller now (or within the next 200 ms)."""
        if self.paused:
            return False
        return bool(self._queue) or time.monotonic() < self._audio_until

    @property
    def queued(self) -> bool:
        return bool(self._queue)

    @property
    def pending(self) -> bool:
        """Audio is playing or waiting, held or not."""
        return bool(self._queue) or self.speaking

    def enqueue(self, pcm: np.ndarray) -> Segment:
        seg = Segment(frames(np.asarray(pcm, dtype="<i2").tobytes()))
        for i in range(len(seg.frames)):
            self._queue.append((seg, i))
        self._wake.set()
        return seg

    def pause(self) -> bool:
        """Holds the queue. Returns True if audio was reaching the caller."""
        audible = self.speaking
        self.paused = True
        self._audio_until = 0.0
        return audible

    def resume(self) -> None:
        if self.paused:
            self.paused = False
            self._wake.set()

    def clear(self) -> None:
        self._queue.clear()
        self._audio_until = 0.0
        self.paused = False

    async def drain(self, held_too: bool = False) -> None:
        """Waits until the queue has played. held_too: also wait out a pause."""
        while not self._closed and (self.speaking or (held_too and self._queue)):
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
                while self._queue and not self.paused:
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
            if self._queue and not self.paused:
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
        self.hint = rule.language_hint if rule.language_hint in self.allowed else None
        self.language = self.hint or cc.default_language
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
        self._echo_tail = 0                   # caller frames left in the echo tail
        self._silence_reported = False
        self._time_limit_sent = False
        self._silences = 0
        self._misunderstood = 0
        self._asked_language = False
        self._language_locked = self.hint is not None
        self._caller_turns = 0
        self._pending_turns = 0
        self._transcribing = False
        self._deferred: Heard | None = None   # a reply waiting for the caller to finish
        self._filler_task: asyncio.Task | None = None
        self._last_bot_audio_at = 0.0         # when bot audio was last queued
        self._current_timing: dict | None = None
        self._ended = False                   # the caller has gone
        self._hangup_due = False              # the bot said goodbye; a noise turn came in
        self._echo_turns = 0
        self.message = MessageTaker()         # what the caller has told us so far
        self.slots: MessageTaker | None = None
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
                # The caller is gone: nothing more to say, but what they said last is
                # still transcribed so the message is not lost.
                self._ended = True
                self.player.clear()
                if self._collecting and len(self._turn_buf) >= 15 * P.FRAME_BYTES:
                    self._queue_turn(self.detector.frame)
                self._collecting = False
                if self._reply_task and not self._reply_task.done():
                    self._reply_task.cancel()
                self.events.put_nowait(_Event("ended"))
            try:
                await asyncio.wait_for(asyncio.shield(task), FINAL_STT_WAIT_S)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        await self._finish()

    # ---- caller audio --------------------------------------------------------
    def feed_caller(self, frame: bytes) -> None:
        if len(frame) != P.FRAME_BYTES or self.finished or self._ended \
                or self.answered_at is None:
            return
        self.recorder.add_caller(frame)
        # Just after the bot stops, its voice may still be coming back down the line,
        # so the stricter barge-in settings stay on a little longer. Counted in frames,
        # like the rest of turn taking, so a replay behaves the same at any speed.
        if self.player.speaking:
            self._echo_tail = ECHO_TAIL_FRAMES
        elif self._echo_tail:
            self._echo_tail -= 1
        speaking = self.player.speaking or self._echo_tail > 0
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
                self._hold()
                self._collecting = True
                self._silence_reported = False
                # The pre-roll ends with the current frame, so it starts this far back.
                self._turn_buf = bytearray(b"".join(self._pre_roll))
                self._turn_start_ms = max(0, (event.frame - len(self._pre_roll) + 1)
                                          * P.FRAME_MS)
                self._pre_roll.clear()
            elif event.kind == "end_of_turn" and self._collecting:
                self._queue_turn(event.frame + 1)

        # Without real-time pacing (tests, replays) bot audio takes no time to play.
        playing = self.player.pending if self.config.realtime else False
        busy = (self._collecting or self._transcribing or self._pending_turns > 0
                or playing or self._deferred is not None
                or (self._reply_task is not None and not self._reply_task.done()))
        silence_frames = self.config.call.silence_prompt_s * 1000 // P.FRAME_MS
        if busy:
            self.detector.reset_silence()
        elif not self._silence_reported and self.detector.silent_frames() >= silence_frames:
            self._silence_reported = True
            self.events.put_nowait(_Event("silence"))

        if not self._time_limit_sent and self.recorder.position_ms >= self.max_seconds * 1000:
            self._time_limit_sent = True
            self.events.put_nowait(_Event("time_limit"))

    def _queue_turn(self, end_frame: int) -> None:
        self._collecting = False
        self._pending_turns += 1
        self.events.put_nowait(_Event(
            "turn", audio=bytes(self._turn_buf), start_ms=self._turn_start_ms,
            end_ms=max(self._turn_start_ms, end_frame * P.FRAME_MS),
            detected_at=time.monotonic()))
        self._turn_buf = bytearray()

    def _hold(self) -> None:
        """The caller started talking: hold the bot's audio until we know what they said.

        A real interruption then drops the rest of the reply; a cough or a line click
        lets it carry on.
        """
        if self.player.pause():
            asyncio.create_task(self._send({"type": P.CLEAR_AUDIO, "call_id": self.call_id}))

    def _drop_bot_audio(self) -> None:
        """The caller said something real over the bot: what was held is not played.

        Audio queued after the caller stopped (the 'one moment' filler) is not held
        and plays on."""
        if not self.player.paused:
            return
        had_audio = self.player.queued
        self.player.clear()
        if had_audio:
            self._trim_last_bot_turn()

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

    def _recent_bot_lines(self) -> list[str]:
        lines: list[str] = []
        for t in [t for t in self.turns if t.speaker == "bot"][-3:]:
            lines += [text for text, seg in t.parts if seg.sent > 0] or ([t.text] if t.text else [])
        return lines

    # ---- conversation --------------------------------------------------------
    async def _run(self) -> None:
        try:
            greeting_lang = self.language if self.hint else MULTI
            greeting_key = "greeting" if greeting_lang != MULTI else "greeting_short"
            self.history.append({"role": "assistant",
                                 "content": self.engines.phrases.text(greeting_key, greeting_lang)})
            await self._say_phrase(greeting_key, greeting_lang)
            while True:
                ev = await self.events.get()
                if ev.kind == "ended":
                    break
                if ev.kind == "turn":
                    await self._on_turn(ev)
                    continue
                if self._ended:
                    continue
                if ev.kind == "time_limit":
                    await self._cancel_reply()
                    self.player.clear()
                    self.end_reason = "time_limit"
                    await self._say_phrase("wrap_up", self.language)
                    await self._hangup()
                    break
                if ev.kind == "silence":
                    if self._busy():
                        continue
                    self._silences += 1
                    if self._silences >= 2:
                        await self._say_phrase("goodbye", self.language)
                        await self._hangup()
                        break
                    self._silence_reported = False      # count afresh from here
                    self.detector.reset_silence()
                    await self._say_phrase("still_there", self.language)
                    continue
                if ev.kind == "reply_done" and ev.end_call:
                    if self._busy():
                        # The caller may be speaking up again: decide once that turn is
                        # heard. Real words carry on the call; noise or echo do not.
                        self._hangup_due = True
                        continue
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

    def _busy(self) -> bool:
        return (self._collecting or self._pending_turns > 0 or self._transcribing
                or self._deferred is not None
                or (self._reply_task is not None and not self._reply_task.done()))

    async def _on_turn(self, ev: _Event) -> None:
        self._pending_turns -= 1
        self._arm_filler(ev.detected_at)
        self._transcribing = True
        try:
            # Transcription is never cancelled, so no caller words are lost; meanwhile
            # the bot's reply (if any) is held, not dropped.
            heard = await self._transcribe(ev)
        finally:
            self._transcribing = False
        caller_still_talking = self._pending_turns > 0 or self._collecting
        if heard.kind == "echo":
            self._echo_turns += 1
            if self._echo_turns == ECHO_TURNS_NO_BARGE_IN:
                # A line this echoey makes every bot sentence stop and start; letting
                # the bot finish its sentences is the lesser evil.
                log.info("[%s] strong echo on the line: interrupting the bot turned off",
                         self.call_id)
                self.detector.barge_frames = 10 ** 9
        if heard.kind in ("noise", "echo"):
            log.info("[%s] ignored %s turn %r", self.call_id, heard.kind, heard.text)
            self._disarm_filler()
            if self._ended or caller_still_talking:
                return
            self.player.resume()
            if self._hangup_due and not self._reply_running():
                self.events.put_nowait(_Event("reply_done", end_call=True))
                return
            if self._deferred is not None and not self._reply_running():
                self._start_reply(self._deferred)
            return

        # Real words: whatever the bot was saying, or about to say, is dropped.
        self._silences = 0
        self._hangup_due = False
        await self._cancel_reply()
        self._drop_bot_audio()
        self._record_caller(heard, ev)
        if self._ended:
            return
        if caller_still_talking:
            self._deferred = heard     # answer once, with everything they said
            return
        self._start_reply(heard)

    def _arm_filler(self, t_eos: float) -> None:
        """'One moment please' if the caller would otherwise wait too long in silence,
        whether transcription or the language model is the slow part."""
        self._disarm_filler()
        self._filler_task = asyncio.create_task(self._filler_after(t_eos))

    def _disarm_filler(self) -> None:
        if self._filler_task and not self._filler_task.done():
            self._filler_task.cancel()
        self._filler_task = None

    async def _filler_after(self, t_eos: float) -> None:
        delay = self.config.call.filler_after_ms / 1000 - (time.monotonic() - t_eos)
        await asyncio.sleep(max(0.0, delay))
        if (self._ended or self._hangup_due or self._collecting or self.player.speaking
                or self._last_bot_audio_at > t_eos):
            return
        if not (self._transcribing or self._reply_running() or self._deferred is not None):
            return
        if self.player.paused:
            if self.player.queued:
                return        # an interrupted reply is on hold; its fate is decided first
            self.player.resume()
        self._play_filler()
        if self._current_timing is not None:
            self._current_timing["slow"] = True

    def _reply_running(self) -> bool:
        return self._reply_task is not None and not self._reply_task.done()

    def _start_reply(self, heard: Heard) -> None:
        self._deferred = None
        self._reply_task = asyncio.create_task(self._respond(heard))

    def _record_caller(self, heard: Heard, ev: _Event) -> None:
        self.turns.append(Turn("caller", heard.text or "[unclear speech]", ev.start_ms,
                               ev.end_ms, heard.language, heard.confidence))
        if heard.kind != "words":
            return
        if self.history and self.history[-1]["role"] == "user":
            # The caller went on before the bot said anything: one message, not two.
            self.history[-1]["content"] += " " + heard.text
        else:
            self.history.append({"role": "user", "content": heard.text})

    async def _transcribe(self, ev: _Event) -> Heard:
        t_eos = ev.detected_at
        timing = {"eos_wait_ms": self.config.call.end_of_turn_ms}
        audio16 = resample(int16_to_float(bytes_to_int16(ev.audio)), P.SAMPLE_RATE, 16000)
        stt = self.engines.stt

        t0 = time.monotonic()
        probs = None
        if not (self._caller_turns == 0 and self.hint):
            probs = await asyncio.to_thread(stt.detect_language, audio16)
        choice = choose_language(probs, self.allowed, self.language, self.hint,
                                 self._caller_turns, locked=self._language_locked)
        transcript = await asyncio.to_thread(stt.transcribe, audio16, choice.language)
        timing["stt_ms"] = int((time.monotonic() - t0) * 1000)

        text = transcript.text.strip()
        conf = transcript.confidence
        heard = Heard(text, "words", choice.language, choice.confident, conf, t_eos, timing)
        self._current_timing = timing
        # Pre-roll and the end-of-turn wait are silence; the rest was voice.
        voiced_ms = len(ev.audio) // P.FRAME_BYTES * P.FRAME_MS \
            - PRE_ROLL_FRAMES * P.FRAME_MS - self.config.call.end_of_turn_ms
        if not text and voiced_ms >= LONG_SPEECH_MS:
            # Seconds of speech and no words: most likely a language Whisper was not
            # decoding (Kannada heard as English), not noise. Ask instead of ignoring.
            heard.kind = "unclear"
            heard.confidence = 0.0
            heard.confident = False
        elif is_noise(text, conf):
            heard.kind = "noise"
        elif is_echo(text, self._recent_bot_lines()):
            heard.kind = "echo"
        elif conf is not None and conf < MIN_CONFIDENCE:
            heard.kind = "unclear"
        if heard.kind in ("noise", "echo"):
            return heard

        self._caller_turns += 1
        asked = language_request(text) if (self._asked_language or len(text) < 40) else None
        if asked and asked in self.allowed:
            heard.chosen_language = asked
            heard.kind = "words"
            heard.confident = True
            heard.language = asked
            self._language_locked = True
        self.language = heard.language
        log.info("[%s] caller (%s, conf %s): %s", self.call_id, heard.language,
                 None if conf is None else round(conf, 2), text)
        return heard

    async def _respond(self, heard: Heard) -> None:
        unsure = not heard.confident and (heard.confidence is None
                                          or heard.confidence < UNSURE_CONFIDENCE)
        if unsure and not self._asked_language and not self.hint:
            # Garbled words in a language we are not sure of: probably the wrong one.
            self._asked_language = True
            await self._say_phrase(ASK_LANGUAGE_PHRASE, MULTI)
            return
        if heard.kind == "unclear":
            self._misunderstood += 1
            if self._misunderstood >= MISUNDERSTOOD_LIMIT:
                await self._say_phrase("send_text", self.language)
                self.events.put_nowait(_Event("reply_done", end_call=True))
            else:
                await self._say_phrase("repeat", self.language)
            return
        self._misunderstood = 0
        if self.config.call.reply_mode == "scripted" or \
                self.language in self.config.call.scripted_languages:
            await self._scripted_reply(heard.t_eos, heard.timing)
        else:
            await self._reply(heard.t_eos, heard.timing)

    async def _scripted_reply(self, t_eos: float, timing: dict) -> None:
        """The model reads the call and reports what it heard; the reply is chosen by
        rules (app/message.py), so it is always on topic and never invented."""
        lines = [(t.speaker, t.text) for t in self.turns if t.text]
        caller = [t.text for t in self.turns
                  if t.speaker == "caller" and t.text and t.text != "[unclear speech]"]
        t0 = time.monotonic()
        try:
            raw = await asyncio.wait_for(
                self.engines.llm.complete_json(
                    [{"role": "user", "content": build_prompt(self.owner_name, lines)}]),
                SLOT_TIMEOUT_S)
            slots = parse_slots(raw, self.owner_name)
        except Exception as e:
            # Model down or far too slow: the call goes on with simple pattern matching.
            log.warning("[%s] message check failed (%s); using simple rules", self.call_id,
                        type(e).__name__)
            slots = fallback_slots(caller, self.owner_name)
            timing["fallback"] = True
        timing["first_token_ms"] = int((time.monotonic() - t0) * 1000)
        step = self.message.update(slots)
        self.slots = self.message

        # Hindi and Kannada: the prepared line; English (or a language without one):
        # a line with the caller's name, synthesised now.
        if self.language == "en" or self.language not in self.engines.phrases.phrases[step.key]:
            text = english_line(step, self.owner_name)
            audio = await asyncio.to_thread(self.engines.tts.synthesize, text, "en")
            lang = "en"
        else:
            text = self.engines.phrases.text(step.key, self.language, exact=True)
            audio = self.engines.phrases.get(step.key, self.language, exact=True)
            lang = self.language
        timing["first_audio_ms"] = int((time.monotonic() - t_eos) * 1000)
        timing["step"] = step.key
        seg = self._enqueue(audio)
        self.turns.append(Turn("bot", text, 0, 0, lang, timing=timing, parts=[(text, seg)]))
        self.history.append({"role": "assistant", "content": text})
        log.info("[%s] heard %s -> bot: %s  timing %s", self.call_id, slots, text, timing)
        if step.end:
            await self.player.drain(held_too=True)
        self.events.put_nowait(_Event("reply_done", end_call=step.end))

    async def _reply(self, t_eos: float, timing: dict) -> None:
        messages = [{"role": "system", "content": self._system_prompt()}, *self.history]
        splitter = SentenceSplitter()
        spoken: list[str] = []
        turn = Turn("bot", "", 0, 0, self.language, timing=timing)
        ended = False
        t_llm = time.monotonic()
        first_token: float | None = None
        max_sentences = self.config.call.max_reply_sentences

        async def speak(sentence: str) -> None:
            nonlocal ended
            sentence, end = strip_end_token(sentence)
            ended = ended or end
            sentence = clean_reply(sentence)
            # The sentence that ends the call (the goodbye) is always said.
            if not sentence or (len(spoken) >= max_sentences and not end):
                return
            audio = await asyncio.to_thread(self.engines.tts.synthesize, sentence, self.language)
            if "first_audio_ms" not in timing:
                timing["first_audio_ms"] = int((time.monotonic() - t_eos) * 1000)
            turn.parts.append((sentence, self._enqueue(audio)))
            spoken.append(sentence)

        stream = self.engines.llm.stream_chat(messages)
        overflow: list[str] = []    # sentences past the limit; only a goodbye among them is said
        try:
            async for chunk in stream:
                if first_token is None:
                    first_token = time.monotonic()
                    timing["first_token_ms"] = int((first_token - t_llm) * 1000)
                for sentence in splitter.feed(chunk):
                    if len(spoken) < max_sentences and not overflow:
                        await speak(sentence)
                    else:
                        overflow.append(sentence)
                if ended or len(overflow) >= 3 or \
                        any(strip_end_token(o)[1] for o in overflow):
                    break             # said goodbye, or said enough: stop generating
            rest = splitter.flush()
            if rest:
                if len(spoken) < max_sentences and not overflow:
                    await speak(rest)
                else:
                    overflow.append(rest)
            if not ended and any(strip_end_token(o)[1] for o in overflow):
                # The model said more than allowed and then goodbye: say the goodbye.
                said = [clean_reply(strip_end_token(o)[0]) for o in overflow]
                said = [o for o in said if o]
                await speak((said[-1] if said else "") + " " + END_TOKEN)
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
            log.info("[%s] bot: %s  timing %s", self.call_id, turn.text, timing)

        if not spoken:
            # The model said nothing usable; do not leave the caller in silence.
            await self._say_phrase("repeat", self.language)
        if ended:
            await self.player.drain(held_too=True)
        self.events.put_nowait(_Event("reply_done", end_call=ended))

    def _enqueue(self, audio: np.ndarray) -> Segment:
        self._last_bot_audio_at = time.monotonic()
        return self.player.enqueue(audio)

    def _play_filler(self) -> None:
        text = self.engines.phrases.text("one_moment", self.language)
        seg = self._enqueue(self.engines.phrases.get("one_moment", self.language))
        self.turns.append(Turn("bot", text, 0, 0, self._phrase_language(self.language),
                               parts=[(text, seg)]))

    def _system_prompt(self) -> str:
        template = (PROMPTS_DIR / "system.txt").read_text(encoding="utf-8")
        return template.format(owner_name=self.owner_name,
                               language=LANGUAGE_NAMES.get(self.language, "English"),
                               rule_specific_lines=self.rule.extra_prompt or "")

    def _phrase_language(self, language: str) -> str:
        """The language a fixed phrase is actually spoken in."""
        langs = self.config.call.phrase_languages or ["en"]
        if language == MULTI:
            return langs[0]
        return language if language in langs else langs[0]

    async def _say_phrase(self, key: str, language: str) -> None:
        if self._ended:
            return
        self.player.resume()
        audio = self.engines.phrases.get(key, language)
        text = self.engines.phrases.text(key, language)
        seg = self._enqueue(audio)
        self.turns.append(Turn("bot", text, 0, 0, self._phrase_language(language),
                               parts=[(text, seg)]))
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
        if self._ended:
            return
        self.player.resume()          # the goodbye is played out even if held
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
