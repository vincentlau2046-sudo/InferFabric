"""
inferfabric/manager.py — Model orchestration layer (v4.0, slim facade).

v4.0: Profile concept eliminated. Models are self-describing plugins.
Tri-state GPU mode: idle / exclusive / shared.
Switch rules enforced by validate_transition().

This file is now a thin facade — GPU state machine logic lives in
gpu_state.py, model lifecycle operations live in model_lifecycle.py.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import VLLMConfig, ComfyUIConfig

from .config import (
    MODELS_DIR,
    DEFAULT_STATE_DB,
    ModelConfig,
    load_models,
    # Legacy
    Profile,
    load_profiles,
)
from .state import StateDB, ProfileState, GPUMode, validate_transition
from .gpu_lock import GPULock
from .process_manager import ProcessManager
from .health import (
    gpu_used_mb,
    gpu_total_mb,
    check_http_status,
)
from .health_checker import DefaultHealthChecker
from .interfaces import IStateDB, IProcessManager, IHealthChecker, IGPULock
from .model_discovery import discover_local_models as _discover_local_models
from .model_discovery import auto_deploy as _auto_deploy
from .gpu_state import GpuStateMachine
from .model_lifecycle import ModelLifecycle

log = logging.getLogger("inferfabric")


class ModelManager:
    """Orchestrates GPU resource allocation via model plugins.

    GPU Mode State Machine:
      idle → exclusive: deploy exclusive model, GPU fully locked
      idle → shared:    deploy shared model/service
      exclusive → idle:  stop exclusive model
      shared → idle:     stop all shared services
      shared → shared:   add/remove shared service (hot-plug V1: full restart)

      ❌ exclusive → shared:  must idle first
      ❌ shared → exclusive:  must idle first
    """

    def __init__(
        self,
        models_dir: str | Path = str(MODELS_DIR),
        state_db_path: str | Path = str(DEFAULT_STATE_DB),
        proc: IProcessManager | None = None,
        state: IStateDB | None = None,
        lock: IGPULock | None = None,
        health: IHealthChecker | None = None,
    ):
        """Initialize ModelManager.

        All dependency parameters are optional — when omitted the concrete
        implementations are used (runtime zero-overhead structural typing).
        """
        self.models_dir = Path(models_dir)
        self.state = state if state is not None else StateDB(Path(state_db_path))
        self._lock = lock if lock is not None else GPULock()
        self._proc = proc if proc is not None else ProcessManager(self.state)
        self._health = health if health is not None else DefaultHealthChecker()
        self._models = load_models(self.models_dir)

        # Sub-module instances
        self._gpu_state = GpuStateMachine(
            self.state, self._proc, self._health, self._lock, self._models,
        )
        self._lifecycle = ModelLifecycle(
            self.state, self._proc, self._health, self._lock,
            self._gpu_state, self._models, self.models_dir,
        )

    # ── Properties (thin) ─────────────────────────────────────────

    @property
    def gpu_mode(self) -> str:
        return self.state.gpu_mode

    @property
    def active_services(self) -> list[str]:
        return self.state.get_active_services()

    @property
    def current_service(self) -> Optional[str]:
        """For backward compat — returns first active service or 'idle'."""
        services = self.active_services
        return services[0] if services else "idle"

    # ── Model Lookup ─────────────────────────────────────────────

    def get_model(self, name: str) -> Optional[ModelConfig]:
        """Get model config by name."""
        return self._models.get(name)

    def list_models(self) -> list[dict]:
        """List all available models from models.d/. Skips ollama_daemon entries."""
        def _get_context(m):
            """Get context window in K units."""
            if m.vllm:
                return m.vllm.max_model_len
            if m.ollama_cpp:
                return m.ollama_cpp.context_size
            return None

        return [
            {
                "name": m.name,
                "description": m.description,
                "mode": m.gpu_role,
                "type": m.type,
                "active": self._gpu_state._is_model_actively_running(m),
                "model_type": getattr(m, "model_type", "llm"),
                "modality": getattr(m, "modality", "text"),
                "quantization": getattr(m, "quantization", ""),
                "context_window": _get_context(m),
            }
            for m in self._models.values()
            if m.type != "ollama_daemon"
        ]

    def find_model_by_served_name(self, served_name: str) -> Optional[ModelConfig]:
        """Find model by its served_name across all backend types (for proxy routing)."""
        for m in self._models.values():
            if m.served_name == served_name:
                return m
        # Fallback: match by model name
        for m in self._models.values():
            if m.name == served_name:
                return m
        return None

    # ── Health Check (tri-state) ─────────────────────────────────

    def check_vllm_health(self, port: int) -> str:
        return check_http_status(f"http://localhost:{port}/health")

    def check_comfyui_health(self, url: str) -> str:
        return check_http_status(url)

    def check_http_health(self, port: int, path: str = "/health") -> str:
        """Generic HTTP health check for any backend."""
        return check_http_status(f"http://localhost:{port}{path}")

    def check_ollama_health(self, port: int = 11434) -> str:
        """Check if Ollama daemon is running."""
        return check_http_status(f"http://localhost:{port}/api/tags")

    # ── Switch ────────────────────────────────────────────────────

    def switch(self, target: str) -> dict:
        """Switch to target model/service.

        Two orthogonal paths dispatched by gpu_role:
          - gpu_role == "none"  → _switch_independent() — does NOT touch the
            GPU state machine (can coexist with idle/exclusive/shared).
          - gpu_role != "none" → GPU state machine path, enforcing tri-state:
              idle → exclusive/shared: allowed
              exclusive → idle: allowed
              shared → idle: allowed
              shared → shared: allowed (add/remove service, V1: full restart)
              exclusive → exclusive: allowed (same-port swap)
              exclusive → shared:    ❌ must idle first
              shared → exclusive:    ❌ must idle first
        """
        # ── Handle idle ──────────────────────────────────────────────
        if target == "idle":
            return self._lifecycle._switch_to_idle()

        # ── Model lookup & already-running check ──────────────────────
        model = self._models.get(target)
        if not model:
            return {"status": "error", "message": f"Unknown model: {target}. Available: {list(self._models.keys())}"}

        # Already running? (P0-1: check config drift before skipping)
        if target in self.active_services:
            if model.is_vllm and model.vllm:
                # Reload YAML to detect config drift against disk
                self._models = load_models(self.models_dir)
                model = self._models.get(target)
                if model is None:
                    log.warning("YAML for %s not found after reload — skipping drift check", target)
                    return {"status": "already_active", "model": target}
                changed = self._gpu_state._check_model_config_changed(model)
                if changed:
                    log.info("Config changed for %s — restarting", target)
                    self._lifecycle._switch_to_idle()
                else:
                    return {"status": "already_active", "model": target}
            else:
                return {"status": "already_active", "model": target}

        # ── Dispatch by gpu_role ────────────────────────────────────
        # Path A: GPU-independent models — do not touch the GPU state machine.
        if model.gpu_role == "none":
            return self._lifecycle._switch_independent(model)

        # Path B: GPU-bound models — go through the tri-state GPU state machine.
        target_mode = model.gpu_role  # 'exclusive' or 'shared'
        current_mode = self.gpu_mode

        # ── Validate GPU mode transition ──────────────────────────────
        # Validate transition — allow exclusive → exclusive (same-port swap)
        if not validate_transition(current_mode, target_mode):
            # Allow exclusive → exclusive: stop old, start new
            if current_mode == GPUMode.EXCLUSIVE and target_mode == GPUMode.EXCLUSIVE:
                pass  # Allowed below
            elif current_mode == GPUMode.EXCLUSIVE:
                return {
                    "status": "error",
                    "message": f"GPU is in exclusive mode ({self.active_services[0] if self.active_services else 'unknown'} running). "
                               f"Run 'iff switch idle' first.",
                }
            elif current_mode == GPUMode.SHARED and target_mode == GPUMode.EXCLUSIVE:
                return {
                    "status": "error",
                    "message": f"GPU is in shared mode ({', '.join(self.active_services) if self.active_services else 'none'} running). "
                               f"Run 'iff switch idle' first to deploy exclusive model.",
                }
            else:
                return {"status": "error", "message": f"Invalid transition: {current_mode} → {target_mode}"}

        # ── Acquire lock, deploy, and record ──────────────────────────
        # Acquire GPU lock
        if not self._lock.acquire():
            return {"status": "error", "message": "GPU switch in progress (lock held)"}

        t0 = time.time()
        from_services = list(self.active_services)
        log.info("Switch: %s → %s (gpu_mode: %s → %s)", from_services, target, current_mode, target_mode)

        self.state.set_multi({"profile_state": ProfileState.SWITCHING, "switching_target": target})

        try:
            if current_mode == GPUMode.IDLE:
                # Fresh start — just deploy
                result = self._lifecycle._deploy_model(model, target_mode)
            elif current_mode == GPUMode.EXCLUSIVE and target_mode == GPUMode.EXCLUSIVE:
                # Exclusive → exclusive: stop old, start new (same-port swap)
                result = self._lifecycle._switch_exclusive(model)
            elif current_mode == GPUMode.SHARED and target_mode == GPUMode.SHARED:
                # V1: full restart — stop all, then start all including new one
                result = self._lifecycle._shared_add_service(model)
            else:
                result = {"status": "error", "message": f"Unexpected state: {current_mode} → {target_mode}"}

            # Record history
            elapsed = round(time.time() - t0, 1)
            result_status = result.get("status")
            status = "ok" if result_status in ("switched", "already_active") else "error"
            from_label = ",".join(from_services) if from_services else "idle"
            self.state.add_history(from_label, target, elapsed, status)

            # P2-4: Guard against leaked gpu_mode on non-exception failure.
            if status == "error" and current_mode != GPUMode.IDLE:
                actual = self.state.gpu_mode
                if actual == target_mode:
                    log.warning("Switch failed but gpu_mode leaked as %s — rolling back to %s",
                                actual, current_mode)
                    self.state.set("gpu_mode", current_mode)

            # P2-4: Ensure profile_state is reset on non-exception failure
            # (lifecycle may leave it at SWITCHING if it returned error without setting it).
            if status == "error":
                try:
                    self.state.set("profile_state", ProfileState.ERROR)
                except Exception:
                    log.warning("Failed to set profile_state=ERROR after switch failure")

            return result

        except Exception as e:
            log.exception("Switch failed")
            # P2-4: Roll back gpu_mode to the pre-switch value.
            self.state.set("gpu_mode", current_mode)
            self.state.set("profile_state", ProfileState.ERROR)
            self.state.add_history(",".join(from_services), target, time.time() - t0, "error")
            return {"status": "error", "message": str(e)}
        finally:
            self.state.set("switching_target", "")
            self._lock.release()

    # ── Status ────────────────────────────────────────────────────

    def status(self) -> dict:
        active = list(self.active_services)
        sleep_states = self.state.get_all_sleep_states()
        services_status = {}
        services_info = {}
        dead_services = []
        for svc_name in active:
            m = self._models.get(svc_name)
            if not m:
                dead_services.append(svc_name)
                continue
            info = {"mode": m.gpu_role, "type": m.type}
            if m.port:
                info["port"] = m.port
            if m.vllm and m.vllm.port:
                info["port"] = m.vllm.port
            health = self._health.check_model(m)
            # Append sleep state if applicable
            sleep_label = sleep_states.get(svc_name, "")
            if sleep_label:
                health = f"{health} (sleeping {sleep_label.upper()})"
            services_status[svc_name] = health
            services_info[svc_name] = info
            # Collect dead services (excluded from returned active list)
            if health == "❌":
                dead_services.append(svc_name)

        active_services = [s for s in active if s not in dead_services]

        return {
            "gpu_mode": self.gpu_mode,
            "active_services": active_services,
            "services_health": services_status,
            "services_info": services_info,
            "sleep_states": sleep_states,
            "gpu_used_mb": gpu_used_mb(),
            "gpu_total_mb": gpu_total_mb(),
            "gpu_util_pct": gpu_used_mb() / gpu_total_mb() * 100 if gpu_total_mb() > 0 else 0.0,
            "vllm_pid": self._proc.vllm_pid,
            "comfyui_pid": self._proc.comfyui_pid,
        }

    # ── Delegation: GpuStateMachine ───────────────────────────────

    def reconcile(self) -> dict:
        return self._gpu_state.reconcile()

    def cleanup_dead_services(self) -> list[str]:
        return self._gpu_state.cleanup_dead_services()

    def force_reset(self) -> dict:
        return self._gpu_state.force_reset()

    # ── Delegation: ModelLifecycle ────────────────────────────────

    def stop_service(self, name: str) -> dict:
        return self._lifecycle.stop_service(name)

    def stop_independent(self, name: str) -> dict:
        return self._lifecycle.stop_independent(name)

    def list_independent(self) -> list[str]:
        return self._lifecycle.list_independent()

    def sleep_model(self, name: str) -> dict:
        return self._lifecycle.sleep_model(name)

    def wake_model(self, name: str) -> dict:
        return self._lifecycle.wake_model(name)

    # ── Discovery ─────────────────────────────────────────────────

    def discover_local_models(self) -> dict:
        """Scan ~/models/, ~/ComfyUI/models/, and Ollama for unconfigured models.

        Delegates to model_discovery.discover_local_models().
        """
        return _discover_local_models(self.models_dir)

    def auto_deploy(self, name: str, model_type: str) -> dict:
        """Auto-generate YAML and deploy a discovered model.

        Delegates to model_discovery.auto_deploy() for YAML generation,
        then reloads config and switches.
        """
        return _auto_deploy(name, model_type, self.models_dir, self._models, self.switch)

    def pull_model(self, name: str, framework: str) -> dict:
        """Pull/download a model before deployment."""
        import subprocess, re
        # Validate name format to prevent abuse (allow ollama tags like qwen2.5:7b, library/model:tag)
        # Disallow path traversal (..) even though subprocess list form prevents shell injection
        if not re.match(r'^(?!.*\.\.)[\w.\-:/]+$', name):
            return {"status": "error", "message": f"Invalid model name: {name!r}"}
        if framework == "ollama":
            try:
                result = subprocess.run(["ollama", "pull", name], capture_output=True, text=True, timeout=1800)
                if result.returncode == 0:
                    return {"status": "pulled", "message": f"ollama model {name} pulled"}
                return {"status": "error", "message": result.stderr[:200]}
            except FileNotFoundError:
                return {"status": "error", "message": "ollama command not found on PATH"}
            except subprocess.TimeoutExpired:
                return {"status": "error", "message": "ollama pull timed out after 1800s"}
        elif framework in ("vllm", "ollama_cpp"):
            return {"status": "error", "message": f"{framework} pull not supported yet — download manually"}
        else:
            return {"status": "error", "message": f"Unknown framework: {framework}"}


# ─── Backward Compatibility ──────────────────────────────────────

class ProfileManager(ModelManager):
    """Backward-compatible alias. All v3.x code using ProfileManager will work."""
    pass