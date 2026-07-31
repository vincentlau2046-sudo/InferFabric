"""内存滑动窗口聚合器 — 从 RequestLog dict 实时消费并计算指标

G-2: MetricsAggregator
- MetricsAggregator: 基于全量样本的滑动窗口聚合
- AggregatorThread: 后台线程从 queue 消费 RequestLog dict
- 通过 queue 与 RequestLogger 解耦，主路径零额外锁等待
"""

import logging
import queue as _queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass

log = logging.getLogger("inferfabric.metrics_aggregator")


@dataclass
class CloudModelPrice:
    """模型计价配置"""
    price_input: float = 0.0   # ¥/1M tokens
    price_output: float = 0.0  # ¥/1M tokens


def quantile(data: list[float], q: float) -> float:
    """线性插值分位计算，无需 numpy"""
    if not data:
        return 0.0
    s = sorted(data)
    idx = q * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] + frac * (s[hi] - s[lo])


class MetricsAggregator:
    """内存滑动窗口聚合器 — queue 解耦，主路径零锁等待"""

    _MAX_SAMPLES = 100_000
    _TRIM_TO = 50_000

    def __init__(self, price_config: dict[str, CloudModelPrice] | None = None):
        self._lock = threading.Lock()
        self._samples: list[dict] = []  # asdict(RequestLog)
        self._price_config: dict[str, CloudModelPrice] = price_config or {}
        self._total_requests = 0
        self._total_success = 0
        self._total_fail = 0

    def update_prices(self, prices: dict[str, CloudModelPrice]):
        """更新计价配置（CloudDiscovery reload 后调用）"""
        with self._lock:
            self._price_config = prices

    def record(self, entry: dict):
        """记录一条请求日志（由 AggregatorThread 调用）

        entry: asdict(RequestLog) 的字典
        """
        now = time.time()
        with self._lock:
            entry["_ts"] = now  # 聚合时间戳，用于窗口过滤
            self._samples.append(entry)
            self._total_requests += 1
            status = entry.get("status", 0)
            error = entry.get("error")
            if status >= 400 or error:
                self._total_fail += 1
            else:
                self._total_success += 1
            # 内存保护
            if len(self._samples) > self._MAX_SAMPLES:
                self._samples = self._samples[-self._TRIM_TO:]

    def get_metrics(self, window: str = "24h") -> dict:
        """返回聚合指标

        window: "1h" | "24h" | "7d" | "all"
        """
        now = time.time()
        window_s = {"1h": 3600, "24h": 86400, "7d": 604800, "all": float("inf")}
        cutoff = now - window_s.get(window, 86400)

        with self._lock:
            samples = [s for s in self._samples if s.get("_ts", 0) >= cutoff]
            price_config = dict(self._price_config)  # snapshot

        if not samples:
            return {"window": window, "total_requests": 0, "success_rate": 0.0}

        by_model: dict[str, list[dict]] = defaultdict(list)
        for s in samples:
            by_model[s.get("model", "unknown")].append(s)

        total = len(samples)
        success = sum(1 for s in samples if s.get("status", 0) < 400 and not s.get("error"))
        fail = total - success
        total_cost = 0.0

        models = {}
        for model, msamples in by_model.items():
            ttfts = [s["ttft_ms"] for s in msamples
                     if s.get("ttft_ms") and s["ttft_ms"] > 0]
            durations = [s["duration_ms"] for s in msamples
                         if s.get("duration_ms") and s["duration_ms"] > 0]
            tokens_in = sum(s.get("tokens_in", 0) for s in msamples)
            tokens_out = sum(s.get("tokens_out", 0) for s in msamples)
            m_success = sum(1 for s in msamples if s.get("status", 0) < 400 and not s.get("error"))
            m_fail = len(msamples) - m_success

            price = price_config.get(model)
            cost = 0.0
            if price:
                cost = (tokens_in / 1_000_000) * price.price_input + \
                       (tokens_out / 1_000_000) * price.price_output

            m: dict = {
                "requests": len(msamples),
                "success": m_success,
                "fail": m_fail,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_yuan": round(cost, 4),
            }

            if ttfts:
                m["ttft_p50"] = round(quantile(ttfts, 0.50), 1)
                m["ttft_p95"] = round(quantile(ttfts, 0.95), 1)
                m["ttft_p99"] = round(quantile(ttfts, 0.99), 1)
            if durations:
                m["duration_p50"] = round(quantile(durations, 0.50), 1)
                m["duration_p95"] = round(quantile(durations, 0.95), 1)
                m["duration_p99"] = round(quantile(durations, 0.99), 1)

            models[model] = m
            total_cost += cost

        return {
            "window": window,
            "total_requests": total,
            "success": success,
            "fail": fail,
            "success_rate": round(success / max(total, 1) * 100, 1),
            "cost_yuan": round(total_cost, 4),
            "models": models,
        }


class AggregatorThread(threading.Thread):
    """后台线程从 queue 消费 RequestLog dict 推入聚合器"""

    def __init__(self, aggregator: MetricsAggregator, q: _queue.Queue):
        super().__init__(daemon=True, name="iff-aggregator")
        self._agg = aggregator
        self._q = q

    def run(self):
        while True:
            try:
                entry = self._q.get()
                if entry is None:
                    break  # shutdown signal
                self._agg.record(entry)
            except Exception:
                log.warning("aggregator record failed", exc_info=True)
