"""
unit/proxy/test_r6_metrics.py — R6: Prometheus /metrics 端点

测试对象: inferfabric.proxy.metrics_exporter.PrometheusMetricsExporter
覆盖范围:
  - 生成文本包含标准 HEADER/TYPE 行
  - 从空数据生成不会 crash
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
from inferfabric.proxy.metrics_exporter import PrometheusMetricsExporter, generate_metrics


def _make_telemetry(rows=None):
    t = MagicMock()
    t.query_request_log.return_value = rows or []
    return t


def _make_anomalies(events=None):
    a = MagicMock()
    a.query.return_value = events or []
    return a


class TestPrometheusMetricsExporter:

    def test_empty_returns_header_lines(self):
        """空数据仍输出 HELP/TYPE 头。"""
        text = generate_metrics(_make_telemetry(), _make_anomalies())
        assert "# HELP iff_uptime_seconds" in text
        assert "# TYPE iff_uptime_seconds gauge" in text
        assert "# HELP iff_requests_total" in text
        assert "# TYPE iff_requests_total counter" in text
        assert "# HELP iff_errors_total" in text
        assert "# HELP iff_tokens_in_total" in text
        assert "# HELP iff_tokens_out_total" in text
        assert "# HELP iff_anomalies_total" in text

    def test_request_counts(self):
        """请求日志产生 iff_requests_total 行。"""
        t = _make_telemetry(rows=[
            {"model": "haiku", "route": "cloud", "status": 200, "tokens_in": 10, "tokens_out": 5},
            {"model": "haiku", "route": "cloud", "status": 200, "tokens_in": 20, "tokens_out": 15},
            {"model": "qwen38", "route": "local", "status": 503, "tokens_in": 0, "tokens_out": 0},
        ])
        text = generate_metrics(t, _make_anomalies())
        assert 'iff_requests_total{model="haiku",route="cloud",status="200"} 2' in text
        assert 'iff_requests_total{model="qwen38",route="local",status="503"} 1' in text

    def test_token_counts(self):
        """token 聚合。"""
        t = _make_telemetry(rows=[
            {"model": "haiku", "route": "cloud", "status": 200, "tokens_in": 100, "tokens_out": 50},
            {"model": "haiku", "route": "cloud", "status": 200, "tokens_in": 200, "tokens_out": 150},
        ])
        text = generate_metrics(t, _make_anomalies())
        assert 'iff_tokens_in_total{model="haiku"} 300' in text
        assert 'iff_tokens_out_total{model="haiku"} 200' in text

    def test_error_counts(self):
        """错误计数（仅非 200）。"""
        t = _make_telemetry(rows=[
            {"model": "haiku", "route": "cloud", "status": 503, "error": "upstream_error"},
            {"model": "haiku", "route": "cloud", "status": 503, "error": "upstream_error"},
            {"model": "qwen38", "route": "local", "status": 200, "error": None},
        ])
        text = generate_metrics(t, _make_anomalies())
        assert 'iff_errors_total{model="haiku",error="upstream_error"} 2' in text
        assert 'iff_errors_total{model="qwen38"' not in text

    def test_anomaly_counts(self):
        """Anomaly 事件聚合。"""
        from inferfabric.anomaly_collector import AnomalyEvent
        a = _make_anomalies(events=[
            AnomalyEvent(category="routing", severity="warning", message="x"),
            AnomalyEvent(category="routing", severity="warning", message="y"),
            AnomalyEvent(category="model", severity="error", message="z"),
        ])
        text = generate_metrics(_make_telemetry(), a)
        assert 'iff_anomalies_total{category="routing",severity="warning"} 2' in text
        assert 'iff_anomalies_total{category="model",severity="error"} 1' in text

    def test_uptime_is_numeric(self):
        """uptime 是数字。"""
        text = generate_metrics(_make_telemetry(), _make_anomalies())
        for line in text.split("\n"):
            if line.startswith("iff_uptime_seconds "):
                val = float(line.split()[1])
                assert val >= 0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))