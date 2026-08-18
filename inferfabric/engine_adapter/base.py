"""
EngineAdapter ABC — per-engine adapter interface.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from inferfabric.config import ModelConfig

class EngineAdapter(ABC):
    @property
    @abstractmethod
    def engine_type(self) -> str: ...

    @abstractmethod
    def check_health(self, model: ModelConfig) -> str: ...

    @abstractmethod
    def get_context_window(self, model: ModelConfig) -> int | None: ...

    def get_metadata(self, model: ModelConfig) -> dict:
        return {"context_window": self.get_context_window(model)}

    @abstractmethod
    def validate_config(self, model: ModelConfig) -> list[str]: ...

    @abstractmethod
    def start(self, model: ModelConfig) -> dict: ...

    @abstractmethod
    def stop(self, model: ModelConfig) -> dict: ...

    @abstractmethod
    def is_alive(self, model: ModelConfig) -> bool: ...

    def get_metrics_flags(self, model: ModelConfig) -> list[str]:
        return []

    def set_process_manager(self, proc) -> None:
        """Inject ProcessManager reference for lifecycle delegation."""
        self._proc = proc

    def fetch_engine_metrics(self, model: ModelConfig) -> dict | None:
        return None

    def sleep(self, model: ModelConfig) -> dict:
        """Suspend a model process (L2 sleep). Default: not supported."""
        return {"status": "error", "message": "Sleep not supported for this engine type"}

    def wake(self, model: ModelConfig) -> dict:
        """Resume a sleeping model process. Default: not supported."""
        return {"status": "error", "message": "Wake not supported for this engine type"}

    def get_pid(self, model: ModelConfig) -> int | None:
        """Return PID of the engine process, or None if unknown."""
        return None

    def get_port(self, model: ModelConfig) -> int | None:
        """Return engine port number, or None."""
        return None

    def get_pid_state_key(self) -> str | None:
        """Return the state.db key for storing PID, or None."""
        return None