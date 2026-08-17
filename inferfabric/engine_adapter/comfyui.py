
"""ComfyUIAdapter — image generation server.
Health: /system_stats  |  Metrics: none
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from inferfabric.engine_adapter.base import EngineAdapter
from inferfabric.engine_adapter import register
from inferfabric.health import check_http_status
if TYPE_CHECKING:
    from inferfabric.config import ModelConfig

class ComfyUIAdapter(EngineAdapter):
    def __init__(self, process_manager=None):
        self._proc = process_manager

    @property
    def engine_type(self) -> str:
        return "comfyui"

    def check_health(self, model: ModelConfig) -> str:
        if not model.comfyui:
            return "?"
        url = model.comfyui.health_url or f"http://localhost:{model.comfyui.port}/system_stats"
        return check_http_status(url)

    def get_context_window(self, model: ModelConfig) -> int | None:
        return None  # image generation, no context window

    def validate_config(self, model: ModelConfig) -> list[str]:
        issues = []
        if not model.comfyui:
            return ["Missing comfyui config block"]
        if not model.comfyui.conda_env:
            issues.append("comfyui.conda_env is empty")
        if model.comfyui.port <= 0:
            issues.append(f"Invalid comfyui.port: {model.comfyui.port}")
        return issues

    def start(self, model: ModelConfig) -> dict:
        """Start comfyui via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set — call set_process_manager() first")
        cfg = getattr(model, 'comfyui')
        return self._proc.start_comfyui(cfg)

    def stop(self, model: ModelConfig) -> dict:
        """Stop comfyui via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = getattr(model, 'comfyui')
        return self._proc.stop_comfyui(port=cfg.port)

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "\u2705"

register("comfyui", ComfyUIAdapter)
