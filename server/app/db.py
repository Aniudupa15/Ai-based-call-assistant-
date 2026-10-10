"""SQLite access. Everything except audio lives here; audio paths are stored in rows."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL DEFAULT 'answer',
    language_hint TEXT,
    extra_prompt TEXT NOT NULL DEFAULT '',
    max_seconds INTEGER,
    severity_floor TEXT
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    rule_id INTEGER REFERENCES rules(id)
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    number TEXT NOT NULL UNIQUE,
    group_id INTEGER REFERENCES groups(id),
    language_hint TEXT,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    number TEXT NOT NULL DEFAULT '',
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    started_at INTEGER NOT NULL,
    answered_at INTEGER,
    ended_at INTEGER,
    duration_s INTEGER,
    handled_by TEXT NOT NULL,
    end_reason TEXT,
    status TEXT NOT NULL,
    language TEXT,
    summary TEXT,
    reason TEXT,
    severity TEXT,
    severity_why TEXT,
    severity_corrected TEXT,
    caller_name TEXT,
    action_needed TEXT,
    deadline TEXT,
    callback_number TEXT,
    flags TEXT NOT NULL DEFAULT '[]',
    audio_path TEXT,
    audio_sha256 TEXT,
    seen_at INTEGER,
    notified_app_at INTEGER,
    notified_sms_at INTEGER
);
CREATE INDEX IF NOT EXISTS calls_started ON calls(started_at);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    speaker TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    language TEXT,
    text TEXT NOT NULL,
    text_en TEXT,
    confidence REAL,
    timing TEXT
);
CREATE INDEX IF NOT EXISTS turns_call ON turns(call_id, seq);
CREATE TABLE IF NOT EXISTS commitments (
    id INTEGER PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sms_outbox (
    id INTEGER PRIMARY KEY,
    to_number TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    sent_at INTEGER,
    ok INTEGER
);
CREATE TABLE IF NOT EXISTS health (
    ts INTEGER NOT NULL,
    registered INTEGER,
    rssi INTEGER,
    sd_free_mb INTEGER,
    on_battery INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    call_id TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_RULES = [
    # name, action, extra_prompt, severity_floor
    ("family", "answer",
     "This caller is family. Tell them {owner_name} will call back by evening.", "normal"),
    ("known", "answer", "This caller is a known contact.", None),
    ("unknown", "answer",
     "This caller is not in the contacts. Share nothing about {owner_name} "
     "beyond that they are unavailable.", None),
    ("blocked", "reject", "", None),
]

CALL_COLUMNS = {
    "number", "contact_id", "started_at", "answered_at", "ended_at", "duration_s",
    "handled_by", "end_reason", "status", "language", "summary", "reason", "severity",
    "severity_why", "severity_corrected", "caller_name", "action_needed", "deadline",
    "callback_number", "flags", "audio_path", "audio_sha256", "seen_at",
    "notified_app_at", "notified_sms_at",
}


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
        self._seed()

    def close(self) -> None:
        self._conn.close()

    # ---- helpers -------------------------------------------------------------
    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(args))

    def _one(self, sql: str, args: Iterable[Any] = ()) -> dict | None:
        row = self._exec(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self._exec(sql, args).fetchall()]

    def _seed(self) -> None:
        if self._one("SELECT id FROM rules LIMIT 1"):
            return
        with self._lock:
            for name, action, prompt, floor in DEFAULT_RULES:
                cur = self._conn.execute(
                    "INSERT INTO rules(name, action, extra_prompt, severity_floor) VALUES (?,?,?,?)",
                    (name, action, prompt, floor))
                self._conn.execute("INSERT INTO groups(name, rule_id) VALUES (?,?)",
                                   (name, cur.lastrowid))

    # ---- calls ---------------------------------------------------------------
    def create_call(self, call_id: str, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if k in CALL_COLUMNS}
        if isinstance(fields.get("flags"), list):
            fields["flags"] = json.dumps(fields["flags"])
        cols = ["id", *fields]
        self._exec(
            f"INSERT INTO calls({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [call_id, *fields.values()])

    def update_call(self, call_id: str, **fields: Any) -> None:
        bad = set(fields) - CALL_COLUMNS
        if bad:
            raise ValueError(f"unknown call columns: {bad}")
        if not fields:
            return
        if isinstance(fields.get("flags"), list):
            fields["flags"] = json.dumps(fields["flags"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE calls SET {sets} WHERE id = ?", [*fields.values(), call_id])

    def get_call(self, call_id: str) -> dict | None:
        row = self._one(
            "SELECT c.*, k.name AS contact_name FROM calls c "
            "LEFT JOIN contacts k ON k.id = c.contact_id WHERE c.id = ?", [call_id])
        return _decode_call(row) if row else None

    def find_call(self, number: str, started_at: int, handled_by: str) -> dict | None:
        row = self._one("SELECT id FROM calls WHERE number = ? AND started_at = ? AND handled_by = ?",
                        [number, started_at, handled_by])
        return self.get_call(row["id"]) if row else None

    def list_calls(self, since: int | None = None, severity: str | None = None,
                   handled_by: str | None = None, unseen: bool = False,
                   limit: int = 100) -> list[dict]:
        where, args = [], []
        if since is not None:
            where.append("c.started_at >= ?")
            args.append(since)
        if severity:
            where.append("COALESCE(c.severity_corrected, c.severity) = ?")
            args.append(severity)
        if handled_by:
            where.append("c.handled_by = ?")
            args.append(handled_by)
        if unseen:
            where.append("c.seen_at IS NULL")
        sql = ("SELECT c.*, k.name AS contact_name FROM calls c "
               "LEFT JOIN contacts k ON k.id = c.contact_id")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY c.started_at DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        return [_decode_call(r) for r in self._all(sql, args)]

    def delete_call(self, call_id: str) -> dict | None:
        """Removes the call with its turns and commitments. Returns the row so the
        caller can delete the audio file."""
        call = self.get_call(call_id)
        if call:
            self._exec("DELETE FROM calls WHERE id = ?", [call_id])
        return call

    def calls_with_audio_before(self, ts: int) -> list[dict]:
        return self._all("SELECT id, audio_path FROM calls WHERE audio_path IS NOT NULL "
                         "AND started_at < ?", [ts])

    # ---- turns and commitments ----------------------------------------------
    def add_turn(self, call_id: str, seq: int, speaker: str, start_ms: int, end_ms: int,
                 text: str, language: str | None = None, text_en: str | None = None,
                 confidence: float | None = None, timing: dict | None = None) -> None:
        self._exec(
            "INSERT INTO turns(call_id, seq, speaker, start_ms, end_ms, language, text, "
            "text_en, confidence, timing) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [call_id, seq, speaker, start_ms, end_ms, language, text, text_en, confidence,
             json.dumps(timing) if timing else None])

    def set_turn_translation(self, call_id: str, seq: int, text_en: str) -> None:
        self._exec("UPDATE turns SET text_en = ? WHERE call_id = ? AND seq = ?",
                   [text_en, call_id, seq])

    def get_turns(self, call_id: str) -> list[dict]:
        rows = self._all("SELECT seq, speaker, start_ms, end_ms, language, text, text_en, "
                         "confidence, timing FROM turns WHERE call_id = ? ORDER BY seq", [call_id])
        for r in rows:
            r["timing"] = json.loads(r["timing"]) if r["timing"] else None
        return rows

    def set_commitments(self, call_id: str, items: list[str]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM commitments WHERE call_id = ?", [call_id])
            for text in items:
                self._conn.execute("INSERT INTO commitments(call_id, text) VALUES (?,?)",
                                   [call_id, text])

    def get_commitments(self, call_id: str) -> list[str]:
        return [r["text"] for r in self._all(
            "SELECT text FROM commitments WHERE call_id = ? ORDER BY id", [call_id])]

    # ---- contacts, groups, rules --------------------------------------------
    def list_contacts(self) -> list[dict]:
        return self._all("SELECT c.*, g.name AS group_name FROM contacts c "
                         "LEFT JOIN groups g ON g.id = c.group_id ORDER BY c.name")

    def get_contact(self, contact_id: int) -> dict | None:
        return self._one("SELECT c.*, g.name AS group_name FROM contacts c "
                         "LEFT JOIN groups g ON g.id = c.group_id WHERE c.id = ?", [contact_id])

    def contact_by_number(self, number: str) -> dict | None:
        return self._one("SELECT c.*, g.name AS group_name FROM contacts c "
                         "LEFT JOIN groups g ON g.id = c.group_id WHERE c.number = ?", [number])

    def add_contact(self, name: str, number: str, group_id: int | None,
                    language_hint: str | None = None, notes: str = "") -> int:
        cur = self._exec("INSERT INTO contacts(name, number, group_id, language_hint, notes) "
                         "VALUES (?,?,?,?,?)", [name, number, group_id, language_hint, notes])
        return int(cur.lastrowid)

    def update_contact(self, contact_id: int, **fields: Any) -> None:
        allowed = {"name", "number", "group_id", "language_hint", "notes"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self._exec(f"UPDATE contacts SET {sets} WHERE id = ?", [*fields.values(), contact_id])

    def delete_contact(self, contact_id: int) -> bool:
        return self._exec("DELETE FROM contacts WHERE id = ?", [contact_id]).rowcount > 0

    def list_groups(self) -> list[dict]:
        return self._all("SELECT g.*, r.name AS rule_name FROM groups g "
                         "LEFT JOIN rules r ON r.id = g.rule_id ORDER BY g.id")

    def get_group(self, group_id: int) -> dict | None:
        return self._one("SELECT * FROM groups WHERE id = ?", [group_id])

    def group_by_name(self, name: str) -> dict | None:
        return self._one("SELECT * FROM groups WHERE name = ?", [name])

    def set_group_rule(self, group_id: int, rule_id: int) -> None:
        self._exec("UPDATE groups SET rule_id = ? WHERE id = ?", [rule_id, group_id])

    def list_rules(self) -> list[dict]:
        return self._all("SELECT * FROM rules ORDER BY id")

    def get_rule(self, rule_id: int) -> dict | None:
        return self._one("SELECT * FROM rules WHERE id = ?", [rule_id])

    def update_rule(self, rule_id: int, **fields: Any) -> None:
        allowed = {"name", "action", "language_hint", "extra_prompt", "max_seconds",
                   "severity_floor"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self._exec(f"UPDATE rules SET {sets} WHERE id = ?", [*fields.values(), rule_id])

    # ---- SMS outbox ----------------------------------------------------------
    def queue_sms(self, to_number: str, text: str) -> int:
        cur = self._exec("INSERT INTO sms_outbox(to_number, text, created_at) VALUES (?,?,?)",
                         [to_number, text, now()])
        return int(cur.lastrowid)

    def pending_sms(self) -> list[dict]:
        return self._all("SELECT * FROM sms_outbox WHERE ok IS NULL ORDER BY id")

    def get_sms(self, sms_id: int) -> dict | None:
        return self._one("SELECT * FROM sms_outbox WHERE id = ?", [sms_id])

    def mark_sms_attempt(self, sms_id: int) -> None:
        self._exec("UPDATE sms_outbox SET sent_at = ? WHERE id = ?", [now(), sms_id])

    def mark_sms(self, sms_id: int, ok: bool) -> None:
        self._exec("UPDATE sms_outbox SET sent_at = ?, ok = ? WHERE id = ?",
                   [now(), 1 if ok else 0, sms_id])

    # ---- health --------------------------------------------------------------
    def add_health(self, registered: bool, rssi: int, sd_free_mb: int, on_battery: bool,
                   ts: int | None = None) -> None:
        self._exec("INSERT INTO health(ts, registered, rssi, sd_free_mb, on_battery) "
                   "VALUES (?,?,?,?,?)",
                   [ts or now(), int(registered), rssi, sd_free_mb, int(on_battery)])
        # Keep about a month of five-minute reports.
        self._exec("DELETE FROM health WHERE ts < ?", [now() - 31 * 86400])

    def last_health(self) -> dict | None:
        row = self._one("SELECT * FROM health ORDER BY ts DESC LIMIT 1")
        if row:
            row["registered"] = bool(row["registered"])
            row["on_battery"] = bool(row["on_battery"])
        return row

    # ---- events --------------------------------------------------------------
    def add_event(self, kind: str, call_id: str | None = None) -> int:
        cur = self._exec("INSERT INTO events(ts, kind, call_id) VALUES (?,?,?)",
                         [now(), kind, call_id])
        return int(cur.lastrowid)

    def events_after(self, seq: int, limit: int = 100) -> list[dict]:
        return self._all("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", [seq, limit])

    def last_event_seq(self) -> int:
        row = self._one("SELECT MAX(seq) AS s FROM events")
        return int(row["s"] or 0) if row else 0

    # ---- settings ------------------------------------------------------------
    def get_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self._all("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: Any) -> None:
        self._exec("INSERT INTO settings(key, value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", [key, str(value)])


def _decode_call(row: dict) -> dict:
    row["flags"] = json.loads(row.get("flags") or "[]")
    return row
