"""
edge_llm/manager.py — Profile orchestration layer.

Extracted from profile_manager.py (v3.0 → v3.1 refactoring).

Key improvements over v3.0:
  - ComfyUI failure now interrupts switch (was silently ignored)
  - stop_all uses ComfyUI config for proper cleanup
  - ComfyUI PID tracked alongside vLLM PID
"""

import os
import time
import logging
from pathlib import Path
from typing import Optional

from .config import (
    DEFAULT_PROFILES,
    DEFAULT_STATE_DB,
    GPU_FREE_TIMEOUT,
    GPU_FREE_THRESHOLD_MB,
    Profile,
    ComfyUIConfig,
    load_profiles,
)
from .state import StateDB, ProfileState
from .gpu_lock import GPULock
from .process_manager import ProcessManager
from .health import (
    gpu_used_mb,
    gpu_total_mb,
    check_http_status,
    wait_gpu_free,
)

log = logging.getLogger("edge_llm")


class ProfileManager:
    """Orchestrates GPU resource allocation via predefined profiles."""

    def __init__(
        self,
        profiles_path: str | Path = str(DEFAULT_PROFILES),
        state_db_path: str | Path = str(DEFAULT_STATE_DB),
    ):
        self.profiles_path = Path(profiles_path)
        self.state = StateDB(Path(state_db_path))
        self._lock = GPULock()
        self._proc = ProcessManager(self.state)
        self._profiles = load_profiles(self.profiles_path)

    @property
    def current_profile(self) -> str:
        return self.state.get("current_profile") or "idle"

    @property
    def profile_state(self) -> str:
        return self.state.get("profile_state") or ProfileState.IDLE

    def list_profiles(self) -> list[dict]:
        return [
            {
                "name": p.name,
                "description": p.description,
                "current": p.name == self.current_profile,
                "gpu_owner": p.gpu_owner,
                "has_vllm": p.vllm is not None,
                "has_comfyui": p.comfyui is not None,
                "switch_cost_sec": p.switch_cost_sec,
            }
            for p in self._profiles.values()
        ]

    # ── Health Check (tri-state) ─────────────────────────────────

    def check_vllm_health(self, port: int) -> str:
        """Check vLLM health: ✅ healthy, ⏳ loading, ❌ dead."""
        return check_http_status(f"http://localhost:{port}/health")

    def check_comfyui_health(self, url: str) -> str:
        """Check ComfyUI health."""
        return check_http_status(url)

    # ── Reconciliation ──────────────────────────────────────────

    def reconcile(self) -> dict:
        """Compare DB state against actual running processes. Fix inconsistencies.
        Uses tri-state health check to avoid killing processes during loading."""
        db_profile = self.current_profile
        db_state = self.profile_state

        # Scan all known vLLM ports for actual state
        actual_states = {}
        for name, p in self._profiles.items():
            if p.vllm:
                actual_states[name] = self.check_vllm_health(p.vllm.port)

        # Find the actually running profile (✅ or ⏳)
        actual_profile = None
        loading_profile = None
        for name, state in actual_states.items():
            if state == "✅":
                actual_profile = name
                break
            if state == "⏳":
                loading_profile = name

        # If nothing is ✅ but something is ⏳, don't kill it
        if actual_profile is None and loading_profile is not None:
            actual_profile = loading_profile

        # Check ComfyUI
        comfyui_ok = False
        comfyui_loading = False
        for p in self._profiles.values():
            if p.comfyui:
                h = self.check_comfyui_health(p.comfyui.health_url or f"http://localhost:{p.comfyui.port}/system_stats")
                if h == "✅":
                    comfyui_ok = True
                elif h == "⏳":
                    comfyui_loading = True

        actions: list[str] = []

        # Fix state inconsistencies
        if actual_profile and actual_profile != db_profile:
            if actual_states.get(actual_profile) == "⏳":
                actions.append(f"DB says '{db_profile}', but {actual_profile} is loading (⏳) — updating DB")
                self.state.set_multi({
                    "current_profile": actual_profile,
                    "profile_state": ProfileState.SWITCHING,
                })
            else:
                actions.append(f"DB says '{db_profile}', but {actual_profile} is running (✅) — updating DB")
                self.state.set_multi({
                    "current_profile": actual_profile,
                    "profile_state": ProfileState.HEALTHY,
                })
        elif actual_profile is None and db_profile != "idle":
            # Nothing running — check if there's a tracked PID
            if self._proc.vllm_pid:
                try:
                    os.killpg(self._proc.vllm_pid, 0)
                    actions.append(f"Tracked PGID {self._proc.vllm_pid} alive but HTTP dead — killing orphan")
                    self._proc.stop_vllm()
                except (ProcessLookupError, PermissionError):
                    actions.append(f"DB says '{db_profile}' but nothing running and tracked PID is dead → forcing idle")
            else:
                actions.append(f"DB says '{db_profile}' but nothing running → forcing idle")
            self.state.set_multi({
                "current_profile": "idle",
                "profile_state": ProfileState.IDLE,
                "vllm_pid": "",
                "comfyui_pid": "",
            })
        elif actual_profile is None and db_profile == "idle" and db_state != ProfileState.IDLE:
            actions.append(f"Profile is idle but state is '{db_state}' → fixing to idle")
            self.state.set("profile_state", ProfileState.IDLE)

        # Fix profile_state inconsistencies
        if actual_profile and actual_states.get(actual_profile) == "✅" and db_state != ProfileState.HEALTHY:
            actions.append(f"State was '{db_state}' but {actual_profile} is healthy (✅) — updating to healthy")
            self.state.set("profile_state", ProfileState.HEALTHY)
        elif db_state == ProfileState.SWITCHING and actual_profile:
            actual_health = actual_states.get(actual_profile, "❌")
            if actual_health == "✅":
                actions.append(f"State was 'switching' but {actual_profile} is healthy — updating to healthy")
                self.state.set("profile_state", ProfileState.HEALTHY)

        return {
            "db_profile": db_profile,
            "db_state": db_state,
            "actual_profile": actual_profile or "none",
            "actual_states": actual_states,
            "actions": actions,
            "comfyui_alive": comfyui_ok,
            "comfyui_loading": comfyui_loading,
        }

    # ── Switch ────────────────────────────────────────────────────

    def switch(self, target: str) -> dict:
        """Switch to target profile. Validates ALL services healthy before success.
        v3.1 fix: ComfyUI failure now properly interrupts the switch."""
        if target == self.current_profile and self.profile_state == ProfileState.HEALTHY:
            return {"status": "already_active", "profile": target}

        profile = self._profiles.get(target)
        if not profile:
            return {"status": "error", "message": f"Unknown profile: {target}"}

        if not self._lock.acquire():
            return {"status": "error", "message": "GPU switch in progress (lock held)"}

        t0 = time.time()
        from_profile = self.current_profile
        log.info("Switching %s → %s", from_profile, target)

        # Set switching state
        self.state.set_multi({
            "current_profile": target,
            "profile_state": ProfileState.SWITCHING,
        })

        try:
            # Step 1: Stop current services
            # Get current profile's ComfyUI config for proper cleanup
            current_profile = self._profiles.get(from_profile)
            comfyui_cfg = current_profile.comfyui if current_profile else None
            stop_result = self._proc.stop_all(comfyui_cfg=comfyui_cfg)
            log.info("Stop result: %s", stop_result)

            # Step 2: Wait for GPU to be free
            if not wait_gpu_free():
                log.warning("GPU not free after %ds, force killing...", GPU_FREE_TIMEOUT)
                self._proc.force_kill_all()
                if not wait_gpu_free(timeout=15):
                    self.state.set("profile_state", ProfileState.ERROR)
                    return {
                        "status": "error",
                        "message": "GPU not freed even after force kill — check nvidia-smi",
                    }

            # Step 3: Start target services
            results = {}
            if profile.comfyui:
                log.info("Starting ComfyUI")
                results["comfyui"] = self._proc.start_comfyui(profile.comfyui)
            if profile.vllm:
                log.info("Starting vLLM: %s on :%d", profile.vllm.served_name, profile.vllm.port)
                results["vllm"] = self._proc.start_vllm(profile.vllm)

            # Step 4: Validate ALL services
            # v3.1 fix: check ComfyUI failure too (was silently ignored in v3.0)
            if profile.comfyui and results.get("comfyui", {}).get("status") not in ("healthy", "started"):
                log.error("ComfyUI failed to start for %s", target)
                self.state.set("profile_state", ProfileState.ERROR)
                # Clean up partial start
                if profile.vllm:
                    self._proc.stop_vllm()
                self.state.add_history(from_profile, target, time.time() - t0, "error")
                return {
                    "status": "error",
                    "message": f"ComfyUI failed: {results['comfyui'].get('message', 'unknown')}",
                    "profile": target,
                    "results": results,
                }

            if profile.vllm and results.get("vllm", {}).get("status") != "healthy":
                log.error("vLLM failed to become healthy for %s", target)
                self.state.set("profile_state", ProfileState.ERROR)
                # Clean up partial start
                if profile.comfyui:
                    self._proc.stop_comfyui()
                self.state.add_history(from_profile, target, time.time() - t0, "error")
                return {
                    "status": "error",
                    "message": f"vLLM failed: {results['vllm'].get('message', 'timeout')}",
                    "profile": target,
                    "results": results,
                }

            # Step 5: Success — update state
            elapsed = round(time.time() - t0, 1)
            self.state.set_multi({
                "current_profile": target,
                "profile_state": ProfileState.HEALTHY,
            })
            self.state.add_history(from_profile, target, elapsed, "ok")
            log.info("Switch complete in %.1fs", elapsed)
            return {
                "status": "switched",
                "profile": target,
                "elapsed_sec": elapsed,
                "results": results,
            }
        except Exception as e:
            log.exception("Switch failed")
            self.state.set("profile_state", ProfileState.ERROR)
            self.state.add_history(from_profile, target, time.time() - t0, "error")
            return {"status": "error", "message": str(e)}
        finally:
            self._lock.release()

    def status(self) -> dict:
        profile = self._profiles.get(self.current_profile)
        vllm_status = "❌"
        comfyui_status = "❌"
        if profile:
            if profile.vllm:
                vllm_status = self.check_vllm_health(profile.vllm.port)
            if profile.comfyui:
                health_url = profile.comfyui.health_url or f"http://localhost:{profile.comfyui.port}/system_stats"
                comfyui_status = self.check_comfyui_health(health_url)
        return {
            "profile": self.current_profile,
            "state": self.profile_state,
            "description": profile.description if profile else "unknown",
            "vllm": vllm_status,
            "comfyui": comfyui_status,
            "gpu_used_mb": gpu_used_mb(),
            "gpu_total_mb": gpu_total_mb(),
            "vllm_pid": self._proc.vllm_pid,
            "comfyui_pid": self._proc.comfyui_pid,
        }

    # ── Force Reset ───────────────────────────────────────────────

    def force_reset(self, target: str = "idle") -> dict:
        """Nuclear reset: kill everything, verify GPU, clean state."""
        log.info("Force reset → %s", target)

        # 1. Stop everything through proper channels first
        self._proc.stop_all()

        # 2. SIGKILL all remaining
        self._proc.force_kill_all()

        # 3. Wait for GPU
        if not wait_gpu_free(timeout=20):
            try:
                subprocess.run(["nvidia-smi", "--gpu-reset"], timeout=10, check=False)
                time.sleep(5)
            except Exception:
                pass
            if not wait_gpu_free(timeout=15):
                log.warning("GPU still busy after force reset — orphan CUDA context likely")

        # 4. Clean lock
        self._lock.force_clear()

        # 5. Write state
        self.state.set_multi({
            "current_profile": target,
            "profile_state": ProfileState.IDLE if target == "idle" else ProfileState.ERROR,
            "vllm_pid": "",
            "comfyui_pid": "",
        })
        return {
            "status": "reset",
            "profile": target,
            "gpu_free": gpu_used_mb() < GPU_FREE_THRESHOLD_MB,
        }
