"""
inferfabric/manager.py — Model orchestration layer (v4.0).

v4.0: Profile concept eliminated. Models are self-describing plugins.
Tri-state GPU mode: idle / exclusive / shared.
Switch rules enforced by validate_transition().
"""

import os
import sys
import json
import time
import subprocess
import logging
from pathlib import Path
from typing import Optional

from .config import (
    MODELS_DIR,
    DEFAULT_STATE_DB,
    GPU_FREE_TIMEOUT,
    GPU_FREE_THRESHOLD_MB,
    ModelConfig,
    load_models,
    # Legacy
    DEFAULT_PROFILES,
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
    wait_gpu_free,
)

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
    ):
        self.models_dir = Path(models_dir)
        self.state = StateDB(Path(state_db_path))
        self._lock = GPULock()
        self._proc = ProcessManager(self.state)
        self._models = load_models(self.models_dir)

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
        """List all available models from models.d/. Skips alias_map and ollama_daemon entries."""
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
                "active": self._is_model_actively_running(m),
                "model_type": getattr(m, "model_type", "llm"),
                "quantization": getattr(m, "quantization", ""),
                "context_window": _get_context(m),
            }
            for m in self._models.values()
            if m.type not in ("alias_map", "ollama_daemon")
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

    # ── Reconciliation ──────────────────────────────────────────

    @staticmethod
    def _port_pid(port: int) -> Optional[int]:
        """Return PID owning a TCP port via fuser, or None."""
        import subprocess, re
        result = subprocess.run(
            ["fuser", "-v", str(port) + "/tcp"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            m = re.search(r'(\d+)', result.stdout)
            return int(m.group(1)) if m else None
        return None

    def reconcile(self) -> dict:
        """Compare DB state against actual running processes. Fix inconsistencies."""
        db_gpu_mode = self.gpu_mode
        db_services = self.active_services

        # Scan all known model ports
        actual_services = []
        for name, m in self._models.items():
            if m.is_vllm:
                status = self.check_vllm_health(m.vllm.port)
                if status in ("✅", "⏳"):
                    actual_services.append(name)
                else:
                    # Health check failed — cross-verify via fuser before marking dead
                    if self._port_pid(m.vllm.port) is not None:
                        actual_services.append(name)
            elif m.is_comfyui:
                health_url = m.comfyui.health_url or f"http://localhost:{m.comfyui.port}/system_stats"
                status = self.check_comfyui_health(health_url)
                if status in ("✅", "⏳"):
                    actual_services.append(name)
            elif m.is_ollama_daemon:
                status = self.check_ollama_health(m.ollama_daemon.port)
                if status in ("✅", "⏳"):
                    actual_services.append(name)
            elif m.is_ollama_cpp:
                status = self.check_http_health(m.ollama_cpp.port, "/health")
                if status in ("✅", "⏳"):
                    actual_services.append(name)
            # Note: ollama model type doesn't have its own process — it's served by ollama-daemon

        actions: list[str] = []

        # Determine actual gpu_mode from running services (gpu_none services don't count)
        actual_gpu_mode = GPUMode.IDLE
        gpu_services = [s for s in actual_services if not (self._models.get(s) and self._models[s].is_gpu_none)]
        if gpu_services:
            # Check if any exclusive model is running
            for svc_name in actual_services:
                m = self._models.get(svc_name)
                if m and m.is_exclusive:
                    actual_gpu_mode = GPUMode.EXCLUSIVE
                    break
            if actual_gpu_mode == GPUMode.IDLE:
                actual_gpu_mode = GPUMode.SHARED

        # Fix state inconsistencies
        if actual_gpu_mode != db_gpu_mode:
            actions.append(f"DB gpu_mode='{db_gpu_mode}', actual='{actual_gpu_mode}' — updating")
            self.state.gpu_mode = actual_gpu_mode

        if set(actual_services) != set(db_services):
            actions.append(f"DB services={db_services}, actual={actual_services} — updating")
            self.state.set_active_services(actual_services)

        # Fix profile_state
        db_profile_state = self.state.get("profile_state") or "idle"
        if actual_services and db_profile_state != ProfileState.HEALTHY:
            actions.append(f"profile_state was '{db_profile_state}', services running → healthy")
            self.state.set("profile_state", ProfileState.HEALTHY)
        elif not actual_services and db_gpu_mode == GPUMode.IDLE and db_profile_state != ProfileState.IDLE:
            actions.append(f"profile_state was '{db_profile_state}', no services → idle")
            self.state.set("profile_state", ProfileState.IDLE)

        # P0-4: Fix orphan PID detection — check if PID process actually exists and owns a running port
        if self._proc.vllm_pid:
            try:
                os.killpg(self._proc.vllm_pid, 0)
            except (ProcessLookupError, PermissionError):
                # PID doesn't exist — check if any vLLM service is actually running
                # P0-4 fix: check if port has a live process via fuser
                has_live_vllm = False
                for svc_name in actual_services:
                    m = self._models.get(svc_name)
                    if m and m.is_vllm:
                        if self._port_pid(m.vllm.port) is not None:
                            has_live_vllm = True
                            break
                if not has_live_vllm:
                    actions.append(f"Orphan vllm_pid={self._proc.vllm_pid} dead — clearing")
                    self.state.set("vllm_pid", "")

        # P0-5: Check if PID exists but no services running — stale
        # P0-5 fix: verify via fuser before clearing to avoid health-check false negatives
        if self._proc.vllm_pid and not actual_services:
            has_live_vllm = False
            for name, m in self._models.items():
                if m.is_vllm:
                    if self._port_pid(m.vllm.port) is not None:
                        has_live_vllm = True
                        break
            if not has_live_vllm:
                actions.append(f"Stale vllm_pid={self._proc.vllm_pid} with no active services — clearing")
                self.state.set("vllm_pid", "")
            else:
                actions.append(f"vllm_pid={self._proc.vllm_pid} still owns port — keeping (health check false negative)")

        # P0-5: If a vLLM is actually running but PID is not tracked, recover via fuser
        if not self._proc.vllm_pid:
            for svc_name in actual_services:
                m = self._models.get(svc_name)
                if m and m.is_vllm:
                    pid = self._port_pid(m.vllm.port)
                    if pid is not None:
                        self.state.set("vllm_pid", str(pid))
                        actions.append(f"Recovered vllm_pid={pid} for {svc_name} via fuser")
                        break

        return {
            "db_gpu_mode": db_gpu_mode,
            "actual_gpu_mode": actual_gpu_mode,
            "db_services": db_services,
            "actual_services": actual_services,
            "actions": actions,
        }

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
        # Handle idle — stops ALL services including gpu_none models
        if target == "idle":
            return self._switch_to_idle()

        # Look up model
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
                changed = self._check_model_config_changed(model)
                if changed:
                    log.info("Config changed for %s — restarting", target)
                    self._switch_to_idle()
                else:
                    return {"status": "already_active", "model": target}
            else:
                return {"status": "already_active", "model": target}

        # ── Dispatch by gpu_role ────────────────────────────────────
        # Path A: GPU-independent models — do not touch the GPU state machine.
        if model.gpu_role == "none":
            return self._switch_independent(model)

        # Path B: GPU-bound models — go through the tri-state GPU state machine.
        target_mode = model.gpu_role  # 'exclusive' or 'shared'
        current_mode = self.gpu_mode

        # Validate transition — allow exclusive → exclusive (same-port swap)
        if not validate_transition(current_mode, target_mode):
            # Allow exclusive → exclusive: stop old, start new
            if current_mode == GPUMode.EXCLUSIVE and target_mode == GPUMode.EXCLUSIVE:
                pass  # Allowed below
            elif current_mode == GPUMode.EXCLUSIVE:
                return {
                    "status": "error",
                    "message": f"GPU is in exclusive mode ({running[0] if running else 'unknown'} running). "
                               f"Run 'iff switch idle' first.",
                }
            elif current_mode == GPUMode.SHARED and target_mode == GPUMode.EXCLUSIVE:
                return {
                    "status": "error",
                    "message": f"GPU is in shared mode ({running} running). "
                               f"Run 'iff switch idle' first to deploy exclusive model.",
                }
            else:
                return {"status": "error", "message": f"Invalid transition: {current_mode} → {target_mode}"}

        # Acquire GPU lock
        if not self._lock.acquire():
            return {"status": "error", "message": "GPU switch in progress (lock held)"}

        t0 = time.time()
        from_services = list(self.active_services)
        log.info("Switch: %s → %s (gpu_mode: %s → %s)", from_services, target, current_mode, target_mode)

        self.state.set("profile_state", ProfileState.SWITCHING)

        try:
            if current_mode == GPUMode.IDLE:
                # Fresh start — just deploy
                result = self._deploy_model(model, target_mode)
            elif current_mode == GPUMode.EXCLUSIVE and target_mode == GPUMode.EXCLUSIVE:
                # Exclusive → exclusive: stop old, start new (same-port swap)
                result = self._switch_exclusive(model)
            elif current_mode == GPUMode.SHARED and target_mode == GPUMode.SHARED:
                # V1: full restart — stop all, then start all including new one
                result = self._shared_add_service(model)
            else:
                result = {"status": "error", "message": f"Unexpected state: {current_mode} → {target_mode}"}

            # Record history
            elapsed = round(time.time() - t0, 1)
            status = "ok" if result.get("status") in ("switched", "already_active") else "error"
            from_label = ",".join(from_services) if from_services else "idle"
            self.state.add_history(from_label, target, elapsed, status)

            return result

        except Exception as e:
            log.exception("Switch failed")
            self.state.set("profile_state", ProfileState.ERROR)
            self.state.add_history(",".join(from_services), target, time.time() - t0, "error")
            return {"status": "error", "message": str(e)}
        finally:
            self._lock.release()

    # P0-1: Check if model config has changed since it was started
    def _check_model_config_changed(self, model: ModelConfig) -> bool:
        """Compare stored config hash against current YAML. Returns True if drifted."""
        try:
            current_hash = model.config_hash()
            stored_hash = self.state.get(f"config_hash:{model.name}")
            if stored_hash is None:
                # First check after upgrade — record hash, treat as no drift
                log.info("Config hash for %s not found, recording: %s", model.name, current_hash)
                self.state.set(f"config_hash:{model.name}", current_hash)
                return False
            if stored_hash != current_hash:
                log.info("Config drift detected for %s: stored=%s current=%s",
                         model.name, stored_hash, current_hash)
                return True
            return False
        except Exception:
            log.debug("Config hash check failed for %s: %s", model.name, sys.exc_info()[1])
            return False  # conservative: don't restart on check failure
    
    def _switch_exclusive(self, model: ModelConfig) -> dict:
        """Switch from one exclusive model to another (same-port swap).

        Stops the currently active exclusive model, clears active_services,
        then deploys the new one. Reuses the same port so proxy mapping is stable.
        """
        # Stop current active exclusive model
        current = list(self.active_services)
        for svc_name in current:
            log.info("Stopping current exclusive service: %s", svc_name)
            self._stop_service(svc_name)

        # Clear active state before deploying new model
        self.state.set("active_services", json.dumps([]))

        # Deploy the new model
        return self._deploy_model(model, GPUMode.EXCLUSIVE)

    def _switch_independent(self, model: ModelConfig) -> dict:
        """Switch to a GPU-independent model — does NOT change the GPU state machine.

        GPU-independent models (gpu_role == "none"):
          - Do not change self.gpu_mode
          - May coexist with any GPU mode (exclusive/shared/other none)
          - Launch the model process directly — framework determined by type
        """
        t0 = time.time()
        # GPU-independent models (cpu-only) can coexist with any GPU mode.

        # Reload YAML to detect config drift against disk (parity with GPU-bound path)
        model_name = model.name
        self._models = load_models(self.models_dir)
        model = self._models.get(model_name)
        if model is None:
            return {"status": "error", "message": f"Model {model_name} not found in YAML after reload"}

        # Start the model (centralized type dispatch)
        result = self._start_model(model)
        if result.get("status") not in ("healthy", "started", "ok"):
            return result

        # Update active_services + record config hash (gpu_mode stays unchanged)
        remaining = list(self.active_services)
        if model.name not in remaining:
            remaining.append(model.name)
        self.state.set_active_services(remaining)
        self.state.set(f"config_hash:{model.name}", model.config_hash())
        self.state.set("profile_state", ProfileState.HEALTHY)

        elapsed = round(time.time() - t0, 1)
        return {
            "status": "switched",
            "model": model.name,
            "gpu_mode": self.gpu_mode,  # unchanged
            "elapsed_sec": elapsed,
            "results": {model.name: result},
        }

    def _switch_to_idle(self) -> dict:
        """Stop all services (including GPU-independent models) and transition to idle."""
        current_mode = self.gpu_mode
        if current_mode == GPUMode.IDLE and not self.active_services:
            return {"status": "already_active", "model": "idle"}

        # P0-2: reconcile first to sync state before stopping
        log.info("Reconciling state before idle switch")
        self.reconcile()

        if not self._lock.acquire():
            return {"status": "error", "message": "GPU switch in progress (lock held)"}

        t0 = time.time()
        from_services = list(self.active_services)
        log.info("Switch to idle from %s (gpu_mode=%s)", from_services, current_mode)

        try:
            # ── Stop GPU-bound services with port-based cleanup ──
            ports = []
            comfyui_cfg = None
            for svc_name in from_services:
                m = self._models.get(svc_name)
                if m:
                    if m.is_vllm:
                        ports.append(("vllm", m.vllm.port))
                    elif m.is_comfyui:
                        ports.append(("comfyui", m.comfyui.port))
                        comfyui_cfg = m.comfyui
            self._proc.stop_all(
                comfyui_cfg=comfyui_cfg,
                vllm_ports=[p for t, p in ports if t == "vllm"],
                comfyui_port=ports[-1][1] if ports and ports[-1][0] == "comfyui" else None,
            )

            # ── Stop GPU-independent services (gpu_role == "none") ──
            # These bypass the GPU state machine but must still be stopped on idle.
            for svc_name in from_services:
                m = self._models.get(svc_name)
                if not m or m.gpu_role != "none":
                    continue
                if m.is_ollama_cpp:
                    self._proc.stop_ollama_cpp(port=m.ollama_cpp.port)
                # type=ollama and ollama_daemon are served by an external daemon —
                # unregister by dropping them from active_services below.

            if not wait_gpu_free():
                self._proc.force_kill_all()
                if not wait_gpu_free(timeout=15):
                    self.state.set("profile_state", ProfileState.ERROR)
                    return {"status": "error", "message": "GPU not freed after force kill"}

            # Update state
            self.state.set_multi({
                "gpu_mode": GPUMode.IDLE,
                "active_services": json.dumps([]),
                "vllm_pid": "",
                "comfyui_pid": "",
                "profile_state": ProfileState.IDLE,
            })

            elapsed = round(time.time() - t0, 1)
            from_label = ",".join(from_services) if from_services else "idle"
            self.state.add_history(from_label, "idle", elapsed, "ok")

            return {
                "status": "switched",
                "model": "idle",
                "elapsed_sec": elapsed,
                "stopped": from_services,
            }
        except Exception as e:
            self.state.set("profile_state", ProfileState.ERROR)
            return {"status": "error", "message": str(e)}
        finally:
            self._lock.release()

    def _start_model(self, model: ModelConfig) -> dict:
        """Unified model start entry — dispatches by type.

        Single source of truth for how each framework launches a model,
        eliminating the per-branch if/else that was duplicated between
        _deploy_model() and _shared_add_service(). Returns one of:
          {"status": "healthy"/"started"/"ok", ...} on success,
          {"status": "error"/"timeout", ...} on failure.
        """
        if model.is_vllm:
            return self._proc.start_vllm(model.vllm, model.model_type)
        elif model.is_comfyui:
            return self._proc.start_comfyui(model.comfyui)
        elif model.is_ollama_daemon:
            return {"status": "ok", "message": "Ollama daemon external — verify with 'ollama serve'"}
        elif model.is_ollama:
            return self._start_ollama_model(model)
        elif model.is_ollama_cpp:
            return self._proc.start_ollama_cpp(model.ollama_cpp)
        else:
            return {"status": "error", "message": f"Unknown model type: {model.type}"}

    def _start_ollama_model(self, model: ModelConfig) -> dict:
        """Start a type=ollama model — via the Ollama daemon API.

        Ollama models are served by an external daemon (port 11434), not an
        independent InferFabric process. This ensures the daemon is running,
        then triggers `ollama run` to load the model so it's ready for inference.
        """
        daemon_healthy = self.check_ollama_health(11434)
        if daemon_healthy != "✅":
            log.info("Ollama daemon not running — auto-starting")
            try:
                env = os.environ.copy()
                # 如果当前模型配置了 num_gpu=0，启动时传入环境变量
                model_ref = model.ollama.model_ref
                num_gpu = model.ollama.num_gpu if hasattr(model.ollama, 'num_gpu') else -1
                if num_gpu >= 0:
                    env["OLLAMA_NUM_GPU"] = str(num_gpu)
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=env,
                )
            except FileNotFoundError:
                return {"status": "error", "message": "ollama not found in PATH. Install Ollama first."}
            # Wait for daemon to become healthy (up to 30s)
            for _ in range(60):
                time.sleep(0.5)
                if self.check_ollama_health(11434) == "✅":
                    break
            else:
                return {"status": "error", "message": "Ollama daemon failed to start within 30s"}

        model_ref = model.ollama.model_ref
        keep_alive = model.ollama.keep_alive or "5m"
        num_gpu = model.ollama.num_gpu if hasattr(model.ollama, 'num_gpu') else -1
        return self._proc.run_ollama(model_ref, keep_alive, num_gpu)

    def _deploy_model(self, model: ModelConfig, target_mode: str) -> dict:
        """Deploy a model from idle state."""
        t0 = time.time()

        # Ensure we use the latest YAML (prevents stale config after drift)
        model_name = model.name
        self._models = load_models(self.models_dir)
        model = self._models.get(model_name)
        if model is None:
            return {"status": "error", "message": f"Model {model_name} not found in YAML after reload"}

        results = {}
        services_to_start = [model.name]

        # If shared model, optionally also start ComfyUI
        # V1: shared vLLM models don't auto-start ComfyUI
        # User does: iff switch comfyui separately

        # Start the model — centralized type dispatch (eliminates per-branch forgetting)
        results[model.name] = self._start_model(model)

        # Validate
        failed = False
        for svc, res in results.items():
            if res.get("status") not in ("healthy", "started", "ok"):
                failed = True
                break

        if failed:
            self.state.set("profile_state", ProfileState.ERROR)
            # Clean up partial start with port-based cleanup
            ports = []
            if model.is_vllm:
                ports.append(model.vllm.port)
            elif model.is_comfyui:
                ports.append(model.comfyui.port)
            elif model.is_ollama_cpp:
                ports.append(model.ollama_cpp.port)
            self._proc.stop_all(
                comfyui_cfg=model.comfyui if model.is_comfyui else None,
                vllm_ports=ports if model.is_vllm else [],
                comfyui_port=ports[-1] if model.is_comfyui and ports else None,
            )
            self.state.set_multi({
                "gpu_mode": GPUMode.IDLE,
                "active_services": json.dumps([]),
                "vllm_pid": "",
                "comfyui_pid": "",
            })
            elapsed = round(time.time() - t0, 1)
            return {
                "status": "error",
                "message": f"Failed to start {model.name}: {results}",
                "results": results,
            }

        # Success
        elapsed = round(time.time() - t0, 1)
        # Record config hashes for drift detection on next switch
        config_hashes = {}
        for svc_name in services_to_start:
            svc_model = self._models.get(svc_name)
            if svc_model:
                config_hashes[f"config_hash:{svc_name}"] = svc_model.config_hash()
        self.state.set_multi({
            "gpu_mode": target_mode,
            "active_services": json.dumps(services_to_start),
            "profile_state": ProfileState.HEALTHY,
            **config_hashes,
        })

        return {
            "status": "switched",
            "model": model.name,
            "gpu_mode": target_mode,
            "elapsed_sec": elapsed,
            "results": results,
        }

    def _get_current_vram_pct(self) -> float:
        """Get current GPU VRAM usage as a percentage of total."""
        try:
            from .health import gpu_used_mb, gpu_total_mb
            return gpu_used_mb() / max(gpu_total_mb(), 1) * 100
        except Exception:
            return 0.0

    def _shared_add_service(self, model: ModelConfig) -> dict:
        """Add a shared-mode service. Caller must hold self._lock (see switch()).

        Only starts the new model. Existing shared services remain running.
        Checks typical VRAM headroom before starting.
        """
        if not self._lock.is_held:
            raise RuntimeError("_shared_add_service called without holding GPU lock")
        t0 = time.time()

        if model.name in self.active_services:
            return {
                "status": "already_active",
                "model": model.name,
                "gpu_mode": GPUMode.SHARED,
            }

        # ── VRAM headroom check ──
        if model.typical_vram_pct > 0:
            current_pct = self._get_current_vram_pct()
            if current_pct + model.typical_vram_pct > 95:
                return {
                    "status": "error",
                    "message": (
                        f"Insufficient GPU memory: current ~{current_pct:.0f}%, "
                        f"{model.name} needs ~{model.typical_vram_pct}%, "
                        f"total would be ~{current_pct + model.typical_vram_pct:.0f}% (limit 95%)."
                    ),
                }

        # ── Start only the new service — centralized type dispatch ──
        log.info("Shared add (incremental): %s", model.name)
        results = {model.name: self._start_model(model)}

        # Validate
        failed = [k for k, r in results.items() if r.get("status") not in ("healthy", "started", "ok")]
        if failed:
            self.state.set("profile_state", ProfileState.ERROR)
            return {"status": "error", "message": f"Failed to start: {failed}", "results": results}

        # Update state: add to active services + record config hash
        remaining = list(self.active_services)
        remaining.append(model.name)
        self.state.set_active_services(remaining)
        self.state.set(f"config_hash:{model.name}", model.config_hash())
        self.state.set("profile_state", ProfileState.HEALTHY)

        elapsed = round(time.time() - t0, 1)
        return {
            "status": "switched",
            "model": model.name,
            "gpu_mode": GPUMode.SHARED,
            "elapsed_sec": elapsed,
            "active_services": remaining,
            "results": results,
        }

    # ── Stop Single Service ──────────────────────────────────────

    def stop_service(self, name: str) -> dict:
        """Stop a single shared service. Other shared services remain.

        If this is the last shared service, auto-transition to idle.
        Verifies GPU memory is actually freed (catches orphaned processes).
        """
        if name not in self.active_services:
            return {"status": "error", "message": f"Service '{name}' is not running"}

        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}

        # gpu-none models use stop_independent (not blocked by exclusive GPU)
        if model.is_gpu_none:
            return self.stop_independent(name)

        if self.gpu_mode == GPUMode.EXCLUSIVE:
            return {"status": "error", "message": "Cannot stop individual service in exclusive mode. Use 'switch idle'."}

        # Stop the specific service (pass port for port-based cleanup)
        if model.is_vllm:
            self._proc.stop_vllm(port=model.vllm.port)
        elif model.is_comfyui:
            self._proc.stop_comfyui_with_config(model.comfyui, port=model.comfyui.port)
        elif model.is_ollama:
            # Ollama models are served by daemon — just unregister
            log.info("Unregistering Ollama model %s", name)
        elif model.is_ollama_daemon:
            log.info("Ollama daemon stop: use 'ollama serve' externally")
        elif model.is_ollama_cpp:
            self._proc.stop_ollama_cpp(port=model.ollama_cpp.port)

        # Verify GPU actually freed — catch orphaned processes (skip CPU-only models)
        if model.needs_gpu:
            if not wait_gpu_free(timeout=20):
                log.warning("GPU not freed after stop %s — force kill remaining processes", name)
                self._proc.force_kill_all()
                wait_gpu_free(timeout=15)

        # Update active services
        remaining = [s for s in self.active_services if s != name]
        self.state.set_active_services(remaining)

        # Auto-transition to idle if no services left
        if not remaining:
            self.state.set_multi({
                "gpu_mode": GPUMode.IDLE,
                "profile_state": ProfileState.IDLE,
            })
            return {
                "status": "stopped",
                "model": name,
                "gpu_mode": GPUMode.IDLE,
                "message": f"Stopped {name}. No services remaining → idle.",
            }

        return {
            "status": "stopped",
            "model": name,
            "gpu_mode": GPUMode.SHARED,
            "remaining": remaining,
        }

    # ── Independent Model Management (gpu_role: none) ──────────

    def stop_independent(self, name: str) -> dict:
        """Stop a GPU-independent model (gpu_role: none).

        Unlike stop_service(), this method:
        - Only accepts models with gpu_role == "none"
        - Does NOT change the GPU mode (idle/exclusive/shared tri-state)
        - Does NOT auto-transition to idle when last service is removed

        Use stop_service() for GPU-bound models (exclusive/shared).
        """
        if name not in self.active_services:
            return {"status": "error", "message": f"Independent model '{name}' is not running"}

        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}

        if not model.is_gpu_none:
            return {"status": "error", "message": f"Model '{name}' is not an independent model (gpu_role={model.gpu_role})"}

        # Stop the process — dispatch by type (same pattern as stop_service)
        if model.is_ollama_cpp:
            self._proc.stop_ollama_cpp(port=model.ollama_cpp.port)
        elif model.is_ollama:
            log.info("Unregistering Ollama independent model %s", name)
        elif model.is_ollama_daemon:
            log.info("Ollama daemon stop: use 'ollama serve' externally")
        elif model.is_vllm:
            self._proc.stop_vllm(port=model.vllm.port)
        elif model.is_comfyui:
            self._proc.stop_comfyui_with_config(model.comfyui, port=model.comfyui.port)

        # Remove from active_services (gpu_mode stays unchanged)
        remaining = [s for s in self.active_services if s != name]
        self.state.set_active_services(remaining)

        return {"status": "stopped", "model": name, "gpu_mode": self.gpu_mode}

    def list_independent(self) -> list[str]:
        """Return names of currently running GPU-independent models (gpu_role: none)."""
        return [name for name in self.active_services
                if (m := self._models.get(name)) and m.is_gpu_none]

    # ── Sleep / Wake (L2 only) ─────────────────────────────────

    def sleep_model(self, name: str) -> dict:
        """Put a running vLLM model to L2 sleep.

        Rules:
        - Only one model may sleep at a time.
        - Exclusive model sleeping → GPU transitions to idle (VRAM freed).
        - Shared model sleeping → GPU stays shared (other services unaffected).
        """
        if name not in self.active_services:
            return {"status": "error", "message": f"Model '{name}' is not running"}

        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}
        if not model.is_vllm:
            return {"status": "error", "message": f"Model '{name}' is not a vLLM model"}

        sleep_cfg = model.vllm.sleep_mode
        if not sleep_cfg or not sleep_cfg.enabled:
            return {"status": "error", "message": f"Sleep mode not enabled for '{name}'"}

        # Check if already sleeping
        if self.state.get_sleep_state(name):
            return {"status": "already_sleeping", "model": name}

        # Mutex: only one model sleeping at a time
        all_sleep = self.state.get_all_sleep_states()
        if all_sleep:
            existing = list(all_sleep.keys())[0]
            return {"status": "error",
                    "message": f"Model '{existing}' is already sleeping. Wake or stop it first."}

        log.info("Sleeping model '%s' (L2)", name)
        result = self._proc.sleep_vllm(model.vllm.port)

        if result["status"] == "ok":
            self.state.set_sleep_state(name, 2)

            # Exclusive sleeping → GPU → idle (VRAM freed)
            if model.is_exclusive:
                self.state.set_multi({
                    "gpu_mode": GPUMode.IDLE,
                    "profile_state": ProfileState.IDLE,
                })

            log.info("Model '%s' is now sleeping (L2), GPU=%s", name,
                     "idle" if model.is_exclusive else "shared")
        else:
            self.state.set_sleep_state(name, None)

        return {**result, "model": name}

    def wake_model(self, name: str) -> dict:
        """Wake a sleeping vLLM model.

        Rules:
        - Exclusive model: GPU must be idle → wake → GPU → exclusive.
        - Shared model: GPU must be idle or shared → wake → GPU → shared.
        """
        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}
        if not model.is_vllm:
            return {"status": "error", "message": f"Model '{name}' is not a vLLM model"}

        sleep_state = self.state.get_sleep_state(name)
        if not sleep_state:
            # Double-check actual server state
            if self._proc.is_sleeping(model.vllm.port):
                self.state.set_sleep_state(name, 2)
            else:
                return {"status": "already_awake", "model": name, "message": "Model is not sleeping"}

        # Validate GPU mode for wake — use shared transition table
        current_gpu = self.gpu_mode
        target_mode = model.gpu_role  # 'exclusive' or 'shared'
        if not validate_transition(current_gpu, target_mode):
            return {"status": "error",
                    "message": f"Cannot wake model '{name}': GPU is {current_gpu}, cannot transition to {target_mode}"}

        log.info("Waking model '%s' (L2)", name)
        result = self._proc.wake_vllm(model.vllm.port)

        if result["status"] == "ok" or result["status"] == "killed_for_restart":
            self.state.set_sleep_state(name, None)

            if model.is_exclusive:
                self.state.set_multi({
                    "gpu_mode": GPUMode.EXCLUSIVE,
                    "profile_state": ProfileState.HEALTHY,
                })
                current_services = self.active_services
                if name not in current_services:
                    self.state.add_active_service(name)
            else:
                # Shared model: process was killed, restart via switch
                return self.switch(name)

            log.info("Model '%s' is now awake", name)

        return {**result, "model": name}

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
            if m.is_vllm:
                info["port"] = m.vllm.port
                health = self.check_vllm_health(m.vllm.port)
            elif m.is_comfyui:
                info["port"] = m.comfyui.port
                health_url = m.comfyui.health_url or f"http://localhost:{m.comfyui.port}/system_stats"
                health = self.check_comfyui_health(health_url)
            elif m.is_ollama_daemon:
                info["port"] = m.ollama_daemon.port
                health = self.check_ollama_health(m.ollama_daemon.port)
            elif m.is_ollama:
                # Ollama models use daemon port; check if model loaded
                info["port"] = 11434
                info["model_ref"] = m.ollama.model_ref
                health = self.check_ollama_health(11434)
            elif m.is_ollama_cpp:
                info["port"] = m.ollama_cpp.port
                health = self.check_http_health(m.ollama_cpp.port, "/health")
            else:
                health = "?"
            # Append sleep state if applicable
            sleep_label = sleep_states.get(svc_name, "")
            if sleep_label:
                health = f"{health} (sleeping {sleep_label.upper()})"
            services_status[svc_name] = health
            services_info[svc_name] = info
            # Track dead services for auto-cleanup
            if health == "❌":
                dead_services.append(svc_name)

        # Auto-cleanup: remove dead services from state to avoid stale dashboard state
        if dead_services:
            log.warning("Auto-cleanup: removing dead services %s from state", dead_services)
            remaining = [s for s in active if s not in dead_services]
            self.state.set_active_services(remaining)
            active = remaining
            # If no services left, also reset GPU mode to idle
            if not active and self.gpu_mode != GPUMode.IDLE:
                log.info("No active services — resetting gpu_mode to idle")
                self.state.gpu_mode = GPUMode.IDLE

        return {
            "gpu_mode": self.gpu_mode,
            "active_services": active,
            "services_health": services_status,
            "services_info": services_info,
            "sleep_states": sleep_states,
            "gpu_used_mb": gpu_used_mb(),
            "gpu_total_mb": gpu_total_mb(),
            "vllm_pid": self._proc.vllm_pid,
            "comfyui_pid": self._proc.comfyui_pid,
        }

    # ── Force Reset ───────────────────────────────────────────────

    def force_reset(self) -> dict:
        """Nuclear reset: kill everything, verify GPU, clean state."""
        log.info("Force reset")

        # Collect ComfyUI config for proper stop
        comfyui_cfg = None
        for svc_name in self.active_services:
            m = self._models.get(svc_name)
            if m and m.is_comfyui:
                comfyui_cfg = m.comfyui
                break
        self._proc.stop_all(comfyui_cfg=comfyui_cfg)
        # Stop ollama_cpp (gpu_role=none) processes explicitly
        for svc_name in self.active_services:
            m = self._models.get(svc_name)
            if m and m.is_ollama_cpp:
                self._proc.stop_ollama_cpp(m.ollama_cpp.port)
        self._proc.force_kill_all()

        if not wait_gpu_free(timeout=20):
            log.warning(
                "GPU not free after force_reset (%d MB used). "
                "Skipping nvidia-smi --gpu-reset (destructive). "
                "Manual reset may be needed: sudo nvidia-smi --gpu-reset",
                gpu_used_mb()
            )

        self._lock.force_clear()

        self.state.set_multi({
            "gpu_mode": GPUMode.IDLE,
            "active_services": json.dumps([]),
            "profile_state": ProfileState.IDLE,
            "vllm_pid": "",
            "comfyui_pid": "",
            "sleep_state": "{}",
        })

        return {
            "status": "reset",
            "gpu_mode": GPUMode.IDLE,
            "gpu_free": gpu_used_mb() < GPU_FREE_THRESHOLD_MB,
        }

    # ── Internal Helpers ─────────────────────────────────────────

    def discover_local_models(self) -> dict:
        """Scan ~/models/, ~/ComfyUI/models/, and Ollama for unconfigured models.

        Returns models grouped by framework: vllm, ollama, ollama_cpp, comfyui.
        Each model has a 'framework' field for frontend grouping.
        """
        discovered = []
        configured = sorted(self._models.keys())
        configured_dirs = set()
        configured_ollama_refs = set()

        # Build set of known model directories and ollama refs from configured YAMLs
        for m in self._models.values():
            if m.is_vllm and m.vllm.model_dir:
                configured_dirs.add(m.vllm.model_dir)
            if m.is_ollama and m.ollama:
                configured_ollama_refs.add(m.ollama.model_ref)

        # ── vLLM models (~/models/ with config.json) ──
        models_base = Path.home() / "models"
        if models_base.exists():
            # Scan up to 3 levels deep (avoid rglob on large dirs)
            def _scan_dirs(parent):
                return [c for c in sorted(parent.glob("*")) if c.is_dir() and not c.name.startswith(".")]

            level1 = _scan_dirs(models_base)
            level2 = [s for d1 in level1 for s in _scan_dirs(d1)]
            level3 = [s for d2 in level2 for s in _scan_dirs(d2)]

            for scan_dir in level1 + level2 + level3:
                if scan_dir.name in configured_dirs:
                    continue
                config_json = scan_dir / "config.json"
                if config_json.exists():
                    try:
                        size_mb = sum(f.stat().st_size for f in scan_dir.rglob("*") if f.is_file()) // (1024*1024)
                    except Exception:
                        size_mb = 0
                    discovered.append({
                        "name": scan_dir.name, "path": str(scan_dir),
                        "type": "vllm", "framework": "vllm", "size_mb": size_mb,
                        "files": [f.name for f in sorted(scan_dir.iterdir()) if f.is_file()][:20],
                    })
                gguf_files = list(scan_dir.glob("*.gguf"))
                if gguf_files:
                    skip = False
                    for m in self._models.values():
                        if m.is_ollama_cpp and m.ollama_cpp:
                            mp = str(Path(m.ollama_cpp.model_path).expanduser().parent)
                            if str(scan_dir) == mp or str(scan_dir).startswith(mp + "/"):
                                skip = True
                                break
                    if not skip:
                        size_mb = sum(f.stat().st_size for f in gguf_files) // (1024*1024)
                        discovered.append({
                            "name": scan_dir.name, "path": str(scan_dir),
                            "type": "ollama_cpp", "framework": "ollama_cpp", "size_mb": size_mb,
                            "files": [f.name for f in gguf_files],
                        })
        # ── Ollama pulled models (ollama list) ──
        try:
            import subprocess
            result = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] not in ("NAME", ""):
                        model_ref = parts[0]  # e.g. "llama3.2:1b"
                        # Parse size from column 3+4 (e.g. "2.2" "GB")
                        size_mb = 0
                        for si in range(2, len(parts)):
                            if parts[si].upper() in ("GB", "G", "MB", "M", "KB", "K"):
                                try:
                                    val = float(parts[si-1])
                                    unit = parts[si].upper()
                                    if unit.startswith("G"): size_mb = int(val * 1024)
                                    elif unit.startswith("M"): size_mb = int(val)
                                    elif unit.startswith("K"): size_mb = int(val / 1024)
                                except Exception as e:
                                    log.debug("discover_local_models scan error: %s", e)
                                break
                        if model_ref not in configured_ollama_refs:
                            discovered.append({
                                "name": model_ref, "path": "ollama://" + model_ref,
                                "type": "ollama", "framework": "ollama", "size_mb": size_mb,
                                "files": [],
                            })
        except Exception as e:
            log.debug("discover_local_models scan error: %s", e)

        # ── ComfyUI models (~/ComfyUI/models/) ──
        comfyui_models = Path.home() / "ComfyUI" / "models"
        if comfyui_models.exists():
            for sub in ["checkpoints", "loras", "diffusion_models", "vae", "ipadapter"]:
                subdir = comfyui_models / sub
                if not subdir.exists():
                    continue
                for f in sorted(subdir.glob("*.safetensors")):
                    name = f.stem
                    size_mb = f.stat().st_size // (1024*1024)
                    discovered.append({
                        "name": name, "path": str(f),
                        "type": f"comfyui_{sub.rstrip('s')}", "framework": "comfyui", "size_mb": size_mb,
                        "files": [f.name],
                    })

        return {"discovered": discovered, "configured": configured}

    def auto_deploy(self, name: str, model_type: str) -> dict:
        """Auto-generate YAML and deploy a discovered model."""
        models_dir = self.models_dir
        yaml_path = models_dir / f"{name}.yaml"
        if yaml_path.exists():
            return {"status": "error", "message": f"YAML already exists: {yaml_path}"}

        # Find next available port
        used_ports = set()
        for m in self._models.values():
            if m.port:
                used_ports.add(m.port)

        if model_type == "vllm":
            port = max([p for p in used_ports if p >= 8000] + [7999]) + 1
            yaml_content = f"""name: {name}
description: "{name} (auto-deployed)"
type: vllm
gpu_role: shared
vllm:
  model_dir: {name}
  served_name: {name}
  port: {port}
  conda_env: qw36-27b-vllm
  gpu_memory_utilization: 0.83
  max_model_len: 131072
  max_num_seqs: 4
  kv_cache_dtype: auto
"""
        elif model_type.startswith("ollama_cpp"):
            port = max([p for p in used_ports if p >= 11435] + [11434]) + 1
            yaml_content = f"""name: {name}
description: "{name} (auto-deployed, ollama.cpp)"
type: ollama_cpp
gpu_role: none
ollama_cpp:
  model_path: ~/models/{name}/
  port: {port}
  threads: 16
  context_size: 16384
  gpu_layers: 0
"""
        else:
            return {"status": "error", "message": f"Unsupported type for auto-deploy: {model_type}"}

        yaml_path.write_text(yaml_content)
        log.info("Auto-generated YAML: %s", yaml_path)

        self._models = load_models(self.models_dir)
        result = self.switch(name)
        return result

    def _is_model_actively_running(self, model: ModelConfig) -> bool:
        """True if this model is currently deployed AND its service is actually live.

        Uses `_check_model_health` (live port probe) rather than trusting the
        `active_services` state DB, which can lag behind reality after crashes
        or manual `pkill`. Falls back to `active_services` membership for models
        without a probeable service port (e.g. ollama-type models served by a
        shared daemon).
        """
        try:
            if self._check_model_health(model):
                return True
        except Exception:
            log.debug("Health check failed for %s: %s", model.name, sys.exc_info()[1])
        # Fallback: state DB says it's active, or it has no probeable port
        return model.name in self.active_services

    def _check_model_health(self, model: ModelConfig) -> bool:
        """Check if a specific model's service is healthy."""
        if model.is_vllm:
            return self.check_vllm_health(model.vllm.port) == "✅"
        elif model.is_comfyui:
            health_url = model.comfyui.health_url or f"http://localhost:{model.comfyui.port}/system_stats"
            return self.check_comfyui_health(health_url) == "✅"
        elif getattr(model, "is_ollama_daemon", False):
            return self.check_ollama_health(model.ollama_daemon.port) == "✅"
        elif getattr(model, "is_ollama_cpp", False):
            return self.check_http_health(model.ollama_cpp.port, "/health") == "✅"
        return False


# ─── Backward Compatibility ──────────────────────────────────────

class ProfileManager(ModelManager):
    """Backward-compatible alias. All v3.x code using ProfileManager will work."""
    pass
