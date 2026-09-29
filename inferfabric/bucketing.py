"""inferfabric/bucketing.py — 墙钟对齐固定窗分桶（dashboard 四卡单一事实源）。

桶边界钉死在墙钟自然刻度上（整 5 分钟 / 整点 / 本地零点 / 本地周一），
不随 now 滚动：历史桶闭合后值固定，仅最新桶（含 now 的未闭合桶）随新
请求增长。左闭右开 `start <= ts < end`。

hour/day/week 与 power_stats 原实现逐字一致（test_power_series 锁定）；
minute 为本模块新增（功耗卡 UI 无 minute 档，仅 Token/延迟/Agent 消费）。
power_stats re-export hour/day/week/month_slots 以保向后兼容
（test_power_series 仍从 power_stats 导入）。
"""
from __future__ import annotations

import time

# 档位 → (桶数 n, 桶宽秒 w)。n×w = 窗口覆盖。
WALL = {
    "minute": {"n": 12, "w": 300},        # 近 60min · 12×5min
    "hour":   {"n": 24, "w": 3600},       # 近 24h  · 24×1h
    "day":    {"n": 30, "w": 86400},      # 近 30 天 · 30×1d
    "week":   {"n": 13, "w": 7 * 86400},  # 近 91 天 · 13×7d
}

# 桶数 / 桶宽常量（与 power_stats 历史命名一致，逐档对齐）
MINUTE_WIN_BUCKETS = 12
MINUTE_BUCKET_SEC = 300
HOUR_WIN_BUCKETS = 24
DAY_WIN_BUCKETS = 30
WEEK_WIN_BUCKETS = 13
WEEK_BUCKET_SEC = 7 * 86400


def minute_slots(now_ts: int) -> list[tuple[int, int]]:
    """近 60min：整 5 分钟对齐的 12 个 (start, end) 桶，末桶 = 当前 5 分钟（部分）。

    对齐 `now_ts - (now_ts % 300)`——整 5 分钟刻度（XX:00、XX:05、XX:10…），
    与 hour 同为 epoch-modulo（整时区偏移下 UTC/本地刻度重合）。
    """
    minute = now_ts - (now_ts % MINUTE_BUCKET_SEC)
    return [(minute - (MINUTE_WIN_BUCKETS - 1 - i) * MINUTE_BUCKET_SEC,
             minute - (MINUTE_WIN_BUCKETS - 2 - i) * MINUTE_BUCKET_SEC)
            for i in range(MINUTE_WIN_BUCKETS)]


def hour_slots(now_ts: int) -> list[tuple[int, int]]:
    """近 24h：整点对齐的 24 个 (start, end) 桶，末桶 = 当前小时；跨整点不偏移。"""
    hour = now_ts - (now_ts % 3600)
    return [(hour - (HOUR_WIN_BUCKETS - 1 - i) * 3600, hour - (HOUR_WIN_BUCKETS - 2 - i) * 3600)
            for i in range(HOUR_WIN_BUCKETS)]


def day_slots(now_ts: int) -> list[tuple[int, int]]:
    """近 30 天：本地零点对齐的 30 个日桶，末桶 = 今天（部分）。"""
    lt = time.localtime(now_ts)
    midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    return [(midnight - (DAY_WIN_BUCKETS - 1 - i) * 86400,
             midnight - (DAY_WIN_BUCKETS - 2 - i) * 86400)
            for i in range(DAY_WIN_BUCKETS)]


def week_slots(now_ts: int) -> list[tuple[int, int]]:
    """近 ~90 天：本地周一对齐的 13 个周桶（7 天），末桶 = 本周（部分桶）。

    对齐自然周而非滑动窗口——「周」语义 = 星期；末桶从本周周一起算到 now。
    """
    lt = time.localtime(now_ts)
    midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    week_start = midnight - lt.tm_wday * 86400      # 本地周一 00:00
    return [(week_start - (WEEK_WIN_BUCKETS - 1 - i) * WEEK_BUCKET_SEC,
             week_start - (WEEK_WIN_BUCKETS - 2 - i) * WEEK_BUCKET_SEC)
            for i in range(WEEK_WIN_BUCKETS)]


def wallclock_slots(gran: str, now_ts: float | int) -> list[tuple[int, int]]:
    """档位 → 墙钟对齐 slot 列表（升序，末桶含 now 未闭合）。

    gran ∈ minute/hour/day/week；其余 → ValueError。
    """
    now_ts = int(now_ts)
    if gran == "minute":
        return minute_slots(now_ts)
    if gran == "hour":
        return hour_slots(now_ts)
    if gran == "day":
        return day_slots(now_ts)
    if gran == "week":
        return week_slots(now_ts)
    raise ValueError(f"invalid granularity: {gran}")


def bucket_index(slots: list[tuple[int, int]], ts: float) -> int:
    """ts → 所在桶下标（左闭右开 `start <= ts < end`）；窗外 → -1。

    slots 升序、连续（前桶 end == 后桶 start）；ts==某桶 end 归入后桶。
    线性扫描（n ≤ 30），无需二分。
    """
    t = float(ts)
    for i, (start, end) in enumerate(slots):
        if start <= t < end:
            return i
    return -1
