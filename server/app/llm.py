"""Local language model through Ollama, plus helpers for streaming replies."""
from __future__ import annotations

import json
import logging
import re
from typing import AsyncIterator, Protocol

import httpx

log = logging.getLogger(__name__)

END_TOKEN = "[END]"


class LanguageModel(Protocol):
    def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """Yields reply text as it is generated."""

    async def complete_json(self, messages: list[dict]) -> str:
        """Returns one JSON document as text."""


class OllamaLLM:
    def __init__(self, url: str, model: str, max_tokens: int = 120, temperature: float = 0.4,
                 num_ctx: int = 2048):
        self.url = url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        # The same context size on every request: changing it makes Ollama reload the
        # model. 2048 tokens hold a whole 3-minute call and keep GPU memory small.
        self.num_ctx = num_ctx
        self.gpu_share: float | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": -1,
            "options": {"num_predict": self.max_tokens, "temperature": self.temperature,
                        "num_ctx": self.num_ctx},
        }
        async with self._client.stream("POST", f"{self.url}/api/chat", json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                data = json.loads(line)
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    yield chunk
                if data.get("done"):
                    break

    async def complete_json(self, messages: list[dict]) -> str:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "keep_alive": -1,
            "options": {"temperature": 0.1, "num_ctx": self.num_ctx},
        }
        resp = await self._client.post(f"{self.url}/api/chat", json=body)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")

    async def warm(self) -> None:
        """Loads the model into GPU memory so the first call is not slow, and warns if
        it does not fit: layers left on the CPU make every reply many times slower."""
        await self._client.post(f"{self.url}/api/generate",
                                json={"model": self.model, "keep_alive": -1, "prompt": "Hello",
                                      "options": {"num_predict": 1, "num_ctx": self.num_ctx}})
        try:
            resp = await self._client.get(f"{self.url}/api/ps")
            for m in resp.json().get("models", []):
                if m.get("name") == self.model or m.get("model") == self.model:
                    size, vram = m.get("size") or 0, m.get("size_vram") or 0
                    self.gpu_share = vram / size if size else None
            if self.gpu_share is not None and self.gpu_share < 0.95:
                log.warning(
                    "only %d%% of %s is on the GPU; the rest runs on the CPU and replies "
                    "will be slow. Free GPU memory (close other GPU programs, or set "
                    "models.device: cpu so Whisper leaves the GPU to the language model) "
                    "or pick a smaller model.", int(self.gpu_share * 100), self.model)
            elif self.gpu_share is not None:
                log.info("%s is fully on the GPU", self.model)
        except Exception as e:
            log.info("could not ask Ollama where the model runs: %s", e)

    async def aclose(self) -> None:
        await self._client.aclose()


# A sentence ends at . ! ? or the Devanagari danda, followed by whitespace.
_BOUNDARY = re.compile(r"(.+?[.!?।॥]+)(\s+)", re.S)


class SentenceSplitter:
    """Collects streamed text and hands out whole sentences.

    A full stop only ends a sentence once whitespace follows it, so '3.5' or a
    stop at the very end of a chunk is not split early.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text
        out = []
        while True:
            m = _BOUNDARY.match(self._buf)
            if not m:
                break
            sentence = m.group(1).strip()
            self._buf = self._buf[m.end():]
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> str:
        rest, self._buf = self._buf.strip(), ""
        return rest


# Small models write the end marker loosely: [END], [end], <END>, (END), END.
_END = re.compile(r"[\[<(]\s*end\s*[\]>)]|\bEND\b", re.I)
_END_BARE = re.compile(r"\bEND\b")


def strip_end_token(text: str) -> tuple[str, bool]:
    """Removes the end-of-call marker from a sentence. Returns (text, ended)."""
    found = False

    def drop(m: re.Match) -> str:
        nonlocal found
        # A bare "end" only counts in capitals, so "the end of the month" is safe.
        if m.group(0).strip().lower() == "end" and not _END_BARE.fullmatch(m.group(0)):
            return m.group(0)
        found = True
        return ""

    out = _END.sub(drop, text)
    if not found:
        return text, False
    return " ".join(out.split()).strip(), True
