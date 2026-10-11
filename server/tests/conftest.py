import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402


@pytest.fixture
def config(tmp_path) -> Config:
    c = Config()
    c.models.backend = "fake"
    c.realtime = False
    c.owner.name = "Ani"
    c.owner.number = "+919800000000"
    c.network.esp_token = "esp-secret"
    c.network.app_token = "app-secret"
    c.storage.database = str(tmp_path / "calls.db")
    c.storage.audio_dir = str(tmp_path / "audio")
    c.storage.phrase_dir = str(tmp_path / "phrases")
    return c


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "t.db")
