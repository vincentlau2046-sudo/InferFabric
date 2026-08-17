
"""VLLMAdapter — conda-env-based vLLM server.
Health: /health 200  |  Metrics: /metrics (vllm:* Prometheus)
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from inferfabric.engine_adapter.base import EngineAdapter
from inferfabric.engine_adapter import register
from inferfabric.health import check_http_status
if TYPE_CHECKING:
    from inferfabric.config import ModelConfig

class VLLMAdapter(EngineAdapter):
    def __init__(self, process_manager=None):
        self._proc = process_manager

    @property
    def engine_type(self) -> str:
        return "vllm"

    def check_health(self, model: ModelConfig) -> str:
        if not model.vllm:
            return "?"
        return check_http_status(f"http://localhost:{model.vllm.port}/health")

    def get_context_window(self, model: ModelConfig) -> int | None:
        if not model.vllm:
            return None
        return model.vllm.max_model_len

    def validate_config(self, model: ModelConfig) -> list[str]:
        issues = []
        if not model.vllm:
            return ["Missing vllm config block"]
        if not model.vllm.model_dir:
            issues.append("vllm.model_dir is empty")
        if not model.vllm.served_name:
            issues.append("vllm.served_name is empty")
        if not model.vllm.conda_env:
            issues.append("vllm.conda_env is empty")
        if model.vllm.port <= 0:
            issues.append(f"Invalid vllm.port: {model.vllm.port}")
        if not (0 < model.vllm.gpu_memory_utilization <= 1):
            issues.append(f"gpu_memory_utilization out of range: {model.vllm.gpu_memory_utilization}")
        return issues

    def start(self, model: ModelConfig) -> dict:
        """Start vllm via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set — call set_process_manager() first")
        cfg = getattr(model, 'vllm')
        return self._proc.start_vllm(cfg)

    def stop(self, model: ModelConfig) -> dict:
        """Stop vllm via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = getattr(model, 'vllm')
        return self._proc.stop_vllm(port=cfg.port)

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "\u2705"

    def fetch_engine_metrics(self, model: ModelConfig) -> dict | None:
        from inferfabric.token_stats import parse_prometheus_text
        if not model.vllm:
            return None
        try:
            from urllib.request import urlopen
            with urlopen(f"http://127.0.0.1:{model.vllm.port}/metrics", timeout=10) as r:
                text = r.read().decode("utf-8")
        except Exception:
            return None
        _g, counters, histos = parse_prometheus_text(text)
        ph = histos.get("vllm:request_prompt_tokens")
        gh = histos.get("vllm:request_generation_tokens")
        rt = counters.get("vllm:num_requests_completed")
        result = {}
        if ph: result["prompt_sum"] = int(ph["sum"])
        if gh: result["gen_sum"] = int(gh["sum"])
        if rt is not None: result["req_total"] = int(rt)
        return result if result else None

register("vllm", VLLMAdapter)
