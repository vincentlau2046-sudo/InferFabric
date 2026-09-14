"""
inferfabric/proxy/metrics_exporter.py — Prometheus 文本格式 /metrics (R6)

零依赖自研输出。从 RequestLogDB + AnomalyCollector 聚合指标。
"""

import time
from collections import defaultdict
from typing import Any


class PrometheusMetricsExporter:
    """从 IFF 的内部分布数据生成 Prometheus 文本格式。

    指标列表：
      iff_requests_total      — counter, labels: model, route, status
      iff_errors_total        — counter, labels: model, error
      iff_tokens_in_total     — counter, labels: model
      iff_tokens_out_total    — counter, labels: model
      iff_anomalies_total     — counter, labels: category, severity
    """

    def __init__(self, telemetry, anomalies):
        self._telemetry = telemetry
        self._anomalies = anomalies
        self._start_time = time.time()

    def generate(self) -> str:
        """生成 Prometheus 文本格式输出。"""
        lines = []
        uptime = time.time() - self._start_time

        # HELP + TYPE 头
        lines.append("# HELP iff_uptime_seconds Process uptime in seconds")
        lines.append("# TYPE iff_uptime_seconds gauge")
        lines.append(f"iff_uptime_seconds {uptime:.0f}")

        lines.append("# HELP iff_requests_total Total requests served")
        lines.append("# TYPE iff_requests_total counter")
        for (model, route, status), count in self._get_request_counts().items():
            labels = f'model="{model}",route="{route}",status="{status}"'
            lines.append(f"iff_requests_total{{{labels}}} {count}")

        lines.append("# HELP iff_errors_total Total erroneous requests")
        lines.append("# TYPE iff_errors_total counter")
        for (model, error), count in self._get_error_counts().items():
            labels = f'model="{model}",error="{error}"'
            lines.append(f"iff_errors_total{{{labels}}} {count}")

        lines.append("# HELP iff_tokens_in_total Input tokens")
        lines.append("# TYPE iff_tokens_in_total counter")
        for model, count in self._get_token_counts("in").items():
            lines.append(f'iff_tokens_in_total{{model="{model}"}} {count}')

        lines.append("# HELP iff_tokens_out_total Output tokens")
        lines.append("# TYPE iff_tokens_out_total counter")
        for model, count in self._get_token_counts("out").items():
            lines.append(f'iff_tokens_out_total{{model="{model}"}} {count}')

        lines.append("# HELP iff_anomalies_total Total anomaly events")
        lines.append("# TYPE iff_anomalies_total counter")
        for (cat, sev), count in self._get_anomaly_counts().items():
            labels = f'category="{cat}",severity="{sev}"'
            lines.append(f"iff_anomalies_total{{{labels}}} {count}")

        lines.append("")
        return "\n".join(lines)

    def _get_request_counts(self) -> dict[tuple[str, str, str], int]:
        """按 (model, route, status) 聚合请求计数。"""
        counts: dict[tuple[str, str, str], int] = defaultdict(int)
        try:
            rows = self._telemetry.query_request_log(since=int(time.time() - 86400), limit=2000)
            for r in rows:
                model = r.get("model", "unknown") or "unknown"
                route = r.get("route", "local") or "local"
                status = str(r.get("status", 0))
                counts[(model, route, status)] += 1
        except Exception:
            pass
        return dict(counts)

    def _get_error_counts(self) -> dict[tuple[str, str], int]:
        """按 (model, error) 聚合错误计数（status != 200）。"""
        counts: dict[tuple[str, str], int] = defaultdict(int)
        try:
            rows = self._telemetry.query_request_log(since=int(time.time() - 86400), limit=2000)
            for r in rows:
                if r.get("status", 200) == 200:
                    continue
                model = r.get("model", "unknown") or "unknown"
                error = r.get("error", "unknown") or "unknown"
                counts[(model, error)] += 1
        except Exception:
            pass
        return dict(counts)

    def _get_token_counts(self, direction: str) -> dict[str, int]:
        """按模型聚合 token 用量。"""
        counts: dict[str, int] = defaultdict(int)
        key = "tokens_in" if direction == "in" else "tokens_out"
        try:
            rows = self._telemetry.query_request_log(since=int(time.time() - 86400), limit=2000)
            for r in rows:
                model = r.get("model", "unknown") or "unknown"
                counts[model] += r.get(key, 0) or 0
        except Exception:
            pass
        return dict(counts)

    def _get_anomaly_counts(self) -> dict[tuple[str, str], int]:
        """按 (category, severity) 聚合异常事件。"""
        counts: dict[tuple[str, str], int] = defaultdict(int)
        try:
            events = self._anomalies.query(since=0, limit=2000)
            for e in events:
                counts[(e.category or "unknown", e.severity or "unknown")] += 1
        except Exception:
            pass
        return dict(counts)


def generate_metrics(telemetry, anomalies) -> str:
    """便捷函数：生成 /metrics 文本。"""
    exporter = PrometheusMetricsExporter(telemetry, anomalies)
    return exporter.generate()