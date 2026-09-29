"""unit/config/test_migration_v010.py — 诚实化历史数据 + 预留 x_app 列。

v009 曾把 agent='' 全部回填 claude-code（基于 97% 分布的推断，不诚实）。
v010 修正：
  - 无 ua 的历史行（agent='claude-code' AND ua=''）→ 'historical'（无客户端信号）
  - 有 ua 但非 claude-cli 的行（v009 误归）→ 'unknown'（待 reclassify 修正）
  - 真正 claude-cli UA 的行 → 保留 claude-code
同时加 x_app 列（事实层 header 信号，reclassify 与未来智能化的原料）。
"""
import sqlite3
from inferfabric.db import IFFDB, REQUEST_LOG_DB


def _migrate(tmp_path):
    db = IFFDB(tmp_path)
    import inferfabric.migrations  # noqa: F401
    db._run_migrations()
    return db


def _seed_v009_state(tmp_path, rows):
    """user_version=9 的库（v009 后状态），rows=(req_id, agent, ua)。"""
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
    conn.execute("PRAGMA user_version = 9")
    for rid, agent, ua in rows:
        conn.execute("INSERT INTO request_log (req_id, model, status, timestamp, agent, ua) "
                     "VALUES (?,?,200,1.0,?,?)", (rid, "m", agent, ua))
    conn.commit(); conn.close()


def test_v010_adds_x_app_column(tmp_path):
    _seed_v009_state(tmp_path, [])
    db = _migrate(tmp_path)
    with db.connect(REQUEST_LOG_DB) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(request_log)").fetchall()]
    assert "x_app" in cols
    db.close()


def test_v010_honestizes_history(tmp_path):
    """无 ua 历史行 → historical；有 ua 非 claude-cli → unknown；真 claude-cli → 保留。"""
    _seed_v009_state(tmp_path, [
        ("r1", "claude-code", ""),                                    # 无 ua → historical
        ("r2", "claude-code", "claude-cli/2.1.283 (external, cli)"), # 真 claude-code
        ("r3", "claude-code", "curl/8.5.2"),                          # v009 误归 → unknown
        ("r4", "claude-code", "Python-urllib/3.13"),                  # v009 误归 → unknown
        ("r5", "deepseek-harness", "deepseek-harness/0.1"),           # 已认领不动
        ("r6", "unknown", "curl/8.5.2"),                              # 已 unknown 不动
    ])
    db = _migrate(tmp_path)
    with db.connect(REQUEST_LOG_DB) as conn:
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT req_id, agent FROM request_log").fetchall()}
    assert rows["r1"] == "historical"
    assert rows["r2"] == "claude-code"
    assert rows["r3"] == "unknown"
    assert rows["r4"] == "unknown"
    assert rows["r5"] == "deepseek-harness"
    assert rows["r6"] == "unknown"
    db.close()


def test_v010_idempotent(tmp_path):
    _seed_v009_state(tmp_path, [("r1", "claude-code", "")])
    db = _migrate(tmp_path)
    db._run_migrations()  # 重跑
    with db.connect(REQUEST_LOG_DB) as conn:
        assert conn.execute("SELECT agent FROM request_log WHERE req_id='r1'").fetchone()[0] == "historical"
    db.close()
