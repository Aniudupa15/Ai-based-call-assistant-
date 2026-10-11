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
    def __init__(self, model_name: str, device: str = "cuda", compute_type: str = "auto"):
        try:
            # On Windows, importing torch first puts its CUDA libraries (cuBLAS, cuDNN)
            # where CTranslate2 can find them.
            import torch  # noqa: F401
        except ImportError:
            pass
        from faster_whisper import WhisperModel

        if compute_type == "auto":
            compute_type = "int8_float16" if device == "cuda" else "int8"
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self._last: tuple | None = None   # (audio, language, Transcript) from detect_language

    def _run(self, audio16k: np.ndarray, language: str | None):
        segments, info = self.model.transcribe(
            audio16k, language=language, beam_size=1, vad_filter=False,
            condition_on_previous_text=False)
        segs = list(segments)
        text = " ".join(s.text.strip() for s in segs).strip()
        conf = float(math.exp(sum(s.avg_logprob for s in segs) / len(segs))) if segs else None
        return info, Transcript(text, info.language, conf)

    def detect_language(self, audio16k: np.ndarray) -> dict[str, float]:
        # One pass detects the language and transcribes; transcribe() reuses the text
        # when it is asked for the same audio in the detected language.
        info, transcript = self._run(audio16k, None)
        self._last = (audio16k, info.language, transcript)
        return dict(info.all_language_probs or [(info.language, info.language_probability)])

    def transcribe(self, audio16k: np.ndarray, language: str) -> Transcript:
        last, self._last = self._last, None
        if last is not None and last[0] is audio16k and last[1] == language:
            return last[2]
        return self._run(audio16k, language)[1]


class IndicConformerSTT:
    """ai4bharat/indic-conformer-600m-multilingual, CTC decoding."""

    def __init__(self, model_name: str, device: str = "cuda", model_dir: str = "data/models"):
        from pathlib import Path

        import torch
        from transformers import AutoModel

        self._torch = torch
        path = Path(model_name)
        if not path.exists():
            # The model's ONNX weights are separate files. onnxruntime refuses to load them
            # through the Hugging Face cache's links, so download into a plain folder.
            from huggingface_hub import snapshot_download

            path = Path(model_dir) / model_name.replace("/", "--")
            if not (path / "config.json").exists():
                log.info("downloading %s to %s", model_name, path)
                snapshot_download(model_name, local_dir=str(path))
        self.model = self._load_local(path) or AutoModel.from_pretrained(
            str(path), trust_remote_code=True)
        if hasattr(self.model, "to"):
            try:
                self.model = self.model.to(device)
            except Exception:  # some remote-code models manage their own device
                log.info("IndicConformer stays on its default device")

    @staticmethod
    def _load_local(path):
        """Builds the model straight from the downloaded folder.

        The model's own from_pretrained() treats its argument as a Hub repo id, which a
        Windows folder path is not, so its class is instantiated directly instead.
        """
        import importlib.util

        code = path / "model_onnx.py"
        if not code.exists():
            return None
        try:
            spec = importlib.util.spec_from_file_location("indic_conformer_onnx", code)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cfg_cls = mod.IndicASRConfig
            model_cls = next(obj for obj in vars(mod).values()
                             if isinstance(obj, type) and getattr(obj, "config_class", None) is cfg_cls)
            return model_cls(cfg_cls(ts_folder=str(path)))
        except Exception as e:
            log.warning("loading IndicConformer from %s directly failed: %s", path, e)
            return None

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
