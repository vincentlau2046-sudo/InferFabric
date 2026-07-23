"""
inferfabric/model_lifecycle.py — Model lifecycle management (extracted from manager.py v4.0).

Responsible for: starting/stopping models (vLLM/ComfyUI/Ollama/OllamaCpp),
deployment flow, shared service add, sleep/wake, independent model management.
"""

import json
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from .config import (
    GPU_FREE_TIMEOUT,
    GPU_FREE_THRESHOLD_MB,
    ModelConfig,
    load_models,
)
from .gpu_state import GpuStateMachine
from .health import (
    check_http_status,
    gpu_used_mb,
    gpu_total_mb,
)
from .state import GPUMode, ProfileState, StateDB, validate_transition

log = logging.getLogger("inferfabric")


class ModelLifecycle:
    """Model lifecycle management — deploy, stop, sleep, wake, independent services.

    Owns the concrete operations for each model type. Does NOT hold the orchestration
    logic (that lives in ModelManager switch()). Does NOT hold reconciliation logic
    (that lives in GpuStateMachine).
    """

    def __init__(self, state, proc, health, lock, gpu_state, models, models_dir=None):
        self.state = state
        self._proc = proc
        self._health = health
        self._lock = lock
        self._gpu_state = gpu_state
        self._models = models
        self.models_dir = models_dir

    # ── Start Model (dispatch) ────────────────────────────────────

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
        daemon_healthy = check_http_status("http://localhost:11434/api/tags")
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
                if check_http_status("http://localhost:11434/api/tags") == "✅":
                    break
            else:
                return {"status": "error", "message": "Ollama daemon failed to start within 30s"}

        model_ref = model.ollama.model_ref
        keep_alive = model.ollama.keep_alive or "5m"
        num_gpu = model.ollama.num_gpu if hasattr(model.ollama, 'num_gpu') else -1
        return self._proc.run_ollama(model_ref, keep_alive, num_gpu)

    # ── Deploy ────────────────────────────────────────────────────

    def _deploy_model(self, model: ModelConfig, target_mode: str) -> dict:
        """Deploy a model from idle state."""
        t0 = time.time()

        # Ensure we use the latest YAML (prevents stale config after drift)
        model_name = model.name
        self._models = load_models(self.models_dir)
        model = self._models.get(model_name)
        if model is None:
            return {"status": "error", "message": f"Model {model_name} not found in YAML after reload"}

        # PR-1: VRAM budget guard — reject if peak would exceed 95% of GPU
        if model.gpu_role != "none":
            if model.peak_vram_mb > 0:
                current_vram = gpu_used_mb()
                gpu_total = gpu_total_mb()
                budget = int(gpu_total * 0.95)
                if current_vram + model.peak_vram_mb > budget:
                    msg = (f"VRAM budget exceeded: current {current_vram}MB + "
                           f"peak {model.peak_vram_mb}MB = {current_vram + model.peak_vram_mb}MB "
                           f"> budget {budget}MB (95% of {gpu_total}MB)")
                    log.warning(msg)
                    return {"status": "error", "message": msg}
            elif model.typical_vram_pct > 0:
                # Fallback to legacy pct-based check
                current_pct = self._gpu_state._get_current_vram_pct()
                if current_pct + model.typical_vram_pct > 95:
                    msg = (f"VRAM budget exceeded: current ~{current_pct:.0f}% + "
                           f"~{model.typical_vram_pct}% > 95%")
                    log.warning(msg)
                    return {"status": "error", "message": msg}

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

    # ── Shared Add Service ────────────────────────────────────────

    def _shared_add_service(self, model: ModelConfig) -> dict:
        """Add a shared-mode service. Caller must hold self._lock (see switch()).

        Only starts the new model. Existing shared services remain running.
        Checks typical VRAM headroom before starting.
        """
        if not self._lock.is_held:
            raise RuntimeError("_shared_add_service called without holding GPU lock")
        t0 = time.time()

        if model.name in self.state.get_active_services():
            return {
                "status": "already_active",
                "model": model.name,
                "gpu_mode": GPUMode.SHARED,
            }

        # ── VRAM headroom check (unified with _deploy_model peak_vram_mb guard) ──
        if model.peak_vram_mb > 0:
            current_vram = gpu_used_mb()
            gpu_total = gpu_total_mb()
            budget = int(gpu_total * 0.95)
            if current_vram + model.peak_vram_mb > budget:
                return {
                    "status": "error",
                    "message": (
                        f"VRAM budget exceeded: current {current_vram}MB + "
                        f"peak {model.peak_vram_mb}MB = {current_vram + model.peak_vram_mb}MB "
                        f"> budget {budget}MB (95% of {gpu_total}MB)"
                    ),
                }
        elif model.typical_vram_pct > 0:
            # Fallback to legacy pct-based check for models without peak_vram_mb
            current_pct = self._gpu_state._get_current_vram_pct()
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
        remaining = list(self.state.get_active_services())
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

    # ── Common Process-Stop Dispatch ──────────────────────────────

    def _stop_model_process(self, model: ModelConfig, name: str) -> None:
        """Dispatch process stop by model type. Shared helper for stop_service and stop_independent."""
        if model.is_vllm:
            self._proc.stop_vllm(port=model.vllm.port)
        elif model.is_comfyui:
            self._proc.stop_comfyui_with_config(model.comfyui, port=model.comfyui.port)
        elif model.is_ollama:
            log.info("Unregistering Ollama model %s", name)
        elif model.is_ollama_daemon:
            log.info("Ollama daemon stop: use 'ollama serve' externally")
        elif model.is_ollama_cpp:
            self._proc.stop_ollama_cpp(port=model.ollama_cpp.port)

    # ── Switch (internal helpers) ─────────────────────────────────

    def _switch_exclusive(self, model: ModelConfig) -> dict:
        """Switch from one exclusive model to another (same-port swap).

        Stops the currently active exclusive model, clears active_services,
        then deploys the new one. If deployment fails, attempts rollback to the
        previous model to avoid service interruption.
        """
        # Record old model info for rollback
        current = list(self.state.get_active_services())
        old_models = {svc: self._models.get(svc) for svc in current}

        # Stop current active exclusive model
        for svc_name in current:
            log.info("Stopping current exclusive service: %s", svc_name)
            svc_model = old_models.get(svc_name)
            if svc_model:
                self._stop_model_process(svc_model, svc_name)
            else:
                log.warning("No model config for exclusive service %s, skipping stop", svc_name)

        # Clear active state before deploying new model
        self.state.set("active_services", json.dumps([]))

        # Deploy the new model
        result = self._deploy_model(model, GPUMode.EXCLUSIVE)

        # If deployment failed, attempt rollback to old model
        if result.get("status") not in ("switched", "already_active"):
            log.warning("Deploy of %s failed, attempting rollback to %s", model.name, list(old_models.keys()))
            rollback_ok = False
            rollback_svc = None
            for svc_name, svc_model in old_models.items():
                if svc_model and svc_model.gpu_role != "none":
                    # Reload YAML to get fresh config for rollback
                    self._models = load_models(self.models_dir)
                    fresh_model = self._models.get(svc_name)
                    if fresh_model:
                        rb = self._deploy_model(fresh_model, fresh_model.gpu_role)
                        if rb.get("status") == "switched":
                            rollback_ok = True
                            rollback_svc = svc_name
                            log.info("Rollback to %s succeeded", svc_name)
                            break
            if rollback_ok:
                # Clean result — caller gets a coherent rollback-success dict
                result = {"status": "switched", "model": rollback_svc, "rollback": "succeeded"}
            else:
                self.state.set_multi({
                    "gpu_mode": GPUMode.IDLE,
                    "profile_state": ProfileState.ERROR,
                })
                log.error("Rollback failed — system in ERROR state, no model available")
                result["rollback"] = "failed"

        return result

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
        remaining = list(self.state.get_active_services())
        if model.name not in remaining:
            remaining.append(model.name)
        self.state.set_active_services(remaining)
        self.state.set(f"config_hash:{model.name}", model.config_hash())
        self.state.set("profile_state", ProfileState.HEALTHY)

        elapsed = round(time.time() - t0, 1)
        return {
            "status": "switched",
            "model": model.name,
            "gpu_mode": self.state.gpu_mode,  # unchanged
            "elapsed_sec": elapsed,
            "results": {model.name: result},
        }

    def _switch_to_idle(self) -> dict:
        """Stop all services (including GPU-independent models) and transition to idle."""
        current_mode = self.state.gpu_mode
        if current_mode == GPUMode.IDLE and not self.state.get_active_services():
            return {"status": "already_active", "model": "idle"}

        # P0-2: reconcile first to sync state before stopping
        log.info("Reconciling state before idle switch")
        self._gpu_state.reconcile()

        if not self._lock.acquire():
            return {"status": "error", "message": "GPU switch in progress (lock held)"}

        t0 = time.time()
        from_services = list(self.state.get_active_services())
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
                comfyui_port=comfyui_cfg.port if comfyui_cfg else None,
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

            gpu_idle = self._proc._wait_gpu_idle(timeout=30)
            if gpu_idle.get("status") not in ("ok", "force"):
                self._proc.force_kill_all()
                gpu_idle2 = self._proc._wait_gpu_idle(timeout=15)
                if gpu_idle2.get("status") not in ("ok", "force"):
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

    # ── Stop Single Service ───────────────────────────────────────

    def stop_service(self, name: str) -> dict:
        """Stop a single shared service. Other shared services remain.

        If this is the last shared service, auto-transition to idle.
        Verifies GPU memory is actually freed (catches orphaned processes).
        """
        if name not in self.state.get_active_services():
            return {"status": "error", "message": f"Service '{name}' is not running"}

        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}

        # gpu-none models use stop_independent (not blocked by exclusive GPU)
        if model.is_gpu_none:
            return self.stop_independent(name)

        if self.state.gpu_mode == GPUMode.EXCLUSIVE:
            return {"status": "error", "message": "Cannot stop individual service in exclusive mode. Use 'switch idle'."}

        # Stop the specific service (pass port for port-based cleanup)
        self._stop_model_process(model, name)

        # Verify GPU actually freed — catch orphaned processes (skip CPU-only models)
        if model.needs_gpu:
            gpu_idle = self._proc._wait_gpu_idle(timeout=20)
            if gpu_idle.get("status") not in ("ok", "force"):
                log.warning("GPU not freed after stop %s — force kill remaining processes", name)
                self._proc.force_kill_all()
                self._proc._wait_gpu_idle(timeout=15)

        # Update active services
        remaining = [s for s in self.state.get_active_services() if s != name]
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

    # ── Independent Model Management (gpu_role: none) ──────────────

    def stop_independent(self, name: str) -> dict:
        """Stop a GPU-independent model (gpu_role: none).

        Unlike stop_service(), this method:
        - Only accepts models with gpu_role == "none"
        - Does NOT change the GPU mode (idle/exclusive/shared tri-state)
        - Does NOT auto-transition to idle when last service is removed

        Use stop_service() for GPU-bound models (exclusive/shared).
        """
        if name not in self.state.get_active_services():
            return {"status": "error", "message": f"Independent model '{name}' is not running"}

        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}

        if not model.is_gpu_none:
            return {"status": "error", "message": f"Model '{name}' is not an independent model (gpu_role={model.gpu_role})"}

        # Stop the process — dispatch by type (shared helper)
        self._stop_model_process(model, name)

        # Remove from active_services (gpu_mode stays unchanged)
        remaining = [s for s in self.state.get_active_services() if s != name]
        self.state.set_active_services(remaining)

        return {"status": "stopped", "model": name, "gpu_mode": self.state.gpu_mode}

    def list_independent(self) -> list[str]:
        """Return names of currently running GPU-independent models (gpu_role: none)."""
        return [name for name in self.state.get_active_services()
                if (m := self._models.get(name)) and m.is_gpu_none]

    # ── Sleep / Wake (L2 only) ────────────────────────────────────

    def sleep_model(self, name: str) -> dict:
        """Put a running vLLM model to L2 sleep.

        Rules:
        - Only one model may sleep at a time.
        - Exclusive model sleeping → GPU transitions to idle (VRAM freed).
        - Shared model sleeping → GPU stays shared (other services unaffected).
        """
        if name not in self.state.get_active_services():
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
        current_gpu = self.state.gpu_mode
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
                current_services = self.state.get_active_services()
                if name not in current_services:
                    self.state.add_active_service(name)
            else:
                # Shared model: process was killed, restart via deploy
                if not self._lock.acquire():
                    return {"status": "error", "message": "GPU switch in progress (lock held)"}
                try:
                    if self.state.gpu_mode == GPUMode.IDLE:
                        deploy_result = self._deploy_model(model, GPUMode.SHARED)
                    else:
                        deploy_result = self._shared_add_service(model)
                except Exception as e:
                    deploy_result = {"status": "error", "message": str(e)}
                finally:
                    self._lock.release()
                return deploy_result

            log.info("Model '%s' is now awake", name)

        return {**result, "model": name}