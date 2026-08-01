"""request_log.db 独立数据库管理器 — v4.6.2

与 state.db 物理隔离，管理请求日志的持久化。
线程安全（threading.Lock 保护连接）。

PRAGMA 策略:
- journal_mode=WAL: 并发读写 + 崩溃恢复
- synchronous=NORMAL: WAL 下安全与性能平衡
- auto_vacuum=INCREMENTAL: prune 后回收空间
- wal_autocheckpoint=1000: 自动 checkpoint
"""

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("inferfabric.request_log_db")


class RequestLogDB:
    """request_log.db 独立数据库管理器。线程安全。"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._columns: list[str] | None = None
        self._init()

    def _init(self):
        """创建表 + 索引 + PRAGMA 设置。"""
        db_path = self._db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("""CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            req_id TEXT NOT NULL UNIQUE,
            key_name TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL,
            status INTEGER NOT NULL CHECK (status BETWEEN 100 AND 599),
            ttft_ms REAL CHECK (ttft_ms IS NULL OR ttft_ms >= 0),
            tokens_in INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
            tokens_out INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
            duration_ms REAL NOT NULL DEFAULT 0.0 CHECK (duration_ms >= 0),
            route TEXT NOT NULL DEFAULT 'local',
            cloud_provider TEXT,
            error TEXT,
            timestamp REAL NOT NULL CHECK (timestamp > 0),
            ts TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_request_log_timestamp ON request_log (timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_request_log_model_ts ON request_log (model, timestamp)")
        conn.commit()

        # auto_vacuum 检测：对已有 DB 无效，仅首次创建生效
        av = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        if av != 2:  # 2 = INCREMENTAL
            log.warning("auto_vacuum=%d (expected INCREMENTAL=2); "
                        "fragment reclamation may not work on existing DB. "
                        "Recreate DB or run VACUUM to apply.", av)

        # 缓存列名
        self._columns = [d[0] for d in conn.execute("SELECT * FROM request_log LIMIT 0").description]
        self._conn = conn

    @property
    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            self._init()
        return self._conn

    @property
    def columns(self) -> list[str]:
        """返回 request_log 表的列名列表。"""
        if self._columns is None:
            self._columns = [d[0] for d in self._c.execute("SELECT * FROM request_log LIMIT 0").description]
        return self._columns

    def insert_request_log(self, entries: list[dict]):
        """批量插入 request_log 记录。INSERT OR IGNORE 防重复。线程安全。"""
        if not entries:
            return
        with self._lock:
            self._c.executemany(
                "INSERT OR IGNORE INTO request_log "
                "(req_id, key_name, model, status, ttft_ms, tokens_in, tokens_out, "
                "duration_ms, route, cloud_provider, error, timestamp, ts) "
                "VALUES (:req_id, :key_name, :model, :status, :ttft_ms, :tokens_in, "
                ":tokens_out, :duration_ms, :route, :cloud_provider, :error, :timestamp, :ts)",
                entries,
            )
            self._c.commit()

    def query_request_log(self, since: float, until: float | None = None,
                          model: str | None = None, limit: int = 100000) -> list[dict]:
        """查询 request_log，返回 dict 列表。用于 MetricsAggregator 回填。线程安全。"""
        with self._lock:
            sql = "SELECT * FROM request_log WHERE timestamp >= ?"
            params: list[Any] = [since]
            if until is not None:
                sql += " AND timestamp < ?"
                params.append(until)
            if model is not None:
                sql += " AND model = ?"
                params.append(model)
            sql += " ORDER BY timestamp ASC LIMIT ?"
            params.append(limit)
            rows = self._c.execute(sql, params).fetchall()
            cols = self.columns
            return [dict(zip(cols, row)) for row in rows]

    def prune_request_log(self, before: float) -> int:
        """删除 timestamp < before 的记录，返回删除行数。执行 incremental_vacuum。线程安全。"""
        with self._lock:
            cur = self._c.execute("DELETE FROM request_log WHERE timestamp < ?", (before,))
            self._c.commit()
            deleted = cur.rowcount
            if deleted > 0:
                try:
                    self._c.execute("PRAGMA incremental_vacuum")
                except Exception:
                    pass
            return deleted

    def checkpoint(self):
        """WAL checkpoint TRUNCATE。线程安全。"""
        with self._lock:
            try:
                self._c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass

    def close(self):
        """关闭连接。先 checkpoint 再 close。线程安全。"""
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
