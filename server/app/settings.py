"""Owner settings: config.yaml gives the defaults, edits from the app override them."""
from __future__ import annotations

from .config import Config
from .db import Database

KEYS = {"owner_name", "owner_number", "retention_days"}


class Settings:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    def all(self) -> dict:
        stored = self.db.get_settings()
        return {
            "owner_name": stored.get("owner_name", self.config.owner.name),
            "owner_number": stored.get("owner_number", self.config.owner.number),
            "retention_days": int(stored.get("retention_days", self.config.storage.retention_days)),
        }

    @property
    def owner_name(self) -> str:
        return self.all()["owner_name"]

    @property
    def owner_number(self) -> str:
        return self.all()["owner_number"]

    @property
    def retention_days(self) -> int:
        return self.all()["retention_days"]

    def update(self, values: dict) -> dict:
        for key, value in values.items():
            if key in KEYS and value is not None:
                self.db.set_setting(key, value)
        return self.all()
