"""request_log.db 数据库管理器 — IFFDB-delegated (v5.0)

与 state.db 物理隔离，管理请求日志的持久化。
保持 v4.x 公共 API 接口不变。

线程安全委托至 IFFDB.insert_request_log / query_request_log / prune_request_log。
"""

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("inferfabric.request_log_db")


class RequestLogDB:
    """request_log.db 数据库管理器。IFFDB-delegated。

    保持 v4.x 相同公共 API (insert_request_log / query_request_log /
    prune_request_log / checkpoint / close) 以保证后向兼容。
    实际数据操作通过 IFFDB 完成（物理 DB 由 ConnectionPool 管理）。
    """

    def __init__(self, db_path: Path | None = None, iffdb=None):
        if iffdb is not None:
            self._iffdb = iffdb
        elif db_path is not None:
            from inferfabric.db import IFFDB
            import inferfabric.migrations  # noqa: F401
            self._iffdb = IFFDB(db_path.parent)
            # Legacy compat: remap request_log pool to exact db_path
            from inferfabric.db import REQUEST_LOG_DB, ConnectionPool
            self._iffdb._pools[REQUEST_LOG_DB] = ConnectionPool(
                Path(db_path),
                pragma_setup=[
                    "PRAGMA journal_mode=WAL",
                    "PRAGMA synchronous=NORMAL",
                    "PRAGMA auto_vacuum=INCREMENTAL",
                    "PRAGMA wal_autocheckpoint=1000",
                    "PRAGMA busy_timeout=5000",
                ],
            )
            # Re-run migration for request_log on the exact pool
            for version, db_name, desc in IFFDB._MIGRATIONS:
                if db_name == REQUEST_LOG_DB:
                    with self._iffdb.connect(REQUEST_LOG_DB) as conn:
                        current = conn.execute("PRAGMA user_version").fetchone()[0] or 0
                        if current < version:
                            self._iffdb._apply_migration(conn, version, desc, db_name)
                            conn.execute(f"PRAGMA user_version = {version}")
        else:
            raise ValueError("RequestLogDB requires db_path or iffdb")
        # v4.x compat: lock + connection accessor
        import threading, sqlite3
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if db_path is not None:
            self._conn = sqlite3.connect(str(Path(db_path)), timeout=10)
            self._conn.row_factory = sqlite3.Row
        # Cache column names (compat property)
        self._columns: list[str] | None = None

    @property
    def columns(self) -> list[str]:
        """返回 request_log 表的列名列表。"""
        if self._columns is None:
            self._columns = [
                "id", "req_id", "key_name", "model", "status",
                "ttft_ms", "tokens_in", "tokens_out", "duration_ms",
                "route", "cloud_provider", "error", "timestamp", "ts",
            ]
        return self._columns

    @property
    def _c(self):
        """v4.x compat: direct connection for test PRAGMA access."""
        if self._conn is None:
            raise RuntimeError("Connection closed")
        return self._conn

    def insert_request_log(self, entries: list[dict]):
        """批量插入 request_log 记录。INSERT OR IGNORE 防重复。线程安全。"""
        if not entries:
            return
        self._iffdb.insert_request_log(entries)

    def query_request_log(self, since: float, until: float | None = None,
                          model: str | None = None, limit: int = 100000) -> list[dict]:
        """查询 request_log，返回 dict 列表。线程安全。"""
        return self._iffdb.query_request_log(since=since, until=until,
                                             model=model, limit=limit)

    def prune_request_log(self, before: float) -> int:
        """删除 timestamp < before 的记录，返回删除行数。"""
        return self._iffdb.prune_request_log(before)

    def checkpoint(self):
        """WAL checkpoint TRUNCATE。"""
        self._iffdb.wal_checkpoint()

    def close(self):
        """关闭连接。先 checkpoint 再 close。"""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._iffdb.close()