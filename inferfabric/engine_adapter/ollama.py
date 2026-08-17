"""OllamaAdapter — external Ollama daemon model.

Ollama models are served through a shared external daemon (port 11434).
Startup/deployment is purely registration-based (no process to manage).
ollama_daemon entries are informational only (proxied by maintainer).
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from inferfabric.engine_adapter.base import EngineAdapter
from inferfabric.engine_adapter import register
from inferfabric.health import check_http_status
if TYPE_CHECKING:
    from inferfabric.config import ModelConfig


class OllamaAdapter(EngineAdapter):
    def __init__(self, process_manager=None):
        self._proc = process_manager

    @property
    def engine_type(self) -> str:
        return "ollama"

    def check_health(self, model: ModelConfig) -> str:
        return check_http_status("http://localhost:11434/api/tags")

    def get_context_window(self, model: ModelConfig) -> int | None:
        return None

    def validate_config(self, model: ModelConfig) -> list[str]:
        return []

    def start(self, model: ModelConfig) -> dict:
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        return self._proc._start_ollama_model(model)

    def stop(self, model: ModelConfig) -> dict:
        import logging; log = logging.getLogger("inferfabric.engine_adapter.ollama")
        log.info("Unregistering Ollama model %s", model.name)
        return {"status": "ok", "message": "ollama model unregistered"}

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "\u2705"


class OllamaDaemonAdapter(EngineAdapter):
    """Ollama daemon — external process managed by user."""
    def __init__(self, process_manager=None):
        self._proc = process_manager

    @property
    def engine_type(self) -> str:
        return "ollama_daemon"

    def check_health(self, model: ModelConfig) -> str:
        if not model.ollama_daemon:
            return "?"
        return check_http_status(f"http://localhost:{model.ollama_daemon.port}/api/tags")

    def get_context_window(self, model: ModelConfig) -> int | None:
        return None

    def validate_config(self, model: ModelConfig) -> list[str]:
        return []

    def start(self, model: ModelConfig) -> dict:
        return {"status": "ok", "message": "Ollama daemon external — verify with 'ollama serve'"}

    def stop(self, model: ModelConfig) -> dict:
        import logging; log = logging.getLogger("inferfabric.engine_adapter.ollama")
        log.info("Ollama daemon stop: use 'ollama serve' externally")
        return {"status": "ok", "message": "ollama daemon is external"}

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "\u2705"


register("ollama", OllamaAdapter)
register("ollama_daemon", OllamaDaemonAdapter)
