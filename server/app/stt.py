"""Speech-to-text.

faster-whisper detects the language of every turn and transcribes English.
IndicConformer transcribes Hindi and Kannada, which Whisper handles poorly on
8 kHz phone audio. Model calls block, so the session runs them in a thread.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Transcript:
    text: str
    language: str | None = None
    confidence: float | None = None


class SpeechToText(Protocol):
    def detect_language(self, audio16k: np.ndarray) -> dict[str, float]:
        """audio16k: float32 mono at 16 kHz. Returns language -> probability."""

    def transcribe(self, audio16k: np.ndarray, language: str) -> Transcript:
        ...


class WhisperSTT:
    def __init__(self, model_name: str, device: str = "cuda"):
        from faster_whisper import WhisperModel

        compute = "float16" if device == "cuda" else "int8"
        self.model = WhisperModel(model_name, device=device, compute_type=compute)

    def detect_language(self, audio16k: np.ndarray) -> dict[str, float]:
        if hasattr(self.model, "detect_language"):
            _lang, _prob, all_probs = self.model.detect_language(audio16k)
            return dict(all_probs)
        _segments, info = self.model.transcribe(audio16k, beam_size=1)
        return dict(info.all_language_probs or [(info.language, info.language_probability)])

    def transcribe(self, audio16k: np.ndarray, language: str) -> Transcript:
        segments, _info = self.model.transcribe(
            audio16k, language=language, beam_size=1, vad_filter=False,
            condition_on_previous_text=False)
        segs = list(segments)
        text = " ".join(s.text.strip() for s in segs).strip()
        conf = None
        if segs:
            conf = float(math.exp(sum(s.avg_logprob for s in segs) / len(segs)))
        return Transcript(text, language, conf)


class IndicConformerSTT:
    """ai4bharat/indic-conformer-600m-multilingual, CTC decoding."""

    def __init__(self, model_name: str, device: str = "cuda"):
        import torch
        from transformers import AutoModel

        self._torch = torch
        self.model = AutoModel.from_pretrained(model_name, trust_remote_code=True)
        if hasattr(self.model, "to"):
            try:
                self.model = self.model.to(device)
            except Exception:  # some remote-code models manage their own device
                log.info("IndicConformer stays on its default device")

    def transcribe(self, audio16k: np.ndarray, language: str) -> Transcript:
        wav = self._torch.from_numpy(audio16k.astype(np.float32)).unsqueeze(0)
        text = self.model(wav, language, "ctc")
        if isinstance(text, (list, tuple)):
            text = text[0] if text else ""
        return Transcript(str(text).strip(), language, None)


class RoutingSTT:
    """Whisper for detection and English; IndicConformer for Hindi and Kannada."""

    def __init__(self, whisper: WhisperSTT, indic: IndicConformerSTT | None):
        self.whisper = whisper
        self.indic = indic

    def detect_language(self, audio16k: np.ndarray) -> dict[str, float]:
        return self.whisper.detect_language(audio16k)

    def transcribe(self, audio16k: np.ndarray, language: str) -> Transcript:
        if language in {"hi", "kn"} and self.indic is not None:
            return self.indic.transcribe(audio16k, language)
        return self.whisper.transcribe(audio16k, language)
