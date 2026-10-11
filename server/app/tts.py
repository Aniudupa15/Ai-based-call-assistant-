"""Text-to-speech and the cache of fixed phrases.

Piper speaks English and runs faster than real time on the CPU. Indic Parler-TTS
speaks Hindi and Kannada on the GPU and is slower, so replies are kept short and
fixed phrases are synthesised once and stored as 8 kHz audio.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Protocol

import numpy as np
import yaml

from .audio import float_to_int16, read_wav, resample, write_wav
from .protocol import SAMPLE_RATE

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
MULTI = "multi"
MULTI_ORDER = ["en", "kn", "hi"]


class TextToSpeech(Protocol):
    def synthesize(self, text: str, language: str) -> np.ndarray:
        """Returns int16 mono audio at 8 kHz."""


class PiperTTS:
    def __init__(self, voice_path: str):
        try:
            from piper import PiperVoice
        except ImportError:  # piper-tts 1.2 layout
            from piper.voice import PiperVoice  # type: ignore
        self.voice = PiperVoice.load(voice_path)
        self.rate = int(self.voice.config.sample_rate)

    def synthesize(self, text: str, language: str = "en") -> np.ndarray:
        if hasattr(self.voice, "synthesize_stream_raw"):  # piper-tts 1.2
            pcm = b"".join(self.voice.synthesize_stream_raw(text))
            audio = np.frombuffer(pcm, dtype="<i2")
        else:  # piper-tts 1.3+: yields AudioChunk objects
            chunks = [c.audio_int16_array for c in self.voice.synthesize(text)]
            audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype="<i2")
        return resample(audio, self.rate, SAMPLE_RATE)


class IndicParlerTTS:
    """ai4bharat/indic-parler-tts: free, natural Indian voices (Indian English, Hindi,
    Kannada ...). Too heavy to run beside the language model on a 4 GB GPU, so
    tools/make_voice.py uses it to record the bot's lines ahead of time."""

    # Speakers the model card recommends for each language.
    SPEAKERS = {"en": "Thoma", "hi": "Divya", "kn": "Anu"}
    STYLE = ("{speaker} speaks at a moderate pace with a clear, warm and professional tone. "
             "The recording is very high quality, close-sounding, with no background noise.")

    def __init__(self, model_name: str = "ai4bharat/indic-parler-tts", device: str = "cuda",
                 speakers: dict[str, str] | None = None):
        import torch
        from parler_tts import ParlerTTSForConditionalGeneration
        from transformers import AutoTokenizer

        self._torch = torch
        self.device = device
        self.speakers = {**self.SPEAKERS, **(speakers or {})}
        self.model = ParlerTTSForConditionalGeneration.from_pretrained(model_name).to(device)
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.desc_tokenizer = AutoTokenizer.from_pretrained(
            self.model.config.text_encoder._name_or_path)
        self.rate = int(self.model.config.sampling_rate)

    def synthesize(self, text: str, language: str, speaker: str | None = None,
                   seed: int = 0) -> np.ndarray:
        name = speaker or self.speakers.get(language, self.speakers["en"])
        desc = self.desc_tokenizer(self.STYLE.format(speaker=name),
                                   return_tensors="pt").to(self.device)
        prompt = self.tokenizer(text, return_tensors="pt").to(self.device)
        self._torch.manual_seed(seed)        # the same line comes out the same way
        with self._torch.no_grad():
            out = self.model.generate(
                input_ids=desc.input_ids, attention_mask=desc.attention_mask,
                prompt_input_ids=prompt.input_ids, prompt_attention_mask=prompt.attention_mask)
        audio = out.cpu().numpy().squeeze().astype(np.float32)
        return resample(float_to_int16(audio), self.rate, SAMPLE_RATE)


class MmsTTS:
    """Meta's MMS-TTS voices (facebook/mms-tts-hin, -kan): small VITS models, fast on a CPU."""

    MODELS = {"hi": "facebook/mms-tts-hin", "kn": "facebook/mms-tts-kan"}

    def __init__(self, languages: list[str], device: str = "cpu"):
        import torch
        from transformers import AutoTokenizer, VitsModel

        self._torch = torch
        self.device = device
        self.voices = {}
        self._uroman = None
        for lang in languages:
            if lang in self.MODELS:
                name = self.MODELS[lang]
                model = VitsModel.from_pretrained(name).to(device).eval()
                self.voices[lang] = (model, AutoTokenizer.from_pretrained(name))

    def _romanise(self, text: str) -> str:
        if self._uroman is None:
            import uroman  # some MMS voices are trained on romanised text

            self._uroman = uroman.Uroman()
        return self._uroman.romanize_string(text)

    def synthesize(self, text: str, language: str) -> np.ndarray:
        model, tokenizer = self.voices.get(language) or next(iter(self.voices.values()))
        if getattr(tokenizer, "is_uroman", False):
            text = self._romanise(text)
        inputs = tokenizer(text, return_tensors="pt").to(self.device)
        with self._torch.no_grad():
            wave = model(**inputs).waveform
        audio = wave.cpu().numpy().squeeze().astype(np.float32)
        return resample(float_to_int16(audio), int(model.config.sampling_rate), SAMPLE_RATE)


class KokoroTTS:
    """Kokoro-82M (hexgrad/Kokoro-82M): natural voices, faster than real time on a CPU.

    voices maps a language to a Kokoro voice: American (af_heart, af_bella, am_michael
    ...), British (bf_emma, bm_george ...) or Hindi speakers (hf_alpha, hf_beta,
    hm_omega, hm_psi). English text always goes through English pronunciation rules,
    so a Hindi speaker's voice on English gives Indian-accented English.
    Names it does not know are spelled out by eSpeak, which misaki bundles.
    """

    RATE = 24000

    def __init__(self, voices: dict[str, str], device: str = "cpu", speed: float = 1.0):
        from kokoro import KPipeline

        self.speed = speed
        self.pipes: dict[str, tuple] = {}
        model = True
        for lang, voice in voices.items():
            # Pronunciation rules follow the language of the text, not the speaker.
            code = "h" if lang == "hi" else ("b" if voice.startswith("b") else "a")
            try:
                pipe = KPipeline(lang_code=code, model=model, device=device,
                                 repo_id="hexgrad/Kokoro-82M")
            except TypeError:       # older kokoro without repo_id
                pipe = KPipeline(lang_code=code, model=model, device=device)
            model = pipe.model      # one model shared by every language
            if code in "ab" and getattr(getattr(pipe, "g2p", None), "fallback", True) is None:
                log.warning("Kokoro has no eSpeak fallback: names it does not know will be "
                            "skipped. pip install --upgrade misaki[en] espeakng-loader")
            self.pipes[lang] = (pipe, voice)

    def synthesize(self, text: str, language: str = "en") -> np.ndarray:
        pipe, voice = self.pipes.get(language) or self.pipes.get("en") or \
            next(iter(self.pipes.values()))
        parts = []
        for result in pipe(text, voice=voice, speed=self.speed):
            audio = result.audio if hasattr(result, "audio") else result[2]
            if audio is not None:
                parts.append(audio.detach().cpu().numpy().astype(np.float32))
        audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return resample(float_to_int16(audio), self.RATE, SAMPLE_RATE)


class SarvamTTS:
    """Sarvam AI's Bulbul voices (api.sarvam.ai): natural Indian voices for English,
    Hindi and Kannada. Needs internet and an API key; only the bot's own words are
    sent, never the caller's audio. Fixed lines are cached on disk, so most calls
    only send the few lines that contain the caller's name."""

    URL = "https://api.sarvam.ai/text-to-speech"
    LANG = {"en": "en-IN", "hi": "hi-IN", "kn": "kn-IN"}

    def __init__(self, api_key: str, model: str = "bulbul:v3", speaker: str = "priya",
                 pace: float = 1.0, speakers: dict[str, str] | None = None,
                 timeout: float = 5.0, client=None):
        import httpx

        if not api_key:
            raise ValueError("no Sarvam API key (sarvam.api_key or SARVAM_API_KEY)")
        self.model = model
        self.speaker = speaker
        self.speakers = speakers or {}
        self.pace = pace
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {"api-subscription-key": api_key}
        self._memo: dict[tuple[str, str], np.ndarray] = {}

    def synthesize(self, text: str, language: str = "en") -> np.ndarray:
        key = (text, language)
        if key in self._memo:
            return self._memo[key]
        import base64
        import io
        import wave

        body = {"text": text, "target_language_code": self.LANG.get(language, "en-IN"),
                "speaker": self.speakers.get(language, self.speaker), "model": self.model,
                "pace": self.pace, "speech_sample_rate": SAMPLE_RATE}
        resp = self._client.post(self.URL, json=body, headers=self._headers)
        if resp.status_code != 200:
            raise RuntimeError(f"Sarvam TTS {resp.status_code}: {resp.text[:200]}")
        parts = []
        for b64 in resp.json().get("audios", []):
            with wave.open(io.BytesIO(base64.b64decode(b64))) as w:
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
                if w.getnchannels() > 1:
                    pcm = pcm.reshape(-1, w.getnchannels())[:, 0]
                parts.append(resample(pcm, w.getframerate(), SAMPLE_RATE))
        if not parts:
            raise RuntimeError("Sarvam TTS returned no audio")
        audio = np.concatenate(parts).astype("<i2")
        if len(self._memo) < 200:
            self._memo[key] = audio
        return audio


def line_path(folder: str | Path, text: str, language: str) -> Path:
    """Where a pre-recorded line lives: one file per language and exact text."""
    digest = hashlib.sha1(" ".join(text.split()).encode()).hexdigest()[:16]
    return Path(folder) / language / f"{digest}.wav"


class PrerecordedTTS:
    """Plays lines recorded ahead of time by tools/make_voice.py with a heavy, natural
    voice (Indic Parler-TTS); anything not recorded is spoken by the live voice."""

    def __init__(self, folder: str | Path, fallback: TextToSpeech):
        self.folder = Path(folder)
        self.fallback = fallback
        self.hits = 0
        self.misses = 0

    def synthesize(self, text: str, language: str) -> np.ndarray:
        path = line_path(self.folder, text, language)
        if path.exists():
            self.hits += 1
            audio, rate = read_wav(path)
            return resample(audio, rate, SAMPLE_RATE)
        self.misses += 1
        log.info("no recorded line for %r (%s); live voice used", text[:60], language)
        return self.fallback.synthesize(text, language)


class FallbackTTS:
    """Tries the cloud voice first; if it fails (no internet, quota, a slow reply),
    the local voice speaks instead so the caller never hears silence."""

    def __init__(self, primary: TextToSpeech, fallback: TextToSpeech):
        self.primary = primary
        self.fallback = fallback

    def synthesize(self, text: str, language: str) -> np.ndarray:
        try:
            return self.primary.synthesize(text, language)
        except Exception as e:
            log.warning("cloud voice failed (%s); using the local voice", str(e)[:200])
            return self.fallback.synthesize(text, language)


class RoutingTTS:
    """Picks the engine by language. say_as rewrites words before English synthesis,
    e.g. {"Anirudha": "Uh-nee-rood-ha"} so a name is pronounced the way it should be."""

    def __init__(self, english: TextToSpeech, indic: TextToSpeech | None,
                 per_language: dict[str, TextToSpeech] | None = None,
                 say_as: dict[str, str] | None = None):
        self.english = english
        self.indic = indic
        self.per_language = per_language or {}
        self.say_as = {k: v for k, v in (say_as or {}).items() if k and v}

    def synthesize(self, text: str, language: str) -> np.ndarray:
        if language == "en":
            for word, spoken in self.say_as.items():
                text = text.replace(word, spoken)
        if language in self.per_language:
            return self.per_language[language].synthesize(text, language)
        if language != "en" and self.indic is not None:
            return self.indic.synthesize(text, language)
        return self.english.synthesize(text, language)


def load_phrases(path: Path | None = None) -> dict[str, dict[str, str]]:
    with open(path or PROMPTS_DIR / "phrases.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Spoken in the caller's own language when replies are scripted (call.scripted_languages).
SCRIPTED_KEYS = ("ask_name_reason", "ask_name", "ask_reason", "goodbye")


class PhraseCache:
    """Fixed phrases per language, stored as 8 kHz WAV.

    File names carry a hash of the text, so editing phrases.yaml or the owner's
    name makes the next build synthesise them again.
    """

    def __init__(self, tts: TextToSpeech, cache_dir: str | Path, owner_name: str,
                 phrases: dict[str, dict[str, str]] | None = None,
                 languages: list[str] | None = None,
                 reply_languages: list[str] | None = None,
                 voice_tag: str = ""):
        self.tts = tts
        # One folder per voice set-up, so changing a voice rebuilds the phrases.
        self.dir = Path(cache_dir) / voice_tag if voice_tag else Path(cache_dir)
        self.owner_name = owner_name
        self.phrases = phrases or load_phrases()
        # Languages fixed phrases are spoken in, in MULTI order; others fall back to the first.
        self.languages = [l for l in MULTI_ORDER if l in (languages or MULTI_ORDER)] or ["en"]
        # Languages the scripted replies are built in (see SCRIPTED_KEYS).
        self.reply_languages = list(reply_languages or [])
        self._mem: dict[tuple[str, str], np.ndarray] = {}

    def set_owner(self, name: str) -> None:
        """The owner's name is part of the greeting; a new name means new audio."""
        if name != self.owner_name:
            self.owner_name = name
            self._mem.clear()

    def _lang(self, language: str, key: str = "", exact: bool = False) -> str:
        if exact and key in self.phrases and language in self.phrases[key]:
            return language
        return language if language in self.languages or language == MULTI else self.languages[0]

    def text(self, key: str, language: str, exact: bool = False) -> str:
        """exact: in this language if the phrase has it, whatever phrase_languages say."""
        language = self._lang(language, key, exact)
        if language == MULTI:
            return " ".join(self.text(key, l) for l in self.languages if l in self.phrases[key])
        entry = self.phrases[key]
        return entry.get(language, entry["en"]).format(owner_name=self.owner_name)

    def _path(self, key: str, language: str) -> Path:
        digest = hashlib.sha1(self.text(key, language, exact=True).encode()).hexdigest()[:10]
        return self.dir / language / f"{key}-{digest}.wav"

    def get(self, key: str, language: str, exact: bool = False) -> np.ndarray:
        language = self._lang(language, key, exact)
        if language == MULTI:
            gap = np.zeros(int(0.3 * SAMPLE_RATE), dtype="<i2")
            parts = []
            for l in self.languages:
                if l in self.phrases[key]:
                    parts += [self.get(key, l), gap]
            return np.concatenate(parts[:-1]) if parts else np.zeros(0, dtype="<i2")
        mem_key = (key, language)
        if mem_key in self._mem:
            return self._mem[mem_key]
        path = self._path(key, language)
        if path.exists():
            audio, rate = read_wav(path)
            audio = resample(audio, rate, SAMPLE_RATE)
        else:
            audio = self.tts.synthesize(self.text(key, language, exact=True), language)
            write_wav(path, audio)
        self._mem[mem_key] = audio
        return audio

    def build(self, languages: list[str]) -> None:
        for key, entry in self.phrases.items():
            for language in languages:
                if language in entry and language in self.languages:
                    self.get(key, language)
        for key in SCRIPTED_KEYS:
            for language in self.reply_languages:
                if language in self.phrases.get(key, {}):
                    self.get(key, language, exact=True)
        log.info("phrase cache ready in %s", self.dir)
