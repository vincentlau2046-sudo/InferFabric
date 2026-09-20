"""
SGLangAdapter — Docker-based SGLang server.
Health: /health 200  |  Metrics: /metrics (--enable-metrics)
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING
from inferfabric.engine_adapter.base import EngineAdapter
from inferfabric.engine_adapter import register
from inferfabric.health import check_http_status
if TYPE_CHECKING:
    from inferfabric.config import ModelConfig

log = logging.getLogger("inferfabric.sglang_adapter")

class SGLangAdapter(EngineAdapter):
    def __init__(self, process_manager=None):
        self._proc = process_manager

    @property
    def engine_type(self) -> str:
        return "sglang"

    def check_health(self, model: ModelConfig) -> str:
        if not model.sglang:
            return "?"
        return check_http_status(f"http://localhost:{model.sglang.port}/health")

    def get_context_window(self, model: ModelConfig) -> int | None:
        if not model.sglang:
            return None
        return model.sglang.context_length

    def validate_config(self, model: ModelConfig) -> list[str]:
        issues = []
        if not model.sglang:
            return ["Missing sglang config block"]
        if not model.sglang.model_dir:
            issues.append("sglang.model_dir is empty")
        if not model.sglang.served_name:
            issues.append("sglang.served_name is empty")
        if model.sglang.port <= 0:
            issues.append(f"Invalid sglang.port: {model.sglang.port}")
        if not (0 < model.sglang.mem_fraction <= 1):
            issues.append(f"mem_fraction out of range: {model.sglang.mem_fraction}")
        return issues

    def start(self, model: ModelConfig) -> dict:
        """Start sglang via ProcessManager delegation.

        container_name threaded from ModelConfig.container_name (single source).
        """
        if self._proc is None:
            raise RuntimeError("ProcessManager not set — call inject ._proc on the adapter instance first")
        cfg = getattr(model, 'sglang')
        return self._proc.start_sglang(cfg, model.container_name)

    def stop(self, model: ModelConfig) -> dict:
        """Stop sglang via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = getattr(model, 'sglang')
        return self._proc.stop_sglang(port=cfg.port, container_name=model.container_name)

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "✅"

    def get_metrics_flags(self, model: ModelConfig) -> list[str]:
        return ["--enable-metrics"]

    def fetch_engine_metrics(self, model: ModelConfig) -> dict | None:
        if not model.sglang:
            return None
        from inferfabric.prometheus import VllmMetricsCollector, parse_prometheus_text
        try:
            from urllib.request import urlopen
            with urlopen(f"http://127.0.0.1:{model.sglang.port}/metrics", timeout=10) as r:
                text = r.read().decode("utf-8")
        except Exception as e:
            log.warning("sglang metrics fetch failed for %s: %s", model.name, e)
            return None
        gauges, counters, histos = parse_prometheus_text(text)
        prefix = "sglang:"
        if not histos.get(prefix + "request_prompt_tokens") and histos.get("vllm:request_prompt_tokens"):
            prefix = "vllm:"
        result = VllmMetricsCollector.compute(model.sglang.port, gauges, counters, histos, prefix=prefix)
        result["sleep_state"] = 0
        return result if result else {"sleep_state": 0}

    def get_port(self, model: ModelConfig) -> int | None:
        return model.sglang.port if model.sglang else None

    def get_pid_state_key(self) -> str | None:
        return 'sglang_pid'

register("sglang", SGLangAdapter)
