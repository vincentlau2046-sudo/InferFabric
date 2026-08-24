"""
inferfabric/proxy/metrics.py — vLLM Prometheus metrics & EMA throughput tracker.

Re-exports from inferfabric.prometheus (single source of truth).
"""
from inferfabric.prometheus import (            # noqa: F401
    parse_prometheus_text,
    quantile,
    VllmMetricsCollector,
    handle_vllm_metrics,
)