"""
inferfabric/gpu_state.py — GPU state machine (extracted from manager.py v4.0).

Responsible for: scanning actual GPU processes, deriving GPU mode, detecting
orphan PIDs, restoring dead PIDs, state reconciliation, VRAM queries, force reset.
"""

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .config import (
    GPU_FREE_THRESHOLD_MB,
    ModelConfig,
)
from .config_watcher import detect_drift as _detect_drift
from .health import (
    check_http_status,
    gpu_used_mb,
    gpu_total_mb,
    wait_gpu_free,
)
from .state import GPUMode, ProfileState, StateDB

log = logging.getLogger("inferfabric")


class GpuStateMachine:
    """GPU state machine — compares DB state against actual GPU processes.

    Owns the reconciliation logic, orphan detection, and force-reset.
    """

    def __init__(self, state, proc, health, lock, models):
        self.state = state
        self._proc = proc
        self._health = health
        self._lock = lock
        self._models = models

    # ── Port / Process Helpers ────────────────────────────────────

    @staticmethod
    def _port_pid(port: int) -> Optional[int]:
        """Return PID owning a TCP port via fuser, or None."""
        result = subprocess.run(
            ["fuser", "-v", str(port) + "/tcp"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            m = re.search(r'(\d+)', result.stdout)
            return int(m.group(1)) if m else None
        return None

    # ── Scan / Derive ─────────────────────────────────────────────

    def _scan_actual_services(self) -> list[str]:
        """Scan all known model ports and return names of actually-running services."""
        actual = []
        for name, m in self._models.items():
            status = self._health.check_model(m)
            if status in ("✅", "⏳"):
                actual.append(name)
            elif m.is_vllm and status == "❌" and self._port_pid(m.vllm.port) is not None:
                actual.append(name)
        return actual

    def _derive_gpu_mode(self, actual_services: list[str]) -> GPUMode:
        """Determine actual gpu_mode from running services (gpu_none services don't count)."""
        actual_gpu_mode = GPUMode.IDLE
        gpu_services = [s for s in actual_services if not (self._models.get(s) and self._models[s].is_gpu_none)]
        if gpu_services:
            for svc_name in actual_services:
                m = self._models.get(svc_name)
                if m and m.is_exclusive:
                    actual_gpu_mode = GPUMode.EXCLUSIVE
                    break
            if actual_gpu_mode == GPUMode.IDLE:
                actual_gpu_mode = GPUMode.SHARED
        return actual_gpu_mode

    # ── Orphan / Stale PID Detection ──────────────────────────────

    def _detect_orphan_pids(self, actual_services: list[str], actions: list[str]) -> None:
        """P0-4/P0-5: Clean up orphan or stale vllm_pid entries."""
        if self._proc.vllm_pid:
            try:
                os.killpg(self._proc.vllm_pid, 0)
            except (ProcessLookupError, PermissionError):
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

    def _restore_dead_pids(self, actual_services: list[str], actions: list[str]) -> None:
        """P0-5: If a vLLM is actually running but PID is not tracked, recover via fuser."""
        if not self._proc.vllm_pid:
            for svc_name in actual_services:
                m = self._models.get(svc_name)
                if m and m.is_vllm:
                    pid = self._port_pid(m.vllm.port)
                    if pid is not None:
                        self.state.set("vllm_pid", str(pid))
                        actions.append(f"Recovered vllm_pid={pid} for {svc_name} via fuser")
                        break

    # ── VRAM ──────────────────────────────────────────────────────

    def _get_current_vram_pct(self) -> float:
        """Get current GPU VRAM usage as a percentage of total."""
        try:
            return gpu_used_mb() / max(gpu_total_mb(), 1) * 100
        except Exception:
            return 0.0

    # ── Config Drift ──────────────────────────────────────────────

    def _check_model_config_changed(self, model: ModelConfig) -> bool:
        """Compare stored config hash against current YAML. Returns True if drifted.

        Delegates to config_watcher.detect_drift().
        """
        try:
            return _detect_drift(model, self.state)
        except Exception:
            log.debug("Config hash check failed for %s: %s", model.name, sys.exc_info()[1])
            return False  # conservative: don't restart on check failure

    # ── Health / Running State ────────────────────────────────────

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
        return model.name in self.state.get_active_services()

    def _check_model_health(self, model: ModelConfig) -> bool:
        """Check if a specific model's service is healthy."""
        return self._health.check_model(model) == "✅"

    # ── Reconcile ─────────────────────────────────────────────────

    def reconcile(self) -> dict:
        """Compare DB state against actual running processes. Fix inconsistencies."""
        db_gpu_mode = self.state.gpu_mode
        db_services = self.state.get_active_services()

        actual_services = self._scan_actual_services()
        actual_gpu_mode = self._derive_gpu_mode(actual_services)

        actions: list[str] = []

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

        self._detect_orphan_pids(actual_services, actions)
        self._restore_dead_pids(actual_services, actions)

        return {
            "db_gpu_mode": db_gpu_mode,
            "actual_gpu_mode": actual_gpu_mode,
            "db_services": db_services,
            "actual_services": actual_services,
            "actions": actions,
        }

    # ── Cleanup Dead Services ─────────────────────────────────────

    def cleanup_dead_services(self) -> list[str]:
        """Remove dead services from state and reset GPU mode to idle if all gone."""
        active = list(self.state.get_active_services())
        dead_services = []
        for svc_name in active:
            m = self._models.get(svc_name)
            if not m:
                dead_services.append(svc_name)
                continue
            health = self._health.check_model(m)
            if health == "❌":
                dead_services.append(svc_name)

        if dead_services:
            log.warning("Cleanup: removing dead services %s from state", dead_services)
            remaining = [s for s in active if s not in dead_services]
            self.state.set_active_services(remaining)
            if not remaining and self.state.gpu_mode != GPUMode.IDLE:
                log.info("No active services — resetting gpu_mode to idle")
                self.state.gpu_mode = GPUMode.IDLE
        return dead_services

    # ── Force Reset ───────────────────────────────────────────────

    def force_reset(self) -> dict:
        """Nuclear reset: kill everything, verify GPU, clean state."""
        log.info("Force reset")

        # Collect ComfyUI config for proper stop
        comfyui_cfg = None
        for svc_name in self.state.get_active_services():
            m = self._models.get(svc_name)
            if m and m.is_comfyui:
                comfyui_cfg = m.comfyui
                break
        self._proc.stop_all(comfyui_cfg=comfyui_cfg)
        # Stop ollama_cpp (gpu_role=none) processes explicitly
        for svc_name in self.state.get_active_services():
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