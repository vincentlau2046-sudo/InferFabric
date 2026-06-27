"""
edge_llm/profile_manager.py — Backward-compatible re-export wrapper.

All functionality has been split into focused modules (v3.1 refactoring):
  - config.py:      Constants, Profile/VLLMConfig/ComfyUIConfig, YAML loading
  - state.py:       StateDB, ProfileState
  - gpu_lock.py:    GPULock
  - health.py:      HTTP/GPU health checks, wait_http, gpu_used_mb
  - process_manager.py: ProcessManager (vLLM + ComfyUI native)
  - manager.py:     ProfileManager (orchestration)

This file re-exports everything for backward compatibility.
Existing imports like `from edge_llm.profile_manager import ProfileManager` will continue to work.
"""

# Re-export all public symbols from the new modules
from .config import (
    BASE_DIR,
    DEFAULT_PROFILES,
    DEFAULT_STATE_DB,
    DEFAULT_LOG_DIR,
    GPU_LOCK_PATH,
    MODEL_BASE,
    CONDA_ENVS,
    COMFYUI_DIR,
    STOP_SIGTERM_TIMEOUT,
    VLLM_STARTUP_CHECK_INTERVAL,
    VLLM_STARTUP_CHECK_ROUNDS,
    HEALTH_CHECK_TIMEOUT,
    GPU_FREE_TIMEOUT,
    GPU_FREE_THRESHOLD_MB,
    VLLMConfig,
    ComfyUIConfig,
    Profile,
    load_profiles,
)
from .state import (
    ProfileState,
    StateDB,
)
from .gpu_lock import (
    GPULock,
)
from .health import (
    gpu_used_mb,
    gpu_total_mb,
    wait_gpu_free,
    check_http_status,
    wait_http,
    kill_port,
)
from .process_manager import (
    ProcessManager,
)
from .manager import (
    ProfileManager,
)

# Legacy alias: GPU_LOCK was the old name for the lock file path constant
GPU_LOCK = GPU_LOCK_PATH

__all__ = [
    # Config
    "BASE_DIR", "DEFAULT_PROFILES", "DEFAULT_STATE_DB", "DEFAULT_LOG_DIR",
    "GPU_LOCK_PATH", "GPU_LOCK", "MODEL_BASE", "CONDA_ENVS", "COMFYUI_DIR",
    "STOP_SIGTERM_TIMEOUT", "VLLM_STARTUP_CHECK_INTERVAL",
    "VLLM_STARTUP_CHECK_ROUNDS", "HEALTH_CHECK_TIMEOUT",
    "GPU_FREE_TIMEOUT", "GPU_FREE_THRESHOLD_MB",
    "VLLMConfig", "ComfyUIConfig", "Profile", "load_profiles",
    # State
    "ProfileState", "StateDB",
    # GPU Lock
    "GPULock",
    # Health
    "gpu_used_mb", "gpu_total_mb", "wait_gpu_free",
    "check_http_status", "wait_http", "kill_port",
    # Process Manager
    "ProcessManager",
    # Profile Manager
    "ProfileManager",
]
