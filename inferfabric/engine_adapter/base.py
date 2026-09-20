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

    def _stop_docker_container(self, model: ModelConfig, timeout: int = 30) -> dict:
        """docker stop <model.container_name>. Shared by docker-deployed adapters.

        Reads the unified ModelConfig.container_name property (Task 1.2).
        Guards: missing name, docker not on PATH, timeout, non-zero exit.
        """
        import subprocess
        import logging
        log = logging.getLogger("inferfabric")
        name = model.container_name
        if not name:
            return {"status": "warning",
                    "message": "docker deployment has no container_name — cannot stop"}
        log.info("Stopping docker container: %s", name)
        try:
            result = subprocess.run(
                ["docker", "stop", name],
                timeout=timeout, capture_output=True, check=False)
        except subprocess.TimeoutExpired:
            return {"status": "warning", "message": f"docker stop {name} timed out"}
        except FileNotFoundError:
            return {"status": "warning", "message": "docker not found on PATH"}
        if result.returncode == 0:
            return {"status": "ok", "message": f"Container {name} stopped"}
        msg = result.stderr.decode()[:200] if result.stderr else ""
        return {"status": "warning",
                "message": f"docker stop exit {result.returncode}: {msg}"}