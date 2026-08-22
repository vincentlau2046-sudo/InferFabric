"""
inferfabric/db.py — Unified database abstraction.

Physical DB isolation preserved (state.db vs request_log.db),
logical API unified. Migration engine via PRAGMA user_version.

Connection strategy:
  - Per-thread connection via threading.local (ConnectionPool)
  - WAL journal mode for both DBs
  - Write lock serializes concurrent writers (avoids "database is locked")
  - busy_timeout=5000 as safety net
  - Automatic reconnect on OperationalError (stale connection)
"""

from __future__ import annotations
import json
import sqlite3
import threading
import logging
from pathlib import Path
from typing import Any
from contextlib import contextmanager

log = logging.getLogger("inferfabric.db")

STATE_DB = "state"
REQUEST_LOG_DB = "request_log"

_DB_PATHS: dict[str, Path] = {}


def set_db_paths(data_dir: Path):
    _DB_PATHS[STATE_DB] = data_dir / "state.db"
    _DB_PATHS[REQUEST_LOG_DB] = data_dir / "request_log.db"


class ConnectionPool:
    """Per-thread SQLite connection pool."""

    def __init__(self, db_path: Path, pragma_setup: list[str] | None = None):
        self._db_path = db_path
        self._local = threading.local()
        self._pragma_setup = pragma_setup or ["PRAGMA journal_mode=WAL"]

    def get(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.OperationalError:
                log.debug("Stale connection to %s, reconnecting", self._db_path)
                try:
                    conn.close()
                except Exception as e:
                    log.warning("DB init failed: %s", e)
                self._local.conn = None
        conn = sqlite3.connect(str(self._db_path), timeout=10, check_same_thread=True)
        for pragma in self._pragma_setup:
            conn.execute(pragma)
        self._local.conn = conn
        return conn

    def close_all(self):
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e:
                log.warning("DB connection failed: %s", e)
            try:
                conn.close()
            except Exception as e:
                log.warning("DB operation failed: %s", e)
            self._local.conn = None

    @property
    def db_path(self) -> Path:
        return self._db_path


class IFFDB:
    """IFF database facade: typed API for state + request_log."""

    _MIGRATIONS: list[tuple[int, str, str]] = []

    def __init__(self, data_dir: Path | None = None):
        if data_dir is not None:
            set_db_paths(data_dir)
        self._pools: dict[str, ConnectionPool] = {}
        self._write_lock = threading.Lock()
        self._init_pools()
        import inferfabric.migrations  # noqa: F401 — ensure migration modules are loaded
        self._run_migrations()

    def _init_pools(self):
        self._pools[STATE_DB] = ConnectionPool(
            _DB_PATHS.get(STATE_DB, Path.home() / ".inferfabric" / "state.db"),
            pragma_setup=[
                "PRAGMA journal_mode=WAL",
                "PRAGMA busy_timeout=5000",
            ],
        )
        self._pools[REQUEST_LOG_DB] = ConnectionPool(
            _DB_PATHS.get(REQUEST_LOG_DB, Path.home() / ".inferfabric" / "request_log.db"),
            pragma_setup=[
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=NORMAL",
                "PRAGMA auto_vacuum=INCREMENTAL",
                "PRAGMA wal_autocheckpoint=1000",
                "PRAGMA busy_timeout=5000",
            ],
        )

    @contextmanager
    def connect(self, db_name: str) -> sqlite3.Connection:
        pool = self._pools.get(db_name)
        if not pool:
            raise KeyError(f"Unknown DB: {db_name}")
        yield pool.get()

    def close(self):
        for pool in self._pools.values():
            pool.close_all()

    @classmethod
    def register_migration(cls, version: int, db_name: str, description: str):
        cls._MIGRATIONS.append((version, db_name, description))
        cls._MIGRATIONS.sort(key=lambda x: x[0])

    def _run_migrations(self):
        grouped: dict[str, list[tuple[int, str]]] = {}
        for version, db_name, desc in self._MIGRATIONS:
            grouped.setdefault(db_name, []).append((version, desc))
        for db_name, migrations in grouped.items():
            for version, desc in migrations:
                with self.connect(db_name) as conn:
                    current = conn.execute("PRAGMA user_version").fetchone()[0] or 0
                    if current < version:
                        self._apply_migration(conn, version, desc, db_name)
                        conn.execute(f"PRAGMA user_version = {version}")
                        log.info("Migration %03d '%s' applied to %s", version, desc, db_name)

    def _apply_migration(self, conn: sqlite3.Connection,
                         version: int, description: str, db_name: str):
        from importlib import import_module
        safe_name = description.replace(" ", "_").replace("-", "_")
        mod = import_module(f"inferfabric.migrations.v{version:03d}_{safe_name}")
        mod.upgrade(conn)

    # ── State: low-level ───────────────────────────────────────

    def _get_raw(self, key: str) -> str | None:
        with self.connect(STATE_DB) as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def _set_raw(self, key: str, value: str):
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))
                conn.commit()

    # ── State: typed API ───────────────────────────────────────

    def get_gpu_mode(self) -> str:
        r = self._get_raw("gpu_mode")
        return r if r else "idle"

    def set_gpu_mode(self, mode: str):
        self._set_raw("gpu_mode", mode)

    def get_active_services(self) -> list[str]:
        r = self._get_raw("active_services")
        if not r:
            return []
        return json.loads(r)

    def set_active_services(self, services: list[str]):
        self._set_raw("active_services", json.dumps(services))

    def add_active_service(self, name: str):
        svc = self.get_active_services()
        if name not in svc:
            svc.append(name)
            self.set_active_services(svc)

    def remove_active_service(self, name: str):
        svc = self.get_active_services()
        if name in svc:
            svc.remove(name)
            self.set_active_services(svc)

    # ── State: sleep state (v5.2: table-backed) ───────────────

    def get_sleep_state(self, model: str) -> str | None:
        with self.connect(STATE_DB) as conn:
            row = conn.execute(
                "SELECT level FROM sleep_state WHERE model=?", (model,)
            ).fetchone()
            return row[0] if row else None

    def set_sleep_state(self, model: str, level: int | None):
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                if level is None:
                    conn.execute("DELETE FROM sleep_state WHERE model=?", (model,))
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO sleep_state (model, level) VALUES (?, ?)",
                        (model, f"l{level}"),
                    )
                conn.commit()

    def get_all_sleep_states(self) -> dict[str, str]:
        with self.connect(STATE_DB) as conn:
            rows = conn.execute("SELECT model, level FROM sleep_state").fetchall()
            return {r[0]: r[1] for r in rows}

    def get_current_profile(self) -> str:
        r = self._get_raw("current_profile")
        return r if r else ""

    def set_current_profile(self, profile: str):
        self._set_raw("current_profile", profile)

    def get_switch_target(self) -> str:
        r = self._get_raw("switch_target")
        return r if r else ""

    def set_switch_target(self, target: str):
        self._set_raw("switch_target", target)

    def get_vllm_pid(self) -> int | None:
        r = self._get_raw("vllm_pid")
        return int(r) if r else None

    def set_vllm_pid(self, pid: int | None):
        self._set_raw("vllm_pid", str(pid) if pid is not None else "")

    def get_comfyui_pid(self) -> int | None:
        r = self._get_raw("comfyui_pid")
        return int(r) if r else None

    def set_comfyui_pid(self, pid: int | None):
        self._set_raw("comfyui_pid", str(pid) if pid is not None else "")

    # ── State: history ─────────────────────────────────────────

    def add_switch_history(self, from_profile: str, to_profile: str,
                           duration: float, status: str = "ok"):
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                conn.execute(
                    "INSERT INTO history (from_profile, to_profile, duration, status) "
                    "VALUES (?, ?, ?, ?)",
                    (from_profile, to_profile, duration, status),
                )
                conn.execute(
                    "DELETE FROM history WHERE id NOT IN "
                    "(SELECT id FROM history ORDER BY id DESC LIMIT 100)"
                )
                conn.commit()

    def get_switch_history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect(STATE_DB) as conn:
            rows = conn.execute(
                "SELECT timestamp, from_profile, to_profile, duration, status "
                "FROM history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"timestamp": r[0], "from": r[1], "to": r[2],
                 "duration": r[3], "status": r[4]}
                for r in rows
            ]

    # ── State: manual stops (v5.2: table-backed) ──────────────

    MANUAL_STOP_TTL: float = 600.0  # 10 min (align with StateDB)

    def record_manual_stop(self, name: str):
        import time
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO manual_stops (model, stop_ts) VALUES (?, ?)",
                    (name, time.time()),
                )
                conn.commit()

    def is_manually_stopped(self, name: str) -> bool:
        import time
        now = time.time()
        with self.connect(STATE_DB) as conn:
            row = conn.execute(
                "SELECT stop_ts FROM manual_stops WHERE model=? AND stop_ts > ?",
                (name, now - self.MANUAL_STOP_TTL),
            ).fetchone()
            return row is not None

    def clear_manual_stop(self, name: str):
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                conn.execute("DELETE FROM manual_stops WHERE model=?", (name,))
                conn.commit()

    def get_all_manual_stops(self) -> dict[str, float]:
        with self.connect(STATE_DB) as conn:
            rows = conn.execute("SELECT model, stop_ts FROM manual_stops").fetchall()
            return {r[0]: r[1] for r in rows}

    # ── Request Log API ───────────────────────────────────────

    def insert_request_log(self, entries: list[dict]):
        if not entries:
            return
        with self._write_lock:
            with self.connect(REQUEST_LOG_DB) as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO request_log "
                    "(req_id, key_name, model, status, ttft_ms, tokens_in, tokens_out, "
                    " duration_ms, route, cloud_provider, error, timestamp, ts) "
                    "VALUES (:req_id, :key_name, :model, :status, :ttft_ms, :tokens_in, "
                    ":tokens_out, :duration_ms, :route, :cloud_provider, :error, "
                    ":timestamp, :ts)",
                    entries,
                )
                conn.commit()

    def query_request_log(self, since: float, until: float | None = None,
                          model: str | None = None, limit: int = 10000) -> list[dict]:
        with self.connect(REQUEST_LOG_DB) as conn:
            cols = [d[0] for d in conn.execute("SELECT * FROM request_log LIMIT 0").description]
            sql = "SELECT * FROM request_log WHERE timestamp > ?"
            params: list[Any] = [since]
            if until is not None:
                sql += " AND timestamp < ?"
                params.append(until)
            if model is not None:
                sql += " AND model = ?"
                params.append(model)
            sql += " ORDER BY timestamp ASC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(zip(cols, r)) for r in rows]

    def prune_request_log(self, before: float) -> int:
        with self._write_lock:
            with self.connect(REQUEST_LOG_DB) as conn:
                cur = conn.execute("DELETE FROM request_log WHERE timestamp < ?", (before,))
                conn.commit()
                deleted = cur.rowcount
                if deleted > 0:
                    try:
                        conn.execute("PRAGMA auto_vacuum")
                    except Exception as e:
                        log.warning("DB query failed: %s", e)
                return deleted

    def wal_checkpoint(self):
        with self.connect(REQUEST_LOG_DB) as conn:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e:
                log.warning("DB write failed: %s", e)

    # ── Legacy backward compat ─────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        r = self._get_raw(key)
        return r if r is not None else default

    def set(self, key: str, value: str):
        self._set_raw(key, value)
