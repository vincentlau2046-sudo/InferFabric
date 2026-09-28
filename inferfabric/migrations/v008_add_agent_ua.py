"""Migration 008: request_log 增列 agent/ua — 客户端 Agent 识别（v6.5）。

agent = agents.d 归一化的 Agent id（unknown 兜底）；ua = 原始 User-Agent。
历史行默认为 ''（当时未记录，聚合层统一 '' → unknown）。
"""


def upgrade(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(request_log)").fetchall()]
    if "agent" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN agent TEXT NOT NULL DEFAULT ''")
    if "ua" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN ua TEXT NOT NULL DEFAULT ''")
    conn.commit()