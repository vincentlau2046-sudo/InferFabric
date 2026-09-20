
"""VLLMAdapter — vLLM server (conda process-group or docker container,
dispatched by ModelConfig.resolved_deployment).
Health: /health 200  |  Metrics: /metrics (vllm:* Prometheus)
"""
from __future__ import annotations
import logging
import subprocess
from typing import TYPE_CHECKING
from inferfabric.engine_adapter.base import EngineAdapter
from inferfabric.engine_adapter import register
from inferfabric.health import check_http_status
if TYPE_CHECKING:
    from inferfabric.config import ModelConfig

log = logging.getLogger("inferfabric.vllm_adapter")

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
        if model.resolved_deployment == "conda" and not model.vllm.conda_env:
            issues.append("vllm.conda_env is empty (required for conda deployment)")
        if model.vllm.port <= 0:
            issues.append(f"Invalid vllm.port: {model.vllm.port}")
        if not (0 < model.vllm.gpu_memory_utilization <= 1):
            issues.append(f"gpu_memory_utilization out of range: {model.vllm.gpu_memory_utilization}")
        return issues

    def start(self, model: ModelConfig) -> dict:
        """Start vllm via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set — call inject ._proc on the adapter instance first")
        cfg = getattr(model, 'vllm')
        return self._proc.start_vllm(cfg)

    def stop(self, model: ModelConfig) -> dict:
        """Stop vllm, dispatching by deployment: docker → docker stop, conda → process-group kill."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        if model.resolved_deployment == "docker":
            return self._stop_docker(model)
        cfg = getattr(model, 'vllm')
        return self._proc.stop_vllm(port=cfg.port)

    def _stop_docker(self, model: ModelConfig) -> dict:
        """Stop a docker-deployed vllm container.

        Inlined here; Task 3.4 extracts it to the base helper
        (_stop_docker_container) shared by all docker adapters.
        """
        name = model.container_name
        if not name:
            return {"status": "warning", "message": "docker deployment has no container_name — cannot stop"}
        try:
            result = subprocess.run(["docker", "stop", name], timeout=30, capture_output=True, check=False)
        except subprocess.TimeoutExpired:
            return {"status": "warning", "message": f"docker stop {name} timed out"}
        if result.returncode == 0:
            return {"status": "ok", "message": f"Container {name} stopped"}
        msg = result.stderr.decode()[:200] if result.stderr else ""
        return {"status": "warning", "message": f"docker stop exit {result.returncode}: {msg}"}

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "\u2705"

    def fetch_engine_metrics(self, model: ModelConfig) -> dict | None:
        if not model.vllm:
            return None
        from inferfabric.prometheus import VllmMetricsCollector, parse_prometheus_text
        try:
            from urllib.request import urlopen
            with urlopen(f"http://127.0.0.1:{model.vllm.port}/metrics", timeout=10) as r:
                text = r.read().decode("utf-8")
        except Exception as e:
            log.warning("vllm metrics fetch failed for %s: %s", model.name, e)
            return None
        gauges, counters, histos = parse_prometheus_text(text)
        result = VllmMetricsCollector.compute(model.vllm.port, gauges, counters, histos, prefix="vllm:")
        result["sleep_state"] = 0
        return result if result else {"sleep_state": 0}

    def sleep(self, model: ModelConfig) -> dict:
        """Suspend vLLM process (L2 sleep)."""
        if self._proc is None:
            return {"status": "error", "message": "No process manager set"}
        if not model.vllm:
            return {"status": "error", "message": "Missing vllm config"}
        return self._proc.sleep_vllm(model.vllm.port)

    def wake(self, model: ModelConfig) -> dict:
        """Resume sleeping vLLM process."""
        if self._proc is None:
            return {"status": "error", "message": "No process manager set"}
        if not model.vllm:
            return {"status": "error", "message": "Missing vllm config"}
        return self._proc.wake_vllm(model.vllm.port)

    def get_pid(self, model: ModelConfig) -> int | None:
        """Return vLLM PID if available."""
        if self._proc is None:
            return None
        return self._proc.vllm_pid


    def get_port(self, model: ModelConfig) -> int | None:
        return model.vllm.port if model.vllm else None

    def get_pid_state_key(self) -> str | None:
        return 'vllm_pid'

register("vllm", VLLMAdapter)
