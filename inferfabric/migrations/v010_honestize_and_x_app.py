"""Migration 010: 诚实化历史数据 + 预留 x_app 列（v6.6 架构修正）。

v009 曾把 agent='' 全部回填 claude-code（基于 97% 分布的推断）——不诚实。
v010 修正为事实层/派生层分离：
  - 无 ua 的历史行（agent='claude-code' AND ua=''）→ 'historical'
    （采集功能上线前的存量，无客户端信号，不可认领；token/cost 仍有统计意义）
  - 有 ua 但非 claude-cli 的行（v009 误归）→ 'unknown'
    （待 reclassify 用当前 registry 重新分类）
  - 真正 claude-cli UA 的行 → 保留 claude-code

同时加 x_app 列：事实层存储原始 x-app header 信号（reclassify 正确性的前提
+ 未来智能化原料）。sdk_lang/sdk_ver 等留待后续按需添加。

幂等：仅影响 agent='claude-code' 且 ua 匹配条件的行；重跑无副作用。
"""
import logging

log = logging.getLogger("inferfabric.migrations")


def upgrade(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "request_log" not in tables:
        return

    # 加 x_app 列（幂等）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(request_log)").fetchall()]
    if "x_app" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN x_app TEXT NOT NULL DEFAULT ''")

    # 诚实化：无 ua 的历史行 → historical
    n_hist = conn.execute(
        "UPDATE request_log SET agent='historical' "
        "WHERE agent='claude-code' AND ua=''").rowcount
    # v009 误归的有 ua 行（非 claude-cli）→ unknown（待 reclassify）
    n_unk = conn.execute(
        "UPDATE request_log SET agent='unknown' "
        "WHERE agent='claude-code' AND ua!='' AND ua NOT LIKE 'claude-cli%'").rowcount
    conn.commit()
    if n_hist or n_unk:
        log.info("v010: honestized %d → historical, %d → unknown (pending reclassify)",
                 n_hist, n_unk)
