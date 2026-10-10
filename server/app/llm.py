"""Local language model through Ollama, plus helpers for streaming replies."""
from __future__ import annotations

import json
import re
from typing import AsyncIterator, Protocol

import httpx

END_TOKEN = "[END]"


class LanguageModel(Protocol):
    def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """Yields reply text as it is generated."""

    async def complete_json(self, messages: list[dict]) -> str:
        """Returns one JSON document as text."""


class OllamaLLM:
    def __init__(self, url: str, model: str, max_tokens: int = 120, temperature: float = 0.4):
        self.url = url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": -1,
            "options": {"num_predict": self.max_tokens, "temperature": self.temperature},
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
            "options": {"temperature": 0.1},
        }
        resp = await self._client.post(f"{self.url}/api/chat", json=body)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")

    async def warm(self) -> None:
        """Loads the model into GPU memory so the first call is not slow."""
        await self._client.post(f"{self.url}/api/generate",
                                json={"model": self.model, "keep_alive": -1})

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


def strip_end_token(text: str) -> tuple[str, bool]:
    """Removes [END] from a sentence. Returns (text, ended)."""
    if END_TOKEN in text:
        return text.replace(END_TOKEN, "").strip(), True
    return text, False
