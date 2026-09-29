"""unit/config/test_migration_v008.py — request_log 增列 agent/ua 迁移测试。"""
import sqlite3
from inferfabric.db import IFFDB, REQUEST_LOG_DB


def _migrate(tmp_path):
    db = IFFDB(tmp_path)
    import inferfabric.migrations  # noqa: F401 — 注册迁移
    db._run_migrations()
    return db


def test_v008_adds_columns(tmp_path):
    db = _migrate(tmp_path)
    with db.connect(REQUEST_LOG_DB) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(request_log)").fetchall()]
    assert "agent" in cols and "ua" in cols
    db.close()


def test_v008_idempotent_on_existing(tmp_path):
    """旧库升级：已存在该列不应报错；历史行 v008 默认 ''，v009 回填 claude-code。"""
    import sqlite3
    p = tmp_path / "request_log.db"
    conn = sqlite3.connect(p)
    conn.execute("""CREATE TABLE request_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, req_id TEXT NOT NULL UNIQUE,
        key_name TEXT NOT NULL DEFAULT '', model TEXT NOT NULL,
        status INTEGER NOT NULL, ttft_ms REAL, tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0, duration_ms REAL NOT NULL DEFAULT 0.0,
        route TEXT NOT NULL DEFAULT 'local', cloud_provider TEXT, error TEXT,
        timestamp REAL NOT NULL, ts TEXT NOT NULL DEFAULT '',
        cost TEXT DEFAULT 'local', metadata TEXT DEFAULT '{}')""")
    conn.execute("PRAGMA user_version = 6")
    conn.execute("INSERT INTO request_log (req_id, model, status, timestamp) "
                 "VALUES ('r1', 'm', 200, 1.0)")
    conn.commit(); conn.close()
    db = _migrate(tmp_path)
    with db.connect(REQUEST_LOG_DB) as conn:
        row = conn.execute("SELECT agent, ua FROM request_log WHERE req_id='r1'").fetchone()
    # v008 加列默认 ''；v009 回填历史 agent='' → 'claude-code'
    assert row[0] == "claude-code" and row[1] == ""
    db.close()
