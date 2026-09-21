"""Migration 006: request_log.tokens_in_cached — 缓存命中 token 计数 (v6.1).

tokens_in 口径修正为「总 prompt（含缓存命中部分）」后，需要单独保存
命中前缀缓存的 token 数，才能计算缓存命中率（cached / in，双协议统一）。
历史行默认 0（当时未记录缓存字段，无法回填）。
"""


def upgrade(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(request_log)").fetchall()]
    if "tokens_in_cached" not in cols:
        conn.execute(
            "ALTER TABLE request_log "
            "ADD COLUMN tokens_in_cached INTEGER NOT NULL DEFAULT 0 "
            "CHECK (tokens_in_cached >= 0)"
        )
    conn.commit()
