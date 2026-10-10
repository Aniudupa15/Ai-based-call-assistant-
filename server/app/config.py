"""Server settings, loaded from config.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class OwnerConfig(BaseModel):
    name: str = "the owner"
    number: str = ""


class NetworkConfig(BaseModel):
    listen: str = "0.0.0.0"
    port: int = 8765
    esp_token: str = "change-me"
    app_token: str = "change-me-too"


class CallConfig(BaseModel):
    max_seconds: int = 180
    speech_start_ms: int = 200
    end_of_turn_ms: int = 600
    barge_in_ms: int = 300
    silence_prompt_s: int = 6
    filler_after_ms: int = 1200
    max_reply_sentences: int = 2
    languages: list[str] = Field(default_factory=lambda: ["en", "hi", "kn"])
    default_language: str = "en"


class ModelsConfig(BaseModel):
    backend: Literal["local", "fake"] = "local"
    stt_english: str = "small"
    stt_indic: str = "ai4bharat/indic-conformer-600m-multilingual"
    llm: str = "qwen2.5:7b-instruct"
    ollama_url: str = "http://127.0.0.1:11434"
    tts_english: str = "voices/en_US-lessac-medium.onnx"
    tts_indic: str = "ai4bharat/indic-parler-tts"
    device: str = "cuda"
    vad: Literal["silero", "energy"] = "silero"


class StorageConfig(BaseModel):
    database: str = "data/calls.db"
    audio_dir: str = "data/audio"
    phrase_dir: str = "data/phrases"
    retention_days: int = 30


class NotifyConfig(BaseModel):
    sms_urgent: bool = True
    sms_high_after_minutes: int = 10
    sms_normal_after_minutes: int = 60


class Config(BaseModel):
    owner: OwnerConfig = Field(default_factory=OwnerConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    call: CallConfig = Field(default_factory=CallConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    # Sends bot audio paced in real time. Tests switch it off to run fast.
    realtime: bool = True


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Config.model_validate(data)
