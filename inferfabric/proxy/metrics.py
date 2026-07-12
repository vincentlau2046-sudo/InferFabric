"""
inferfabric/proxy/metrics.py — vLLM Prometheus metrics & EMA throughput tracker.

Extracted from proxy.py (v4.1 P3 split).
"""

import math
import urllib.request
from urllib.parse import urlparse, parse_qs


def parse_prometheus_text(text: str):
    """Parse Prometheus /metrics text into gauges, counters, histograms."""
    gauges, counters, histos = {}, {}, {}

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        bracket = line.find("{")
        if bracket >= 0:
            name = line[:bracket]
            close = line.rfind("}")
            val_str = line[close+1:].strip() if close > bracket else ""
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            name, val_str = parts[0], parts[1]

        try:
            val = float(val_str)
        except ValueError:
            continue

        if name.endswith("_bucket"):
            base = name[:-7]
            le_start = line.find('le="', bracket) if bracket >= 0 else -1
            le_end = line.find('"', le_start + 4) if le_start >= 0 else -1
            le_val = float(line[le_start+4:le_end]) if le_start >= 0 else math.inf
            if base not in histos:
                histos[base] = {"buckets": [], "sum": 0.0, "count": 0}
            histos[base]["buckets"].append((le_val, int(val)))
        elif name.endswith("_sum"):
            base = name[:-4]
            if base not in histos:
                histos[base] = {"buckets": [], "sum": 0.0, "count": 0}
            histos[base]["sum"] = val
        elif name.endswith("_count"):
            base = name[:-6]
            if base not in histos:
                histos[base] = {"buckets": [], "sum": 0.0, "count": 0}
            histos[base]["count"] = int(val)
        elif "_total" in name:
            counters[name.rsplit("_total", 1)[0]] = val
        else:
            gauges[name] = val

    return gauges, counters, histos


# ── Quantile ──────────────────────────────────────────────────────

def quantile(buckets, count, q):
    """Compute quantile from histogram buckets."""
    if count == 0 or not buckets:
        return None
    target = count * q
    sorted_bk = sorted(buckets, key=lambda x: x[0])
    cum = 0
    for i, (le, c) in enumerate(sorted_bk):
        cum = c
        if cum >= target:
            if i == 0:
                return le / 2
            prev_le, prev_c = sorted_bk[i - 1]
            if math.isfinite(le) and c > prev_c:
                return prev_le + (le - prev_le) * (target - prev_c) / (c - prev_c)
            return prev_le
    return sorted_bk[-1][0]


# ── EMA Throughput Collector ──────────────────────────────────────

class VllmMetricsCollector:
    """Per-port Prometheus metric collector with EMA throughput tracking.

    Module-level state:
      gen_counters[port] -> (timestamp, generation_tokens_total)
      throughput_ema[port] -> float (EMA smoothed tokens/s)
    """

    EMA_ALPHA = 0.3
    gen_counters: dict = {}
    throughput_ema: dict = {}

    @classmethod
    def compute(cls, port, gauges, counters, histos):
        """Parse gauges/counter/histos into result dict and update EMA state.

        Returns (result_dict, port_is_new: bool).
        """
        import time
        result = {}

        # KV cache
        kv = gauges.get("vllm:kv_cache_usage_perc")
        if kv is not None:
            result["kv_cache_usage_perc"] = round(kv * 100, 1)

        # TTFT
        ttft = histos.get("vllm:time_to_first_token_seconds")
        if ttft and ttft["count"] > 0:
            result["ttft_seconds"] = {
                "p50": round(quantile(ttft["buckets"], ttft["count"], 0.50), 3),
                "p95": round(quantile(ttft["buckets"], ttft["count"], 0.95), 3),
                "mean": round(ttft["sum"] / ttft["count"], 3),
                "count": ttft["count"],
            }
            result["ttft_cum_mean"] = round(ttft["sum"] / ttft["count"], 3)
            result["ttft_cum_n"] = ttft["count"]

        # TPOT
        tpot = histos.get("vllm:request_time_per_output_token_seconds")
        if tpot and tpot["count"] > 0:
            result["tpot_seconds"] = {
                "p50": round(quantile(tpot["buckets"], tpot["count"], 0.50), 3),
                "p95": round(quantile(tpot["buckets"], tpot["count"], 0.95), 3),
                "mean": round(tpot["sum"] / tpot["count"], 4),
                "count": tpot["count"],
            }
            result["tpot_cum_mean"] = round(tpot["sum"] / tpot["count"], 4)
            result["tpot_cum_n"] = tpot["count"]

        # Seq length
        prompt_h = histos.get("vllm:request_prompt_tokens")
        gen_h = histos.get("vllm:request_generation_tokens")
        total_reqs = 0
        if prompt_h:
            total_reqs = prompt_h.get("count", 0)
        if prompt_h and gen_h and total_reqs > 0:
            avg_prompt = round(prompt_h["sum"] / total_reqs)
            avg_gen = round(gen_h["sum"] / total_reqs)
            result["seq_length"] = avg_prompt + avg_gen
            result["seq_prompt"] = avg_prompt
            result["seq_generation"] = avg_gen
            result["seq_count"] = total_reqs

        # Token sums for external stats collector
        if prompt_h and "sum" in prompt_h:
            result["prompt_tokens_sum"] = int(prompt_h["sum"])
        if gen_h and "sum" in gen_h:
            result["generation_tokens_sum"] = int(gen_h["sum"])

        # Throughput (EMA)
        gen_key = "vllm:generation_tokens"
        gen_counter = counters.get(gen_key)
        cur_ts = time.time()
        prev_state = cls.gen_counters.get(port)

        if gen_counter is not None:
            inst_tp = None
            if prev_state is not None:
                prev_ts, prev_val = prev_state
                elapsed = cur_ts - prev_ts
                actual_tokens = int(gen_counter) - int(prev_val)
                if elapsed > 0 and actual_tokens > 0:
                    inst_tp = round(actual_tokens / elapsed, 1)

            prev_ema = cls.throughput_ema.get(port)
            if inst_tp is not None:
                if prev_ema is None:
                    ema_tp = inst_tp
                else:
                    ema_tp = cls.EMA_ALPHA * inst_tp + (1 - cls.EMA_ALPHA) * prev_ema
                cls.throughput_ema[port] = ema_tp
                result["throughput"] = round(ema_tp, 1)
                result["throughput_inst"] = inst_tp
                result["throughput_cum_n"] = int(gen_counter)
            elif prev_ema is not None:
                result["throughput"] = round(prev_ema, 1)
                result["throughput_cum_n"] = int(gen_counter)

        cls.gen_counters[port] = (cur_ts, gen_counter)
        return result


def handle_vllm_metrics(path_query: str):
    """End-to-end handler for /vllm_metrics requests.

    Fetches Prometheus metrics from the given port, parses them,
    and returns the result dict (caller sends JSON response).

    Args:
        path_query: the raw query string from the request path.
    Returns:
        (result_dict, status_code) or (error_dict, 400/502)
    """
    qs = parse_qs(urlparse(f"?{path_query}").query)
    try:
        port = int(qs.get("port", ["8000"])[0])
    except (ValueError, IndexError):
        return {"error": "invalid port"}, 400

    url = f"http://127.0.0.1:{port}/metrics"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            text = resp.read().decode("utf-8")
    except Exception as e:
        return {"error": str(e)}, 502

    gauges, counters, histos = parse_prometheus_text(text)
    result = VllmMetricsCollector.compute(port, gauges, counters, histos)
    return result, 200