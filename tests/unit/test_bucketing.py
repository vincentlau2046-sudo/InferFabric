"""unit/test_bucketing.py — 墙钟对齐固定窗分桶单一事实源测试。

四档锚点（整 5 分钟 / 整点 / 本地零点 / 本地周一）+ 左闭右开边界归属。
被 dashboard Token/延迟/Agent 三卡 + 功耗卡（re-export）共同消费。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from inferfabric.bucketing import (  # noqa: E402
    WALL, wallclock_slots, bucket_index,
    minute_slots, hour_slots, day_slots, week_slots,
)


def _mktime(y, mo, d, h=0, mi=0, s=0):
    return int(time.mktime((y, mo, d, h, mi, s, 0, 0, -1)))


# ── 1. 四档锚点对齐 ──────────────────────────────────────────────

def test_minute_slots_aligned_5min():
    now = _mktime(2026, 9, 29, 21, 10, 40)        # 21:10:40
    slots = minute_slots(now)
    assert len(slots) == WALL["minute"]["n"] == 12
    assert all(end - start == 300 for start, end in slots)
    # 末桶起点 = 整 5 分钟刻度（21:10:00），含 now
    assert slots[-1][0] == _mktime(2026, 9, 29, 21, 10, 0)
    assert slots[-1][0] <= now < slots[-1][1]
    # 每桶起点 % 300 == 0（墙钟 5 分钟刻度）
    for start, _ in slots:
        assert start % 300 == 0


def test_hour_slots_aligned_hour():
    now = _mktime(2026, 9, 29, 21, 10, 40)
    slots = hour_slots(now)
    assert len(slots) == 24
    assert all(end - start == 3600 for start, end in slots)
    assert slots[-1][0] == _mktime(2026, 9, 29, 21, 0, 0)   # 整点
    assert slots[-1][0] <= now < slots[-1][1]


def test_day_slots_aligned_local_midnight():
    now = _mktime(2026, 9, 29, 21, 10, 40)
    slots = day_slots(now)
    assert len(slots) == 30
    for start, _ in slots:
        lt = time.localtime(start)
        assert (lt.tm_hour, lt.tm_min, lt.tm_sec) == (0, 0, 0)   # 本地零点
    assert slots[-1][0] == _mktime(2026, 9, 29)                  # 今天零点


def test_week_slots_aligned_local_monday():
    # 2026-09-29 是周二 → 本周一 = 2026-09-28
    now = _mktime(2026, 9, 29, 21, 10, 40)
    slots = week_slots(now)
    assert len(slots) == 13
    for start, _ in slots:
        lt = time.localtime(start)
        assert (lt.tm_hour, lt.tm_min, lt.tm_sec) == (0, 0, 0)
        assert lt.tm_wday == 0, "周桶必须本地周一 00:00 对齐"
    assert slots[-1][0] == _mktime(2026, 9, 28)                  # 本周一


def test_wallclock_slots_dispatcher():
    now = _mktime(2026, 9, 29, 21, 10, 40)
    for gran in ("minute", "hour", "day", "week"):
        assert wallclock_slots(gran, now) == {
            "minute": minute_slots, "hour": hour_slots,
            "day": day_slots, "week": week_slots,
        }[gran](now)
    try:
        wallclock_slots("month", now)
        assert False, "month 应 ValueError"
    except ValueError:
        pass


# ── 2. 左闭右开边界归属 ─────────────────────────────────────────

def test_bucket_index_left_closed_right_open():
    """ts == 桶起点 → 入本桶；ts == 桶终点 → 入下桶；窗外 → -1。"""
    now = _mktime(2026, 9, 29, 21, 10, 0)          # 整 5 分钟刻度
    slots = minute_slots(now)
    # ts == 末桶起点 → idx 11
    assert bucket_index(slots, slots[11][0]) == 11
    # ts == 末桶终点 == 下桶起点（窗外，无下桶）→ -1
    assert bucket_index(slots, slots[11][1]) == -1
    # ts == idx 5 桶起点 → idx 5（左闭）
    assert bucket_index(slots, slots[5][0]) == 5
    # ts == idx 5 桶终点 → idx 6（右开，归下桶）
    assert bucket_index(slots, slots[5][1]) == 6
    # ts 早于首桶起点 → -1
    assert bucket_index(slots, slots[0][0] - 1) == -1


def test_bucket_index_now_in_last_bucket():
    """now 恒在末桶（含 now 的未闭合桶）——末桶随新请求增长，历史桶固定。"""
    now = time.time()
    for gran in ("minute", "hour", "day", "week"):
        slots = wallclock_slots(gran, now)
        assert bucket_index(slots, now) == len(slots) - 1, f"{gran}: now 不在末桶"


def test_history_bucket_stable_across_now_shift():
    """墙钟对齐：已闭合历史桶的归属不随 now 小幅推移而变（划窗式会变）。

    now=21:10:40 与 now=21:11:00（推 20s）下，同一历史 ts（21:05:00）应归同一桶。
    """
    ts_hist = _mktime(2026, 9, 29, 21, 5, 0)       # 历史请求 21:05:00
    now1 = _mktime(2026, 9, 29, 21, 10, 40)
    now2 = _mktime(2026, 9, 29, 21, 11, 0)         # +20s
    s1 = minute_slots(now1)
    s2 = minute_slots(now2)
    # 两 now 同属一个 5 分钟对齐周期 → 桶轴不变 → 历史桶归属不变
    assert bucket_index(s1, ts_hist) == bucket_index(s2, ts_hist)
