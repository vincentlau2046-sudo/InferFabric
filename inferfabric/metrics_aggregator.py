"""内存滑动窗口聚合器 — 从 RequestLog 实时消费并计算指标

G-2: MetricsAggregator
- MetricsAggregator: 基于全量样本的滑动窗口聚合
- AggregatorThread: 后台线程从 queue 消费 RequestLog
- 通过 queue 与 RequestLogger 解耦，主路径零额外锁等待

v4.6.2: 启动时从 SQLite request_log.db 回填最近 N 小时数据。
"""

import collections
import logging
import queue as _queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inferfabric.request_log_db import RequestLogDB

log = logging.getLogger("inferfabric.metrics_aggregator")


def quantile(data: list[float], q: float) -> float:
    """简单分位计算（不引入 numpy）"""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = q * (len(sorted_data) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_data) - 1)
    frac = idx - lo
    return sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo])


@dataclass
class CloudModelPrice:
    """云端模型价格配置 — ¥/1M tokens"""
    price_input: float = 0.0
    price_output: float = 0.0


class MetricsAggregator:
    """内存滑动窗口聚合器 — queue 解耦，主路径零锁等待

    v4.6.2: 支持启动时从 SQLite 回填历史数据。
    """

    def __init__(self, price_config: dict[str, CloudModelPrice] | None = None,
                 db: "RequestLogDB | None" = None,
                 replay_hours: float = 24.0,
                 model_name_map: dict[str, str] | None = None):
        self._lock = threading.Lock()
        self._samples: collections.deque = collections.deque(maxlen=100000)
        self._price_config = price_config or {}
        self._db = db
        self._replay_hours = replay_hours
        self._name_map = model_name_map or {}

        if self._db and self._replay_hours > 0:
            self._replay_from_db()

    def _replay_from_db(self):
        """从 SQLite request_log.db 回填最近 replay_hours 小时的数据到内存。"""
        since = time.time() - self._replay_hours * 3600
        try:
            rows = self._db.query_request_log(since=since)
            with self._lock:
                for row in rows:
                    self._samples.append({
                        "model": row["model"],
                        "status": row["status"],
                        "error": row["error"],
                        "ttft_ms": row["ttft_ms"],
                        "duration_ms": row["duration_ms"],
                        "tokens_in": row["tokens_in"],
                        "tokens_out": row["tokens_out"],
                        "route": row["route"],
                        "cloud_provider": row["cloud_provider"],
                        "timestamp": row["timestamp"],
                    })
            log.info("MetricsAggregator replayed %d rows from SQLite (last %.0fh)",
                     len(rows), self._replay_hours)
        except Exception as e:
            log.warning("MetricsAggregator replay failed: %s", e)

    def refresh_from_db(self, hours: float = 720.0):
        """v5.2: Refresh memory deque from request_log.db.
        
        Solves data loss on restart: can replay up to 30 days.
        """
        if not self._db:
            return
        since = time.time() - hours * 3600
        try:
            rows = self._db.query_request_log(since=since, limit=500000)
            if rows:
                with self._lock:
                    self._samples.clear()
                    for r in rows:
                        self._samples.append({
                            "model": r.get("model", "unknown"),
                            "status": r.get("status", 200),
                            "ttft_ms": r.get("ttft_ms"),
                            "tokens_in": r.get("tokens_in", 0),
                            "tokens_out": r.get("tokens_out", 0),
                            "duration_ms": r.get("duration_ms", 0),
                            "error": r.get("error"),
                            "timestamp": r.get("timestamp", 0),
                            "route": r.get("route", "local"),
                        })
                log.info("MetricsAggregator refreshed %d rows from DB (%dh window)",
                         len(rows), hours)
        except Exception as e:
            log.warning("MetricsAggregator refresh_from_db failed: %s", e)

    def record(self, entry):
        """记录一条请求日志（由 AggregatorThread 调用）

        entry: RequestLog dataclass instance (duck-typed to avoid circular import)
        """
        with self._lock:
            # 转换为 dict 存储，节省内存
            d = {
                "model": entry.model,
                "status": entry.status,
                "error": entry.error,
                "ttft_ms": entry.ttft_ms,
                "duration_ms": entry.duration_ms,
                "tokens_in": entry.tokens_in,
                "tokens_out": entry.tokens_out,
                "route": entry.route,
                "cloud_provider": entry.cloud_provider,
                "timestamp": entry.timestamp or time.time(),
            }
            self._samples.append(d)

    def update_prices(self, price_config: dict[str, CloudModelPrice]):
        """Update price configuration (called after cloud discovery completes)."""
        with self._lock:
            self._price_config = price_config

    def get_metrics(self, window: str = "24h") -> dict:
        """返回聚合指标

        window: "1h" | "24h" | "7d" | "all"
        """
        now = time.time()
        window_s = {"1h": 3600, "24h": 86400, "7d": 604800, "all": float("inf")}
        cutoff = now - window_s.get(window, 86400)

        with self._lock:
            samples = [s for s in self._samples if s.get("timestamp", 0) >= cutoff]

        if not samples:
            return {
                "window": window, "total_requests": 0, "success": 0,
                "fail": 0, "models": {}, "cost_yuan": 0.0, "success_rate": 0.0,
            }

        # 按 model 分组
        by_model = defaultdict(list)
        for s in samples:
            by_model[s["model"]].append(s)

        result = {
            "window": window,
            "total_requests": len(samples),
            "success": sum(1 for s in samples if s["status"] < 400 and not s["error"]),
            "fail": sum(1 for s in samples if s["status"] >= 400 or s["error"]),
            "models": {},
            "cost_yuan": 0.0,
        }

        for model, msamples in by_model.items():
            ttfts = [s["ttft_ms"] for s in msamples if s["status"] < 400 and s.get("ttft_ms") and s["ttft_ms"] > 0]
            durations = [s["duration_ms"] for s in msamples if s["status"] < 400 and s.get("duration_ms") and s["duration_ms"] > 0]
            tokens_in = sum(s.get("tokens_in", 0) for s in msamples)
            tokens_out = sum(s.get("tokens_out", 0) for s in msamples)

            # 费用估算
            price = self._price_config.get(model)
            cost = 0.0
            if price:
                cost = (tokens_in / 1_000_000) * price.price_input + \
                       (tokens_out / 1_000_000) * price.price_output

            m = {
                "requests": len(msamples),
                "success": sum(1 for s in msamples if s["status"] < 400 and not s["error"]),
                "fail": sum(1 for s in msamples if s["status"] >= 400 or s["error"]),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_yuan": round(cost, 4),
            }

            if ttfts:
                m["ttft_p50"] = round(median(ttfts), 1)
                m["ttft_p95"] = round(quantile(ttfts, 0.95), 1)
                m["ttft_p99"] = round(quantile(ttfts, 0.99), 1)
            if durations:
                m["duration_p50"] = round(median(durations), 1)
                m["duration_p95"] = round(quantile(durations, 0.95), 1)
                m["duration_p99"] = round(quantile(durations, 0.99), 1)

            friendly = self._name_map.get(model, model)
            result["models"][friendly] = m
            result["cost_yuan"] += cost

        result["cost_yuan"] = round(result["cost_yuan"], 4)
        result["success_rate"] = round(result["success"] / max(result["total_requests"], 1) * 100, 1)
        return result


class AggregatorThread(threading.Thread):
    """后台线程从 queue 消费 RequestLog 推入聚合器"""

    def __init__(self, aggregator: MetricsAggregator, queue: _queue.Queue):
        super().__init__(daemon=True)
        self._agg = aggregator
        self._q = queue

    def run(self):
        while True:
            try:
                entry = self._q.get()
                self._agg.record(entry)
            except Exception:
                log.warning("aggregator record failed", exc_info=True)
