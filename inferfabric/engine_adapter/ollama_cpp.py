
"""OllamaCppAdapter — llama-server (CPU) adapter.
Health: /health returns {"status":"ok"}  |  Metrics: not exposed
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from inferfabric.engine_adapter.base import EngineAdapter
from inferfabric.engine_adapter import register
from inferfabric.health import check_http_status
if TYPE_CHECKING:
    from inferfabric.config import ModelConfig

class OllamaCppAdapter(EngineAdapter):
    def __init__(self, process_manager=None):
        self._proc = process_manager

    @property
    def engine_type(self) -> str:
        return "ollama_cpp"

    def check_health(self, model: ModelConfig) -> str:
        if not model.ollama_cpp:
            return "?"
        return check_http_status(f"http://localhost:{model.ollama_cpp.port}/health")

    def get_context_window(self, model: ModelConfig) -> int | None:
        if not model.ollama_cpp:
            return None
        return model.ollama_cpp.context_size

    def validate_config(self, model: ModelConfig) -> list[str]:
        issues = []
        if not model.ollama_cpp:
            return ["Missing ollama_cpp config block"]
        if not model.ollama_cpp.model_path:
            issues.append("ollama_cpp.model_path is empty")
        if model.ollama_cpp.port <= 0:
            issues.append(f"Invalid ollama_cpp.port: {model.ollama_cpp.port}")
        return issues

    def start(self, model: ModelConfig) -> dict:
        """Start ollama_cpp via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set — call inject ._proc on the adapter instance first")
        cfg = getattr(model, 'ollama_cpp')
        return self._proc.start_ollama_cpp(cfg)

    def stop(self, model: ModelConfig) -> dict:
        """Stop ollama_cpp via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = getattr(model, 'ollama_cpp')
        return self._proc.stop_ollama_cpp(port=cfg.port)

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "\u2705"

    def get_metrics_flags(self, model: ModelConfig) -> list[str]:
        return []  # llama-server does not expose Prometheus metrics

    def fetch_engine_metrics(self, model: ModelConfig) -> dict | None:
        return None  # not supported


    def get_port(self, model: ModelConfig) -> int | None:
        return model.ollama_cpp.port if model.ollama_cpp else None

    def get_pid_state_key(self) -> str | None:
        return None

register("ollama_cpp", OllamaCppAdapter)
