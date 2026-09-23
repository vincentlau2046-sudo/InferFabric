"""Migration 007: gpu_power_samples — GPU 板卡功耗时间序列 (v6.2).

每 60s 由 PowerSampler 落一行 (ts, watts)；供监控 TAB「功耗 / 电费」
单图双轴卡按 小时/天/月 分桶聚合。ts 为主键（同一秒幂等去重）。
"""


def upgrade(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gpu_power_samples (
            ts INTEGER PRIMARY KEY,
            watts REAL NOT NULL CHECK (watts >= 0)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gpu_power_ts ON gpu_power_samples(ts)")
    conn.commit()