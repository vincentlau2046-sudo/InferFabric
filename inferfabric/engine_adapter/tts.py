
"""TTSAdapter — text-to-speech server.
Health: /health  |  Metrics: none
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from inferfabric.engine_adapter.base import EngineAdapter
from inferfabric.engine_adapter import register
from inferfabric.health import check_http_status
if TYPE_CHECKING:
    from inferfabric.config import ModelConfig

class TTSAdapter(EngineAdapter):
    def __init__(self, process_manager=None):
        self._proc = process_manager

    @property
    def engine_type(self) -> str:
        return "tts_server"

    def check_health(self, model: ModelConfig) -> str:
        if not model.tts:
            return "?"
        url = model.tts.health_url or f"http://localhost:{model.tts.port}/health"
        return check_http_status(url)

    def get_context_window(self, model: ModelConfig) -> int | None:
        return None

    def validate_config(self, model: ModelConfig) -> list[str]:
        issues = []
        if not model.tts:
            return ["Missing tts config block"]
        if not model.tts.conda_env:
            issues.append("tts.conda_env is empty")
        if model.tts.port <= 0:
            issues.append(f"Invalid tts.port: {model.tts.port}")
        if not model.tts.start_cmd:
            issues.append("tts.start_cmd is empty")
        return issues

    def start(self, model: ModelConfig) -> dict:
        """Start tts via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set — call set_process_manager() first")
        cfg = getattr(model, 'tts')
        return self._proc.start_tts_server(cfg)

    def stop(self, model: ModelConfig) -> dict:
        """Stop tts via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = getattr(model, 'tts')
        return self._proc.stop_tts_server(port=cfg.port)

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "\u2705"

register("tts_server", TTSAdapter)
