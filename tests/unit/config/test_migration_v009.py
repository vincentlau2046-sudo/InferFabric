"""unit/config/test_migration_v009.py — 历史行 agent='' 回填 claude-code。

个人单 GPU 工作站：采集功能上线前的 14 万行历史（agent='' 且 ua=''）
实际由 Claude Code 发起。回填 claude-code 让历史数据在 agent 卡可见，
而非被聚合静默跳过。迁移幂等：重跑只影响仍为 '' 的行。
"""
import sqlite3
from inferfabric.db import IFFDB, REQUEST_LOG_DB


def _migrate(tmp_path):
    db = IFFDB(tmp_path)
    import inferfabric.migrations  # noqa: F401
    db._run_migrations()
    return db


def _seed_old_request_log(tmp_path, rows):
    """直接建一个 user_version=8 的旧库（v009 前），插历史行。"""
    p = tmp_path / "request_log.db"
    conn = sqlite3.connect(p)
    conn.execute("""CREATE TABLE request_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, req_id TEXT NOT NULL UNIQUE,
        key_name TEXT NOT NULL DEFAULT '', model TEXT NOT NULL,
        status INTEGER NOT NULL, ttft_ms REAL, tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0, duration_ms REAL NOT NULL DEFAULT 0.0,
        route TEXT NOT NULL DEFAULT 'local', cloud_provider TEXT, error TEXT,
        timestamp REAL NOT NULL, ts TEXT NOT NULL DEFAULT '',
        cost TEXT DEFAULT 'local', metadata TEXT DEFAULT '{}',
        tokens_in_cached INTEGER DEFAULT 0,
        agent TEXT NOT NULL DEFAULT '', ua TEXT NOT NULL DEFAULT '')""")
    conn.execute("PRAGMA user_version = 8")
    for r in rows:
        conn.execute(
            "INSERT INTO request_log (req_id, model, status, timestamp, agent, ua) "
            "VALUES (?,?,?,?,?,?)", r)
    conn.commit(); conn.close()


def test_v009_backfills_empty_agent_to_claude_code(tmp_path):
    """v009 把 agent='' → claude-code（后被 v010 诚实化修正）。
    本测试钉 v009 单步行为：跑完 v009（user_version=9）后 agent='' 消失。
    全迁移链下 v010 会进一步修正无 ua 行 → historical。"""
    _seed_old_request_log(tmp_path, [
        ("r1", "m", 200, 1.0, "", ""),              # 历史无 agent
        ("r2", "m", 200, 2.0, "", "curl/8"),        # 有 ua 但 agent 空
        ("r3", "m", 200, 3.0, "claude-code", "cc"), # 已有 agent 不动
        ("r4", "m", 200, 4.0, "unknown", "x"),      # unknown 不动
    ])
    db = _migrate(tmp_path)
    with db.connect(REQUEST_LOG_DB) as conn:
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT req_id, agent FROM request_log").fetchall()}
    # 全迁移链：v009 回填 → v010 诚实化
    # r1 无 ua → historical；r2 有 ua 非 claude-cli → unknown（待 reclassify）
    assert rows["r1"] == "historical"
    assert rows["r2"] == "unknown"
    assert rows["r3"] == "unknown"   # r3 ua="cc" 非 claude-cli → v010 改 unknown
    assert rows["r4"] == "unknown"
    db.close()


def test_v009_idempotent(tmp_path):
    """重跑全迁移不报错、结果稳定。"""
    _seed_old_request_log(tmp_path, [("r1", "m", 200, 1.0, "", "")])
    db = _migrate(tmp_path)
    db._run_migrations()
    with db.connect(REQUEST_LOG_DB) as conn:
        row = conn.execute("SELECT agent FROM request_log WHERE req_id='r1'").fetchone()
    assert row[0] == "historical"  # v010 诚实化后无 ua → historical
    db.close()


def test_v009_empty_db_no_error(tmp_path):
    """新装空库跑 v009 无副作用。"""
    db = _migrate(tmp_path)
    with db.connect(REQUEST_LOG_DB) as conn:
        n = conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
    assert n == 0
    db.close()
