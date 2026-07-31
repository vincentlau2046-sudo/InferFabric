"""内存滑动窗口聚合器 — 从 RequestLog 实时消费并计算指标

G-2: MetricsAggregator
- MetricsAggregator: 基于全量样本的滑动窗口聚合
- AggregatorThread: 后台线程从 queue 消费 RequestLog
- 通过 queue 与 RequestLogger 解耦，主路径零额外锁等待
"""

import logging
import queue as _queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from statistics import median

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
    """内存滑动窗口聚合器 — queue 解耦，主路径零锁等待"""

    def __init__(self, price_config: dict[str, CloudModelPrice] | None = None):
        self._lock = threading.Lock()
        self._samples: list[dict] = []  # asdict(RequestLog) — 轻量存储
        self._price_config = price_config or {}
        self._total_requests = 0
        self._total_success = 0
        self._total_fail = 0

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
            self._total_requests += 1
            if entry.status >= 400 or entry.error:
                self._total_fail += 1
            else:
                self._total_success += 1
            # 7d 样本上限：7d * 10req/min * 60 * 24 ≈ 1M → 截断到 100K
            if len(self._samples) > 100000:
                self._samples = self._samples[-50000:]

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
            return {"window": window, "total": 0}

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
            ttfts = [s["ttft_ms"] for s in msamples if s.get("ttft_ms") and s["ttft_ms"] > 0]
            durations = [s["duration_ms"] for s in msamples if s.get("duration_ms") and s["duration_ms"] > 0]
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

            result["models"][model] = m
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
