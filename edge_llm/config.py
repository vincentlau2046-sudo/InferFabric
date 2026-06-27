"""
edge_llm/config.py — Configuration, constants, and profile definitions.

Extracted from profile_manager.py (v3.0 → v3.1 refactoring).
"""

from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import yaml


# ─── Path Constants ──────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
DEFAULT_PROFILES = BASE_DIR / "profiles.yaml"
DEFAULT_STATE_DB = Path.home() / ".edge_llm" / "state.db"
DEFAULT_LOG_DIR = Path.home() / ".edge_llm" / "logs"
GPU_LOCK_PATH = Path("/tmp/edge_llm_gpu.lock")
MODEL_BASE = Path.home() / "models"
CONDA_ENVS = Path.home() / "miniconda3" / "envs"
COMFYUI_DIR = Path.home() / "ComfyUI"


# ─── Process Management Constants ────────────────────────────────

STOP_SIGTERM_TIMEOUT = 10       # seconds to wait after SIGTERM before SIGKILL
VLLM_STARTUP_CHECK_INTERVAL = 0.5  # seconds between startup checks
VLLM_STARTUP_CHECK_ROUNDS = 20  # 10 seconds total for immediate-failure detection
HEALTH_CHECK_TIMEOUT = 300      # 5 minutes for vLLM to become healthy
GPU_FREE_TIMEOUT = 30           # seconds to wait for GPU memory release
GPU_FREE_THRESHOLD_MB = 2048    # MB below which GPU is considered "free"


# ─── Data Classes ────────────────────────────────────────────────

@dataclass
class VLLMConfig:
    model_dir: str
    served_name: str
    port: int
    conda_env: str
    max_model_len: int
    gpu_memory_utilization: float
    max_num_seqs: int
    kv_cache_dtype: str
    speculative_config: Optional[str] = None
    extra_flags: str = ""

    def build_cmd(self) -> list[str]:
        """Build vLLM command. JSON args stay as single elements."""
        model_path = MODEL_BASE / self.model_dir
        flags = [
            "vllm", "serve", str(model_path),
            "--served-model-name", self.served_name,
            "--max-model-len", str(self.max_model_len),
            "--gpu-memory-utilization", str(self.gpu_memory_utilization),
            "--max-num-seqs", str(self.max_num_seqs),
            "--kv-cache-dtype", self.kv_cache_dtype,
            "--port", str(self.port),
            "--host", "0.0.0.0",
        ]
        if self.speculative_config:
            flags.extend(["--speculative-config", self.speculative_config])
        if self.extra_flags:
            import shlex
            flags.extend(shlex.split(self.extra_flags))
        return flags


@dataclass
class ComfyUIConfig:
    """ComfyUI configuration. Supports both native Python and legacy script modes."""
    conda_env: str = "comfyui"
    port: int = 8188
    working_dir: str = ""
    health_url: str = ""
    extra_flags: str = "--cache-none --enable-manager"
    # Legacy fallback (deprecated — native mode preferred)
    startup_script: str = ""
    stop_script: str = ""

    @property
    def use_native(self) -> bool:
        """True if we should use native Python process management."""
        return bool(self.conda_env and not self.startup_script)

    @property
    def resolved_working_dir(self) -> Path:
        wd = self.working_dir or str(COMFYUI_DIR)
        return Path(wd).expanduser().resolve()


@dataclass
class Profile:
    name: str
    description: str
    gpu_owner: str
    vllm: Optional[VLLMConfig] = None
    comfyui: Optional[ComfyUIConfig] = None
    switch_cost_sec: int = 0


# ─── Profile Loading ─────────────────────────────────────────────

def load_profiles(profiles_path: Path) -> dict[str, Profile]:
    """Load profiles from YAML configuration file."""
    raw = yaml.safe_load(profiles_path.read_text())["profiles"]
    result = {}
    for name, cfg in raw.items():
        vllm_cfg = None
        if cfg.get("vllm"):
            vllm_cfg = VLLMConfig(**cfg["vllm"])
        comfy_cfg = None
        if cfg.get("comfyui"):
            comfy_cfg = ComfyUIConfig(**cfg["comfyui"])
        result[name] = Profile(
            name=name,
            description=cfg.get("description", name),
            gpu_owner=cfg.get("gpu_owner", "none"),
            vllm=vllm_cfg,
            comfyui=comfy_cfg,
            switch_cost_sec=cfg.get("switch_cost_sec", 0),
        )
    return result
