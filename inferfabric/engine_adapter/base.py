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

    # ── 场景预设调优（iff tune）可选钩子 ─────────────────────────────
    # tune.py 只依赖这四个方法；不实现 = 安全默认（该引擎暂不支持调优）。
    # 未来引擎（vLLM/SGLang 等）各自实现即可，CLI/Dashboard/编排零改动。

    def scenario_fields(self, model: ModelConfig) -> list[str]:
        """允许被场景预设修改的引擎 config 字段白名单。空 = 不支持调优。"""
        return []

    def engine_caps(self, model: ModelConfig) -> dict[str, dict]:
        """字段 → 钳制界限 {min, max, step}（None/缺省 = 不限制）。"""
        return {}

    def validate_scenario(self, model: ModelConfig, values: dict) -> list[str]:
        """场景应用前的引擎级校验/提示（相反推导全部落在 tune.py）。

        values: 已钳制后的目标字段值。返回 issue 字符串列表（前缀 ⚠ = 警告，
        ❌ 或非前缀 = 阻止项——tune 对带 '❌' 的场次拒绝应用）。空 = 通过。
        """
        return []

    def restart(self, model: ModelConfig) -> dict:
        """停→启引擎，让新参数生效。默认 stop()+start()。

        子类可覆盖为带 GPU 状态机编排的重启（如 NInfer 经 ModelManager
        stop_service→switch，在途请求收 503+Retry-After）。
        """
        stop_r = self.stop(model)
        if stop_r.get("status") == "error":
            return {"status": "error", "message": f"stop 失败: {stop_r.get('message')}",
                    "step": "stop", "detail": stop_r}
        start_r = self.start(model)
        return {"status": start_r.get("status", "error"),
                "message": f"restart: {start_r.get('message')}",
                "stop_detail": stop_r, "start_detail": start_r}

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
        """docker stop <model.container_name>. Used by vllm-docker stop (ninfer migrated to PM launcher).

        Reads the unified ModelConfig.container_name property (Task 1.2).
        Guards: missing name, docker not on PATH, timeout, non-zero exit.
        Logs a warning on each failure branch (restores observability lost when
        the inline _switch_to_idle ninfer docker stop was extracted here).
        """
        import subprocess
        import logging
        log = logging.getLogger("inferfabric")
        name = model.container_name
        if not name:
            msg = "docker deployment has no container_name — cannot stop"
            log.warning(msg)
            return {"status": "warning", "message": msg}
        log.info("Stopping docker container: %s", name)
        try:
            result = subprocess.run(
                ["docker", "stop", name],
                timeout=timeout, capture_output=True, check=False)
        except subprocess.TimeoutExpired:
            msg = f"docker stop {name} timed out"
            log.warning(msg)
            return {"status": "warning", "message": msg}
        except FileNotFoundError:
            msg = "docker not found on PATH"
            log.warning(msg)
            return {"status": "warning", "message": msg}
        if result.returncode == 0:
            return {"status": "ok", "message": f"Container {name} stopped"}
        stderr_fragment = result.stderr.decode()[:200] if result.stderr else ""
        msg = f"docker stop exit {result.returncode}: {stderr_fragment}"
        log.warning(msg)
        return {"status": "warning", "message": msg}