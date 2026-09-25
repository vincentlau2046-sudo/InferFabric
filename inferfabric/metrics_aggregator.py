"""内存滑动窗口聚合器 — 从 RequestLog 实时消费并计算指标

G-2: MetricsAggregator
- MetricsAggregator: 基于全量样本的滑动窗口聚合
- AggregatorThread: 后台线程从 queue 消费 RequestLog
- 通过 queue 与 RequestLogger 解耦，主路径零额外锁等待

v4.6.2: 启动时从 SQLite request_log.db 回填最近 N 小时数据。
"""

import collections
import datetime
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


# 物理下限护栏（ms/token）：低于此值的 TPOT 样本物理上不可能（RTX 5090D 真实
# decode ~8–18 ms/token），一律当测量伪值排除：不统计、当空值处理（桶 → None，
# 前端 connectNulls 亮点直连）。典型来源：非流式 ttft≈duration（header 到达 ≈
# 总耗时）、引擎缓冲后冲刷时 ttft 贴近 duration → (duration-ttft) 只剩亚 ms 级
# 传输开销（历史伪值 0.2ms/token ≈ 8k–50M t/s 即此类）。
# v6.1: 0.005 → 1.0（相对真实 decode 留 ~8× 安全裕度，伪值 0.2 < 1.0 被排除）。
TPOT_FLOOR_MS = 1.0


def _tpot_of(sample: dict) -> float | None:
    """逐请求派生 TPOT（ms/token）；无意义样本返回 None（零 schema 变更，不落库）。

    合格：status<400、ttft_ms>0、duration_ms>ttft_ms、tokens_out>=2；
    且派生值 >= TPOT_FLOOR_MS（<0.005 舍入为 0.00，排除）。
    """
    if (sample["status"] < 400
            and sample.get("ttft_ms") and sample["ttft_ms"] > 0
            and sample.get("duration_ms") and sample["duration_ms"] > sample["ttft_ms"]
            and (sample.get("tokens_out") or 0) >= 2):
        tp = ((sample["duration_ms"] - sample["ttft_ms"])
              / ((sample.get("tokens_out") or 0) - 1))
        if tp >= TPOT_FLOOR_MS:
            return tp
    return None


def _e2e_tps_of(sample: dict) -> float | None:
    """v6.1: 逐请求 E2E 端到端速率（tokens/s）= tokens_out / (duration_ms/1000)。

    不依赖 ttft_ms — 流式/非流式通用，不受 ttft 语义伪值影响；
    排除失败（status>=400）/ 零时长 / 零输出样本。
    """
    if (sample["status"] < 400
            and sample.get("duration_ms") and sample["duration_ms"] > 0
            and (sample.get("tokens_out") or 0) >= 1):
        return (sample["tokens_out"] or 0) / (sample["duration_ms"] / 1000.0)
    return None


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

    @property
    def price_config(self) -> dict[str, CloudModelPrice]:
        """云端模型价格表（model_id → CloudModelPrice，¥/1M tokens）。

        启动时由 ProxyManager._load_price_config 经 update_prices 注入
        （cloud_provider.yaml 的 cloud_models + provider model_specs）。
        只读视图：/api/token-curve 云端桶 cost 字段以此计费。"""
        return self._price_config

    def update_prices(self, price_config: dict[str, CloudModelPrice]):
        """Update price configuration (called after cloud discovery completes)."""
        with self._lock:
            self._price_config = price_config

    def get_metrics(self, window: str = "24h",
                    axis_models: list[tuple[str, str]] | None = None) -> dict:
        """返回聚合指标

        window: "1h" | "24h" | "7d" | "all"
        axis_models: [(model_name, source)] 配置驱动 x 轴 — 无请求模型补零占位
        """
        now = time.time()
        window_s = {"1h": 3600, "24h": 86400, "7d": 604800, "all": float("inf")}
        win = window_s.get(window, 86400)
        cutoff = now - win

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

        # v6.0: 配置驱动 x 轴 — axis_models 补零占位 + source 标记 + 稳定排序
        friendly_of = (lambda raw: self._name_map.get(raw, raw))
        by_friendly = defaultdict(list)
        for raw, ss in by_model.items():
            by_friendly[friendly_of(raw)].extend(ss)
        source_of = {name: src for name, src in (axis_models or [])}
        for name in source_of:
            by_friendly.setdefault(name, [])
        all_names = sorted(by_friendly.keys(),
                           key=lambda n: (-len(by_friendly[n]), n))

        for model in all_names:
            msamples = by_friendly[model]
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
                "source": source_of.get(model, "observed"),
                "ttft_samples": len(ttfts),
            }

            if ttfts:
                m["ttft_p50"] = round(median(ttfts), 1)
                m["ttft_p95"] = round(quantile(ttfts, 0.95), 1)
                m["ttft_p99"] = round(quantile(ttfts, 0.99), 1)
            if durations:
                m["duration_p50"] = round(median(durations), 1)
                m["duration_p95"] = round(quantile(durations, 0.95), 1)
                m["duration_p99"] = round(quantile(durations, 0.99), 1)

            # v6.0: TPOT 逐请求推导 — 仅成功流式样本（tokens_out>=2 且 duration>ttft），
            # 零 schema 变更；非流式/短响应/失败样本不进分位；
            # 近零值（<TPOT_FLOOR_MS，如非流式 ttft≈duration）视为无意义，排除
            tpots = [tp for s in msamples if (tp := _tpot_of(s)) is not None]
            m["tpot_samples"] = len(tpots)
            if tpots:
                m["tpot_p50"] = round(median(tpots), 2)
                m["tpot_p95"] = round(quantile(tpots, 0.95), 2)
                m["tpot_p99"] = round(quantile(tpots, 0.99), 2)

            # v6.1: E2E 端到端速率（tok/s，逐请求派生后取分位；ttft 无关，
            # 流式/非流式通用 — 监控 KPI 第 7 卡）
            e2es = [e for s in msamples if (e := _e2e_tps_of(s)) is not None]
            m["e2e_tps_samples"] = len(e2es)
            if e2es:
                m["e2e_tps_p50"] = round(median(e2es), 1)
                m["e2e_tps_p95"] = round(quantile(e2es, 0.95), 1)

            # v6.1: 请求速率 + 平均输出长度（监控 KPI 第 8/9 卡；window="all"
            # 时 win=inf → rps=0.0 占位）。rps 留 6 位小数保精度（24h 个位数
            # 请求 → ~3e-05，4 位会全 0）。
            m["rps"] = round(len(msamples) / win, 6)
            m["avg_out_len"] = round(tokens_out / max(len(msamples), 1), 1)

            result["models"][model] = m
            result["cost_yuan"] += cost

        result["cost_yuan"] = round(result["cost_yuan"], 4)
        result["success_rate"] = round(result["success"] / max(result["total_requests"], 1) * 100, 1)
        return result

    def get_latency_series(self, window: str = "hour", bucket_ms: int = 3600000,
                          top_n: int = 5,
                          percentiles: tuple[float, ...] = (0.50, 0.95),
                          source_of: dict | None = None) -> dict:
        """时间分桶 × 逐模型 TTFT/TPOT 分位序列（模型延迟趋势图数据源）。

        - 严格 n 桶覆盖窗口（n = 窗口/桶宽精确整除）：minute=12×5min、
          hour=24×1h、day=30×1d——桶数 = 档位定值，不随墙钟多点/少点。
        - 桶按「相对年龄」锚定 now（同 token-curve）：idx 0 = 最旧（窗口左缘）、
          idx n-1 = 最新（含 now）；桶内无样本 → 该位置 None（前端只桥接中间
          空桶，首尾空不延伸）。
        - 仅保留窗口内**有 TTFT 样本的模型**中请求数前 top_n 个（404 unknown_model
          等零数据模型不占位；键序 = 请求数降序 → 名称升序）。
        - 零 schema 变更：TPOT 逐请求推导（同 get_metrics 过滤条件）。
        - source_of: {模型友好名: "local"/"cloud"}，缺省/未命中 → "observed"。
        """
        now = time.time()
        window_s = {"minute": 3600, "hour": 86400, "day": 2592000, "all": 86400}
        ws = window_s.get(window, 86400)
        cutoff = now - ws
        bucket_s = max(1.0, bucket_ms / 1000.0)
        n_buckets = int(ws / bucket_s)          # 定值：12 / 24 / 30
        start_bucket = now - n_buckets * bucket_s

        with self._lock:
            samples = [s for s in self._samples if s.get("timestamp", 0) >= cutoff]

        friendly_of = (lambda raw: self._name_map.get(raw, raw))
        req_count: "defaultdict" = defaultdict(int)
        ttft_count: "defaultdict" = defaultdict(int)
        for s in samples:
            m = friendly_of(s["model"])
            req_count[m] += 1
            if s["status"] < 400 and s.get("ttft_ms") and s["ttft_ms"] > 0:
                ttft_count[m] += 1
        # v6.0: 排名只统计有 TTFT 样本的模型（404/unknown_model 的请求量不占位）；
        # top_n 截断，第 N+1 名起不统计
        eligible = [m for m in req_count if ttft_count[m] > 0]
        top_models = sorted(eligible, key=lambda m: (-req_count[m], m))[:top_n]
        top_set = set(top_models)

        cells: "defaultdict" = defaultdict(lambda: {"ttft": [], "tpot": []})

        for s in samples:
            m = friendly_of(s["model"])
            if m not in top_set:
                continue
            ts = s.get("timestamp", 0)
            age_s = now - ts
            bi = n_buckets - 1 - int(age_s // bucket_s)   # 相对年龄：idx 0 最旧 → n-1 最新
            if bi < 0 or bi >= n_buckets:
                continue
            cell = cells[(m, bi)]
            if s["status"] < 400 and s.get("ttft_ms") and s["ttft_ms"] > 0:
                cell["ttft"].append(s["ttft_ms"])
            tp = _tpot_of(s)
            if tp is not None:
                cell["tpot"].append(tp)

        q_labels = ["p%.0f" % int(q * 100) for q in percentiles]
        series = {}
        for m in top_models:
            entry: dict = {"source": (source_of or {}).get(m, "observed"),
                           "requests": req_count[m]}
            for kind in ("ttft", "tpot"):
                for q, ql in zip(percentiles, q_labels):
                    rnd = 2 if kind == "tpot" else 1
                    arr = []
                    for bi in range(n_buckets):
                        vals = cells.get((m, bi), {}).get(kind, [])
                        arr.append(round(quantile(vals, q), rnd) if vals else None)
                    entry["%s_%s" % (kind, ql)] = arr
                entry["%s_n" % kind] = [
                    len(cells.get((m, bi), {}).get(kind, [])) for bi in range(n_buckets)]
            series[m] = entry

        labels = []
        for bi in range(n_buckets):
            t = start_bucket + bi * bucket_s
            fmt = "%m-%d %H:%M" if ws >= 86400 else "%H:%M"
            labels.append(datetime.datetime.fromtimestamp(t).strftime(fmt))

        return {
            "window": window,
            "bucket_ms": bucket_ms,
            "buckets": labels,
            "percentiles": q_labels,
            "series": series,
            "total_models": len(top_models),
        }


class AggregatorThread(threading.Thread):
    """后台线程从 queue 消费 RequestLog 推入聚合器"""

    def __init__(self, aggregator: MetricsAggregator, queue: _queue.Queue):
        super().__init__(daemon=True)
        self._agg = aggregator
        self._q = queue
        self._stop = threading.Event()

    def stop(self):
        """Signal the thread to exit gracefully."""
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                entry = self._q.get(timeout=1)
                self._agg.record(entry)
            except _queue.Empty:
                continue
            except Exception:
                log.warning("aggregator record failed", exc_info=True)
