"""Migration 009: 历史行 agent='' 回填 'claude-code'（v6.6）。

采集功能（v6.5）上线前的历史行 agent/ua 均为 ''。个人单 GPU 工作站这些
请求实际由 Claude Code 发起（采集上线后真实分布 claude-code 占 ~97%）。
回填 claude-code 让历史数据在客户端 Agent 卡可见，而非被聚合静默跳过。

幂等：仅影响 agent='' 的行；重跑无副作用（已回填的行 agent 不为 ''）。
"""


def upgrade(conn):
    # 防御：request_log 表不存在（极端新装/损坏）时跳过
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "request_log" not in tables:
        return
    n = conn.execute(
        "UPDATE request_log SET agent='claude-code' WHERE agent=''").rowcount
    conn.commit()
    if n:
        import logging
        logging.getLogger("inferfabric.migrations").info(
            "v009: backfilled %d historical rows (agent='' → 'claude-code')", n)
