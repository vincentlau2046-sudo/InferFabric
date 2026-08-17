
"""ASRAdapter — speech recognition server.
Health: /health  |  Metrics: none
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from inferfabric.engine_adapter.base import EngineAdapter
from inferfabric.engine_adapter import register
from inferfabric.health import check_http_status
if TYPE_CHECKING:
    from inferfabric.config import ModelConfig

class ASRAdapter(EngineAdapter):
    def __init__(self, process_manager=None):
        self._proc = process_manager

    @property
    def engine_type(self) -> str:
        return "asr_server"

    def check_health(self, model: ModelConfig) -> str:
        if not model.asr:
            return "?"
        url = model.asr.health_url or f"http://localhost:{model.asr.port}/health"
        return check_http_status(url)

    def get_context_window(self, model: ModelConfig) -> int | None:
        return None

    def validate_config(self, model: ModelConfig) -> list[str]:
        issues = []
        if not model.asr:
            return ["Missing asr config block"]
        if not model.asr.conda_env:
            issues.append("asr.conda_env is empty")
        if model.asr.port <= 0:
            issues.append(f"Invalid asr.port: {model.asr.port}")
        if not model.asr.start_cmd:
            issues.append("asr.start_cmd is empty")
        return issues

    def start(self, model: ModelConfig) -> dict:
        """Start asr via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set — call set_process_manager() first")
        cfg = getattr(model, 'asr')
        return self._proc.start_asr_server(cfg)

    def stop(self, model: ModelConfig) -> dict:
        """Stop asr via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = getattr(model, 'asr')
        return self._proc.stop_asr_server(port=cfg.port)

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "\u2705"

register("asr_server", ASRAdapter)
