"""
edge_llm/state.py — State machine + SQLite state management.

Extracted from profile_manager.py (v3.0 → v3.1 refactoring).
"""

import sqlite3
import threading
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("edge_llm")


# ─── State Machine ────────────────────────────────────────────────

class ProfileState:
    """Valid profile states for state.db."""
    SWITCHING = "switching"
    HEALTHY = "healthy"
    IDLE = "idle"
    ERROR = "error"

    @classmethod
    def is_active(cls, state: str) -> bool:
        return state in (cls.SWITCHING, cls.HEALTHY, cls.ERROR)


# ─── State Manager ─────────────────────────────────────────────────

class StateDB:
    """Thread-safe SQLite — fresh connection per call (WAL mode)."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        c = sqlite3.connect(str(self._db_path), timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init(self):
        with self._lock:
            c = self._conn()
            c.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
            c.execute(
                "CREATE TABLE IF NOT EXISTS history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "timestamp TEXT DEFAULT CURRENT_TIMESTAMP, "
                "from_profile TEXT, to_profile TEXT, duration REAL, status TEXT)"
            )
            # Migration: add status column if missing (from v2 schema)
            try:
                c.execute("SELECT status FROM history LIMIT 1")
            except sqlite3.OperationalError:
                log.info("Migrating history table: adding status column")
                c.execute("ALTER TABLE history ADD COLUMN status TEXT DEFAULT 'ok'")
            # Ensure default state keys exist
            c.execute("INSERT OR IGNORE INTO state VALUES ('current_profile', 'idle')")
            c.execute("INSERT OR IGNORE INTO state VALUES ('profile_state', 'idle')")
            c.execute("INSERT OR IGNORE INTO state VALUES ('vllm_pid', '')")
            c.execute("INSERT OR IGNORE INTO state VALUES ('comfyui_pid', '')")
            c.commit()
            c.close()

    def get(self, key: str) -> Optional[str]:
        c = self._conn()
        try:
            row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            c.close()

    def set(self, key: str, value: str):
        with self._lock:
            c = self._conn()
            try:
                c.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))
                c.commit()
            finally:
                c.close()

    def set_multi(self, kv: dict[str, str]):
        """Atomically set multiple state keys."""
        with self._lock:
            c = self._conn()
            try:
                for k, v in kv.items():
                    c.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (k, v))
                c.commit()
            finally:
                c.close()

    def add_history(self, from_profile: str, to_profile: str, duration: float, status: str = "ok"):
        with self._lock:
            c = self._conn()
            try:
                c.execute(
                    "INSERT INTO history (from_profile, to_profile, duration, status) VALUES (?, ?, ?, ?)",
                    (from_profile, to_profile, duration, status),
                )
                c.execute(
                    "DELETE FROM history WHERE id NOT IN "
                    "(SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                    (100,),
                )
                c.commit()
            finally:
                c.close()

    def get_history(self, limit: int = 20) -> list[dict]:
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT timestamp, from_profile, to_profile, duration, status "
                "FROM history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"timestamp": r[0], "from": r[1], "to": r[2], "duration": r[3], "status": r[4]}
                for r in rows
            ]
        finally:
            c.close()
