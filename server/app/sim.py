"""Simulated phone calls: a scripted caller talks to the real Session in real time.

The speech models are stand-ins with realistic delays, but everything else is the
live code path: 20 ms frames, voice activity detection, turn taking, barge-in,
fillers, silence prompts, the paced player, the recorder and the post-call record.

Each caller utterance is a tone at its own frequency, so the stand-in
speech-to-text knows which words are in a piece of audio even when turns merge or
split. The bot's speech is a tone too; if it leaks back into the microphone (echo)
the stand-in "transcribes" it as the bot's own words, like Whisper would.

    python ../tools/sim_calls.py           # runs every scenario and prints transcripts
"""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable

import numpy as np

from . import protocol as P
from .audio import SILENCE_FRAME, float_to_int16
from .config import Config
from .fakes import FakeLLM
from .rules import CallRule
from .session import Engines, Session
from .stt import Transcript
from .tts import PhraseCache
from .vad import EnergyVad

RATE = P.SAMPLE_RATE
FIRST_CALLER_FREQ = 640.0           # caller utterances: 640, 683, 726 ... Hz
FREQ_STEP = 43.0
FIRST_BOT_FREQ = 2300.0             # bot sentences: 2300, 2331 ... Hz, reused in a cycle
BOT_STEP = 31.0
BOT_SLOTS = 50


def _tone(freq: float, n: int, level: float, phase0: int = 0) -> np.ndarray:
    t = (np.arange(n) + phase0) / RATE
    return level * np.sin(2 * np.pi * freq * t)


def _peaks(audio16k: np.ndarray, freqs: list[float], rel: float = 0.25) -> list[float]:
    """Which of these frequencies are clearly present in the audio."""
    if len(audio16k) < 400:
        return []
    spec = np.abs(np.fft.rfft(audio16k * np.hanning(len(audio16k))))
    hz = np.fft.rfftfreq(len(audio16k), 1 / 16000)
    top = spec.max() or 1.0
    floor = float(np.median(spec)) or 1e-9
    out = []
    for f in freqs:
        band = spec[(hz > f - 8) & (hz < f + 8)]
        # A tone stands far above the noise floor; white noise never does.
        if len(band) and band.max() >= rel * top and band.max() >= 30 * floor:
            out.append(f)
    return out


# ---- stand-in engines ----------------------------------------------------------
@dataclass
class Utterance:
    text: str
    language: str = "en"
    confidence: float = 0.8
    detected: dict | None = None       # what language detection reports; default certain
    freq: float = 0.0


class SimSTT:
    """Knows the scripted utterances; delays like faster-whisper and IndicConformer."""

    def __init__(self, detect_s: float = 0.9, english_s: float = 0.9, indic_s: float = 1.5):
        self.detect_s = detect_s
        self.english_s = english_s
        self.indic_s = indic_s
        self.utterances: dict[float, Utterance] = {}
        self.bot_said: dict[float, str] = {}          # tone -> the bot sentence it carries
        self.calls: list[tuple[str, float]] = []      # (what, seconds)
        self._last: tuple | None = None

    def _find(self, audio16k: np.ndarray) -> tuple[list[Utterance], list[str]]:
        found = _peaks(audio16k, list(self.utterances) + list(self.bot_said))
        utts = [self.utterances[f] for f in found if f in self.utterances]
        echo = [self.bot_said[f] for f in found if f in self.bot_said]
        return utts, echo

    def detect_language(self, audio16k: np.ndarray) -> dict[str, float]:
        time.sleep(self.detect_s)
        self.calls.append(("detect", self.detect_s))
        utts, echo = self._find(audio16k)
        if utts:
            u = utts[0]
            probs = dict(u.detected or {u.language: 0.92})
            lang = max(probs, key=probs.get)
        elif echo:
            probs, lang = {"en": 0.9}, "en"
        else:
            probs, lang = {"en": 0.4, "si": 0.3, "hi": 0.2}, "en"
        # Like Whisper's single pass: the text in the detected language comes for free.
        self._last = (audio16k, lang)
        return probs

    def transcribe(self, audio16k: np.ndarray, language: str) -> Transcript:
        last, self._last = self._last, None
        # Hindi and Kannada go to IndicConformer, a second model, every time.
        if language in ("hi", "kn") or not (last is not None and last[0] is audio16k
                                             and last[1] == language):
            delay = self.indic_s if language in ("hi", "kn") else self.english_s
            time.sleep(delay)
            self.calls.append((f"transcribe-{language}", delay))
        utts, echo = self._find(audio16k)
        if utts:
            text = " ".join(u.text for u in utts)
            conf = min(u.confidence for u in utts)
            if any(u.language != language for u in utts):
                conf = min(conf, 0.2)        # wrong-language decoding is mostly garbage
            return Transcript(text, language, conf)
        if echo:
            # Whisper writes down the bot's own voice like any other speech.
            return Transcript(" ".join(echo), language, 0.7)
        return Transcript("", language, None)


class SimTTS:
    """A tone as long as the sentence would take to say (~14 characters a second)."""

    def __init__(self, stt: SimSTT, delay_s: float = 0.12, indic_delay_s: float = 0.5):
        self.stt = stt
        self.delay_s = delay_s
        self.indic_delay_s = indic_delay_s
        self.sentences: list[tuple[str, str]] = []

    def synthesize(self, text: str, language: str) -> np.ndarray:
        time.sleep(self.delay_s if language == "en" else self.indic_delay_s)
        freq = FIRST_BOT_FREQ + BOT_STEP * (len(self.sentences) % BOT_SLOTS)
        self.sentences.append((text, language))
        self.stt.bot_said[freq] = text
        n = int(RATE * max(0.3, min(9.0, len(text) / 14)))
        return float_to_int16(_tone(freq, n, 0.1))


class SimLLM(FakeLLM):
    """Streams like Ollama on a small GPU: a pause, then a few tokens at a time."""

    def __init__(self, replies: list[str] | None = None, first_token_s: float = 0.6,
                 tokens_per_s: float = 25.0, policy: Callable[[list[dict]], str] | None = None):
        super().__init__(replies)
        self.first_token_s = first_token_s
        self.tokens_per_s = tokens_per_s
        self.policy = policy
        self.prompts: list[list[dict]] = []
        self.generated: list[str] = []

    def _next_reply(self, messages: list[dict]) -> str:
        if self.replies:
            return self.replies.popleft()
        if self.policy:
            return self.policy(messages)
        return default_policy(messages)

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        self.prompts.append([dict(m) for m in messages])
        reply = self._next_reply(messages)
        self.generated.append(reply)
        await asyncio.sleep(self.first_token_s)
        tokens = re.findall(r"\S+\s*|\s+", reply)
        for tok in tokens:
            yield tok
            await asyncio.sleep(1 / self.tokens_per_s)


def default_policy(messages: list[dict]) -> str:
    """A sensible small model: asks for what is missing, then confirms and ends."""
    caller = " ".join(m["content"] for m in messages if m["role"] == "user").lower()
    has_name = bool(re.search(r"\b(this is|i am|i'm|my name is|name's)\b", caller))
    has_why = bool(re.search(r"\b(about|because|regarding|calling|parcel|meeting|bill|need)\b",
                             caller))
    if has_name and has_why:
        return "Thank you, I will pass your message on. Goodbye. [END]"
    if has_name:
        return "Thank you. What is the call about?"
    if has_why:
        return "I see. May I know your name, please?"
    return "Could you tell me your name and why you are calling?"


# ---- the line ------------------------------------------------------------------
class SimLink:
    """Stands in for the ESP32: receives bot audio and control messages."""

    def __init__(self, echo_level: float = 0.0, echo_delay_ms: int = 200):
        self.messages: list[tuple[float, dict]] = []
        self.bot_frames = 0
        self.echo_level = echo_level
        self.echo: deque[np.ndarray] = deque([np.zeros(P.FRAME_SAMPLES)] * (echo_delay_ms // 20))
        self.last_bot_audio = 0.0     # monotonic time of the last non-silent bot frame
        self.hangup = asyncio.Event()
        self.t0 = time.monotonic()
        self._played: deque[np.ndarray] = deque()

    async def send_json(self, msg: dict) -> None:
        self.messages.append((time.monotonic() - self.t0, msg))
        if msg["type"] == P.CLEAR_AUDIO:
            self._played.clear()
        if msg["type"] == P.HANGUP:
            self.hangup.set()

    async def send_audio(self, frame: bytes) -> None:
        if frame != SILENCE_FRAME:
            self.bot_frames += 1
        # The ESP32 plays frames in real time; the PC sends up to 200 ms ahead.
        self._played.append(np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768)

    def tick(self) -> np.ndarray:
        """One 20 ms of what the ESP32 is playing now; returns its echo for the mic."""
        out = self._played.popleft() if self._played else np.zeros(P.FRAME_SAMPLES)
        if np.abs(out).max() > 0.001:
            self.last_bot_audio = time.monotonic()
        self.echo.append(out)
        return self.echo.popleft() * self.echo_level

    def bot_quiet_for(self) -> float:
        return time.monotonic() - self.last_bot_audio


# ---- the caller ----------------------------------------------------------------
@dataclass
class Say:
    text: str
    language: str = "en"
    ms: int | None = None              # default: about 15 characters a second
    confidence: float = 0.8
    detected: dict | None = None
    gaps_ms: tuple = ()                # pauses inside the sentence, (at_ms, length_ms)


@dataclass
class Pause:
    ms: int


@dataclass
class WaitBot:
    """Waits until the bot has said something and then been quiet a moment."""
    reaction_ms: int = 500
    timeout_s: float = 25.0


@dataclass
class Noise:
    ms: int
    level: float = 0.25


@dataclass
class Hangup:
    pass


@dataclass
class Scenario:
    name: str
    description: str
    script: list
    echo_level: float = 0.0
    echo_delay_ms: int = 200
    background: float = 0.0            # constant line noise level
    rule: CallRule | None = None
    llm: dict = field(default_factory=dict)       # SimLLM options
    stt: dict = field(default_factory=dict)       # SimSTT options
    call: dict = field(default_factory=dict)      # CallConfig overrides
    max_s: float = 60.0


@dataclass
class SimResult:
    scenario: Scenario
    turns: list[dict]
    end_reason: str
    messages: list[tuple[float, dict]]
    timings: list[dict]
    llm_prompts: list[list[dict]]
    llm_generated: list[str]
    stt_calls: list[tuple[str, float]]
    record: dict | None = None
    wall_s: float = 0.0

    def bot_lines(self) -> list[str]:
        return [t["text"] for t in self.turns if t["speaker"] == "bot"]

    def caller_lines(self) -> list[str]:
        return [t["text"] for t in self.turns if t["speaker"] == "caller"]

    def count(self, text: str) -> int:
        return sum(1 for line in self.bot_lines() if text.lower() in line.lower())

    def render(self) -> str:
        out = [f"=== {self.scenario.name}: {self.scenario.description}".rstrip(": ")]
        for t in self.turns:
            who = "Bot   " if t["speaker"] == "bot" else "Caller"
            extra = ""
            if t.get("timing"):
                tm = t["timing"]
                extra = "  (" + ", ".join(f"{k}={v}" for k, v in tm.items()
                                          if k in ("stt_ms", "first_token_ms", "first_audio_ms",
                                                   "slow")) + ")"
            out.append(f"  {t['start_ms'] / 1000:6.1f}s {who} [{t.get('language')}] "
                       f"{t['text']}{extra}")
        out.append(f"  end: {self.end_reason}   wall {self.wall_s:.1f}s")
        if self.record:
            out.append(f"  record: severity={self.record.get('severity')} "
                       f"name={self.record.get('caller_name')} "
                       f"summary={self.record.get('summary')!r}")
        return "\n".join(out)


def unknown_caller(language: str | None = None, extra: str = "") -> CallRule:
    return CallRule(action="answer", group="unknown", contact_id=None, contact_name=None,
                    language_hint=language, extra_prompt=extra, max_seconds=0,
                    severity_floor=None)


def make_config(overrides: dict | None = None) -> Config:
    cfg = Config()
    cfg.realtime = True
    cfg.owner.name = "Anil"
    for k, v in (overrides or {}).items():
        setattr(cfg.call, k, v)
    return cfg


async def run_scenario(sc: Scenario, keep_dir: str | None = None) -> SimResult:
    from .postcall import summarise

    cfg = make_config(sc.call)
    stt = SimSTT(**sc.stt)
    tts = SimTTS(stt)
    llm = SimLLM(**sc.llm)
    tmp = keep_dir or tempfile.mkdtemp(prefix="sim-")
    phrases = PhraseCache(tts, str(Path(tmp) / "phrases"), cfg.owner.name,
                          languages=cfg.call.phrase_languages)
    engines = Engines(stt=stt, llm=llm, tts=tts, phrases=phrases, vad_factory=EnergyVad)
    link = SimLink(sc.echo_level, sc.echo_delay_ms)
    rule = sc.rule or unknown_caller()
    done = asyncio.Event()
    holder: dict = {}

    async def finished(session: Session) -> None:
        holder["session"] = session
        done.set()

    session = Session("sim", "+919800000001", rule, link, engines, cfg, cfg.owner.name, finished)
    t0 = time.monotonic()
    session.start()

    # The caller's audio: a frame source that the script fills.
    pending: deque[np.ndarray] = deque()
    freq_next = [FIRST_CALLER_FREQ]
    rng = np.random.default_rng(7)
    caller_hung_up = asyncio.Event()

    def utter(say: Say) -> None:
        f = freq_next[0]
        freq_next[0] += FREQ_STEP
        stt.utterances[f] = Utterance(say.text, say.language, say.confidence, say.detected, f)
        ms = say.ms or int(max(600, min(6000, len(say.text) * 1000 / 15)))
        n_frames = ms // 20
        gaps = {}
        for at, length in say.gaps_ms:
            for i in range(at // 20, (at + length) // 20):
                gaps[i] = True
        for i in range(n_frames):
            if gaps.get(i):
                pending.append(np.zeros(P.FRAME_SAMPLES))
            else:
                pending.append(_tone(f, P.FRAME_SAMPLES, 0.3, i * P.FRAME_SAMPLES))

    async def mic_loop() -> None:
        next_t = time.monotonic()
        while not session.finished and not caller_hung_up.is_set():
            echo = link.tick()
            voice = pending.popleft() if pending else np.zeros(P.FRAME_SAMPLES)
            x = voice + echo
            if sc.background:
                x = x + rng.normal(0, sc.background, P.FRAME_SAMPLES)
            session.feed_caller(float_to_int16(np.clip(x, -1, 1)).tobytes())
            next_t += 0.02
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))

    async def caller() -> None:
        for step in sc.script:
            if session.finished:
                return
            if isinstance(step, Say):
                utter(step)
                while pending and not session.finished:
                    await asyncio.sleep(0.02)
            elif isinstance(step, Noise):
                for _ in range(step.ms // 20):
                    pending.append(rng.normal(0, step.level, P.FRAME_SAMPLES))
                while pending and not session.finished:
                    await asyncio.sleep(0.02)
            elif isinstance(step, Pause):
                await asyncio.sleep(step.ms / 1000)
            elif isinstance(step, WaitBot):
                start_frames = link.bot_frames
                end = time.monotonic() + step.timeout_s
                while time.monotonic() < end and not session.finished:
                    # A caller who hears "one moment please" waits for the answer.
                    if link.bot_frames > start_frames and link.bot_quiet_for() > 0.3 \
                            and not session._busy() and not session.player.pending:
                        break
                    await asyncio.sleep(0.02)
                await asyncio.sleep(step.reaction_ms / 1000)
            elif isinstance(step, Hangup):
                caller_hung_up.set()
                await session.on_call_end("caller_hangup")
                return

    async def esp_hangup() -> None:
        # The ESP32 answers a hangup by ending the call.
        await link.hangup.wait()
        await asyncio.sleep(0.1)
        await session.on_call_end("pc_hangup")

    tasks = [asyncio.create_task(mic_loop()), asyncio.create_task(caller()),
             asyncio.create_task(esp_hangup())]
    try:
        await asyncio.wait_for(done.wait(), sc.max_s)
    except asyncio.TimeoutError:
        caller_hung_up.set()
        await session.on_call_end("caller_hangup")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    s: Session = holder.get("session", session)
    turns = [{"speaker": t.speaker, "text": t.text, "start_ms": t.start_ms,
              "end_ms": t.end_ms, "language": t.language, "confidence": t.confidence,
              "timing": t.timing} for t in s.turns]
    record = None
    try:
        res = await summarise(llm, [{**t, "seq": i} for i, t in enumerate(turns)],
                              cfg.owner.name, "unknown caller")
        record = json.loads(res.model_dump_json())
    except Exception as e:     # pragma: no cover - reported, not raised
        record = {"error": str(e)}
    return SimResult(sc, turns, s.end_reason or "", link.messages,
                     [t["timing"] for t in turns if t.get("timing")],
                     llm.prompts, llm.generated, stt.calls, record,
                     time.monotonic() - t0)


async def run_all(scenarios: list[Scenario]) -> list[SimResult]:
    """Runs the calls side by side; each one is still in real time."""
    from concurrent.futures import ThreadPoolExecutor

    # Every call blocks threads in its stand-in models; enough threads for all.
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=8 * len(scenarios) + 8))
    return list(await asyncio.gather(*(run_scenario(sc) for sc in scenarios)))
