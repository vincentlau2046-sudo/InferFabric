"""
inferfabric/power_stats.py — GPU 板卡功耗时间序列 (v6.2 监控 TAB「功耗 / 电费」卡).

口径：功耗 = GPU 板卡实时功耗（nvidia-smi power.draw，含 idle 底耗）；
每 60s 后台采样一次落 SQLite（gpu_power_samples，v007 迁移），90 天留存。
电费 = 累计耗电度数 × ¥1/度（PRICE_YUAN_PER_KWH，常量，后续可配置化）。

视图语义（已与用户定稿）：
  小时 = 近 24h，每小时一桶（对齐整点，末桶为当前小时的部分桶）
  天   = 近 30 天，每天一桶（对齐本地零点）
  周   = 近 ~90 天，每周一桶（对齐本地周一，末桶为本周的部分桶；月整体弃用）
  月   = 按自然月（YYYY-MM），从最早有数据的月份到当前月（只读端点保留，UI 不再提供）

每桶输出：
  avg_w     桶内样本均值（无样本 → None，前端断开）
  kwh       桶内耗电（梯形积分；单样本按采样间隔 60s 折算）
  cum_kwh   窗口起点累计到该桶末的总耗电（只增不减）
  cum_yuan  累计电费 = cum_kwh × price（¥1/度 下数值相等）

边界说明：梯形积分只取桶内相邻样本对，跨桶边界的一对样本断桥
（一小时 ~60 个样本，桥接误差 < 2%，可接受）。
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time

log = logging.getLogger("inferfabric.power_stats")

PRICE_YUAN_PER_KWH = 1.0          # 电价常量（¥/度；1 度 = 1 kWh）
SAMPLE_INTERVAL_SEC = 60.0        # PowerSampler 采样间隔（单样本能量折算用）
HOUR_WIN_BUCKETS = 24             # 小时视图桶数
DAY_WIN_BUCKETS = 30              # 天视图桶数
WEEK_WIN_BUCKETS = 13             # 周视图桶数（13×7d ≈ 91 天，覆盖「近 90 天」）
WEEK_BUCKET_SEC = 7 * 86400       # 一周边界秒数


# ── 探针 ───────────────────────────────────────────────────────

def probe_gpu_power_w() -> float | None:
    """当前 GPU 板卡功耗（W）。nvidia-smi 不可用/无 GPU → None（本拍跳过）。"""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return round(float(r.stdout.strip().splitlines()[0].strip().split()[0]), 1)
    except Exception:
        pass
    return None


# ── 采样器 ─────────────────────────────────────────────────────

class PowerSampler:
    """60s 后台线程采样 GPU 板卡功耗并落 SQLite。

    独立于前端轮询（页面关着也记账，历史才连续）。probe 失败（无 GPU）
    跳过本拍、下一拍重试（nvidia-smi 单次 <20ms，代价可忽略）。
    每天清理一次超期样本（RETENTION_DAYS，幂等）。
    """

    RETENTION_DAYS = 90

    def __init__(self, db, interval: float = 60.0, probe=None):
        self._db = db
        self._interval = interval
        self._probe = probe if probe is not None else probe_gpu_power_w
        self._stop = threading.Event()
        self._thread = None
        self._last_prune = 0.0
        self.sample_count = 0

    def _now(self):
        return time.time()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="gpu-power")
        self._thread.start()

    def stop(self):
        self._stop.set()
        th = self._thread
        if th:
            th.join(timeout=2.0)

    def sample_once(self):
        """拍一拍：probe 成功落一行 (ts, watts)，失败返回 None 不落盘。"""
        w = self._probe()
        if w is None:
            return None
        ts = int(self._now())
        self._db.insert_gpu_power_samples([{"ts": ts, "watts": round(float(w), 1)}])
        self.sample_count += 1
        return w

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception as e:
                log.debug("power sample failed: %s", e)
            try:
                now = self._now()
                if now - self._last_prune >= 86400:
                    self._last_prune = now
                    self._db.prune_gpu_power_samples(now - self.RETENTION_DAYS * 86400)
            except Exception as e:
                log.debug("power prune failed: %s", e)
            self._stop.wait(self._interval)


# ── 分桶 ───────────────────────────────────────────────────────

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


def _next_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _month_start(y: int, m: int) -> int:
    return int(time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1)))


def month_slots(now_ts: int, first_ts: int) -> list[tuple[int, int]]:
    """自然月桶：从 first_ts 所在月到当前月（首个无数据月给空桶，与 hour/day 同构）。"""
    lt0 = time.localtime(first_ts)
    lt1 = time.localtime(now_ts)
    y, m = lt0.tm_year, lt0.tm_mon
    y1, m1 = lt1.tm_year, lt1.tm_mon
    slots: list[tuple[int, int]] = []
    while (y, m) <= (y1, m1):
        start = _month_start(y, m)
        ny, nm = _next_month(y, m)
        end = _month_start(ny, nm)
        slots.append((start, end))
        y, m = ny, nm
    return slots


def bucket_stats(samples: list[tuple[int, float]],
                 slots: list[tuple[int, int]]) -> list[dict]:
    """samples[(ts,watts)] 升序 × 升序桶 → 每桶 {t, avg_w, kwh, cum_kwh, cum_yuan}。

    能量 = 桶内相邻样本对梯形积分；单样本按 SAMPLE_INTERVAL_SEC 折算。
    cum 从窗口起点起算，跨桶累计（只增不减）。
    """
    buckets: list[dict] = []
    cum = 0.0
    si = 0
    n = len(samples)
    for start, end in slots:
        pts: list[tuple[int, float]] = []
        while si < n and samples[si][0] < start:
            si += 1
        j = si
        while j < n and samples[j][0] < end:
            pts.append(samples[j])
            j += 1
        if pts:
            avg_w = round(sum(w for _, w in pts) / len(pts), 1)
            if len(pts) == 1:
                en = pts[0][1] * SAMPLE_INTERVAL_SEC / 3.6e6
            else:
                en = sum((pts[i][1] + pts[i + 1][1]) / 2.0 * (pts[i + 1][0] - pts[i][0])
                         for i in range(len(pts) - 1)) / 3.6e6
        else:
            avg_w = None
            en = 0.0
        cum += en
        buckets.append({
            "t": int(start),
            "avg_w": avg_w,
            "kwh": en,
            "cum_kwh": cum,
            "cum_yuan": cum * PRICE_YUAN_PER_KWH,
        })
    return buckets


# ── 查询入口（handler /api/power 调用）────────────────────────

def query_power_series(db, gran: str = "hour", price: float = PRICE_YUAN_PER_KWH) -> dict:
    """按档位返回分桶功耗/电费序列 + 窗口汇总。gran ∈ {hour, day, week, month}。"""
    now = int(time.time())
    if gran == "day":
        slots = day_slots(now)
    elif gran == "week":
        slots = week_slots(now)
    elif gran == "month":
        oldest = db.query_gpu_power_samples(since=0, limit=1)
        slots = month_slots(now, oldest[0]["ts"] if oldest else now)
    else:
        slots = hour_slots(now)
    since = slots[0][0]
    rows = db.query_gpu_power_samples(since, until=now + 60)
    samples = [(int(r["ts"]), float(r["watts"])) for r in rows]
    buckets = bucket_stats(samples, slots)
    avg_ws = [b["avg_w"] for b in buckets if b["avg_w"] is not None]
    kwh_total = sum(b["kwh"] for b in buckets)
    return {
        "gran": gran,
        "price_yuan_per_kwh": price,
        "buckets": buckets,
        "totals": {
            "kwh": round(kwh_total, 4),
            "yuan": round(kwh_total * price, 4),
            "avg_w": round(sum(avg_ws) / len(avg_ws), 1) if avg_ws else None,
            "count": len(rows),
        },
        "available": len(rows) > 0,
    }