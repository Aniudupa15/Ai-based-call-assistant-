"""Spoken language identification: English, Hindi or Kannada, from the audio itself.

Whisper's own language guess is poor for Kannada on phone and laptop audio (it
says Greek, French, Tamil ...), so Kannada callers got English transcripts of
nonsense. SpeechBrain's VoxLingua107 model (speechbrain/lang-id-voxlingua107-ecapa)
is trained to recognise 107 spoken languages, Kannada and Hindi included. It is
small and runs on the CPU in a fraction of a second.

Only the call's languages are compared: Kannada is often confused with its
neighbours Telugu or Tamil, which a Karnataka line does not need to tell apart.
"""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)


class SpeechLID:
    def __init__(self, source: str = "speechbrain/lang-id-voxlingua107-ecapa",
                 model_dir: str = "data/models", device: str = "cpu"):
        import torch
        from speechbrain.inference.classifiers import EncoderClassifier

        self._torch = torch
        savedir = f"{model_dir}/{source.replace('/', '--')}"
        kwargs = {"source": source, "savedir": savedir, "run_opts": {"device": device}}
        try:
            # Windows: plain copies, not symbolic links (those need admin rights).
            from speechbrain.utils.fetching import LocalStrategy

            self.model = EncoderClassifier.from_hparams(**kwargs,
                                                        local_strategy=LocalStrategy.COPY)
        except (ImportError, TypeError):
            self.model = EncoderClassifier.from_hparams(**kwargs)
        encoder = self.model.hparams.label_encoder
        # Labels look like "kn: Kannada"; keep the code.
        self.codes = {i: str(lab).split(":")[0].strip()
                      for i, lab in encoder.ind2lab.items()}

    def probs(self, audio16k: np.ndarray, languages: list[str]) -> dict[str, float]:
        """Probability of each of these languages, adding up to 1."""
        wav = self._torch.from_numpy(np.asarray(audio16k, dtype=np.float32)).unsqueeze(0)
        with self._torch.no_grad():
            log_probs = self.model.classify_batch(wav)[0].squeeze(0)
        scores = {}
        for i, code in self.codes.items():
            if code in languages:
                scores[code] = float(log_probs[i])
        if not scores:
            return {}
        top = max(scores.values())
        exp = {k: np.exp(v - top) for k, v in scores.items()}
        total = sum(exp.values())
        return {k: v / total for k, v in exp.items()}
