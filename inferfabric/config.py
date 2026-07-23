"""
inferfabric/config.py — Configuration, constants, and model definitions.

v4.0: Profile concept eliminated. Models are self-describing plugins in models.d/.
Each YAML file declares its own mode (exclusive/shared) and resource requirements.
"""

from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import json
import yaml
import hashlib
import dataclasses
import logging
import threading

log = logging.getLogger("inferfabric")

# ─── Path Constants ──────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
MODELS_DIR = BASE_DIR / "models.d"
DEFAULT_PROFILES = Path.home() / ".local" / "share" / "inferfabric" / "profiles.yaml"
DEFAULT_STATE_DB = Path.home() / ".inferfabric" / "state.db"
DEFAULT_LOG_DIR = Path.home() / ".inferfabric" / "logs"
GPU_LOCK_PATH = Path("/tmp/inferfabric_gpu.lock")
MODEL_BASE = Path.home() / "models"
CONDA_ENVS = Path.home() / "miniconda3" / "envs"
COMFYUI_DIR = Path.home() / "ComfyUI"

# ─── Process Management Constants ────────────────────────────────

STOP_SIGTERM_TIMEOUT = 10       # seconds to wait after SIGTERM before SIGKILL
VLLM_STARTUP_CHECK_INTERVAL = 0.5  # seconds between startup checks
VLLM_STARTUP_CHECK_ROUNDS = 20  # 10 seconds total for immediate-failure detection
HEALTH_CHECK_TIMEOUT = 300      # 5 minutes default for vLLM to become healthy (overridden by model startup_timeout)
GPU_FREE_TIMEOUT = 30           # seconds to wait for GPU memory release
GPU_FREE_THRESHOLD_MB = 2048    # MB below which GPU is considered "free"


# ─── Data Classes ────────────────────────────────────────────────

@dataclass
class SleepModeConfig:
    """vLLM sleep mode (L2 only: discard weights, wake needs reload 3-6s).

    Requires VLLM_SERVER_DEV_MODE=1 + --enable-sleep-mode at startup.
    """
    enabled: bool = False


@dataclass
class VLLMConfig:
    model_dir: str
    served_name: str
    port: int
    conda_env: str
    max_model_len: int
    gpu_memory_utilization: float
    max_num_seqs: int = 4
    kv_cache_dtype: str = "auto"
    speculative_config: Optional[str] = None
    extra_flags: str = ""
    sleep_mode: Optional[SleepModeConfig] = None
    startup_timeout: int = 0  # seconds for health check; 0 = use global HEALTH_CHECK_TIMEOUT

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
class OllamaModelConfig:
    """Ollama 模型引用 — 不管理 daemon，只声明模型名."""
    model_ref: str  # "llama3.1:8b"
    keep_alive: str = "5m"
    num_gpu: int = -1  # -1=auto, 0=CPU only, N=GPU layers


@dataclass
class OllamaCppConfig:
    """Ollama.cpp / llama.cpp 独立推理进程."""
    model_path: str     # GGUF 文件路径
    port: int = 11435
    threads: int = 8
    context_size: int = 8192
    gpu_layers: int = 0  # 0=CPU only, -1=all GPU, N=部分


@dataclass
class OllamaDaemonConfig:
    """Ollama 守护进程 — 基础设施服务."""
    port: int = 11434
    health_url: str = "http://localhost:11434"
    data_dir: str = ""


class PortPool:
    """Port range conventions for different service types.

    Each range reserves 10 ports (e.g. 11440-11449 for embeddings).
    Ports are configured statically in model YAML files; this class
    documents the convention for administrators.
    """
    EMBEDDING_START = 11440
    # Future ranges: OLLAMA_CPP_START = 11430, etc.


@dataclass
class ModelConfig:
    """A deployable model/service — one per YAML in models.d/.

    Core attributes:
      name:        Unique identifier (must match YAML filename stem)
      description: Human-readable description
      gpu_role:   'exclusive' (GPU fully locked) | 'shared' (coexists with other shared services) | 'none' (CPU-only)
      type:        'vllm' | 'comfyui' | 'ollama' | 'ollama_cpp' | 'ollama_daemon'
      vllm:        VLLMConfig if type='vllm'
      comfyui:     ComfyUIConfig if type='comfyui'
      ollama:      OllamaModelConfig if type='ollama'
      ollama_cpp:  OllamaCppConfig if type='ollama_cpp'
      ollama_daemon: OllamaDaemonConfig if type='ollama_daemon'
      model_type:  'llm' | 'vl' | 'omni' | 'aigc' | 'embedding' — capability classification
      quantization: quantization format string (e.g. 'NVFP4', 'GPTQ-4bit', 'Q8_0')
    """
    name: str
    description: str
    gpu_role: str = "exclusive"  # 'exclusive' | 'shared' | 'none'

    @property
    def mode(self) -> str:
        """Alias for gpu_role for backward compatibility."""
        return self.gpu_role

    type: str = "vllm"  # 'vllm' | 'comfyui' | 'ollama' | 'ollama_cpp' | 'ollama_daemon'
    vllm: Optional[VLLMConfig] = None
    comfyui: Optional[ComfyUIConfig] = None
    ollama: Optional[OllamaModelConfig] = None
    ollama_cpp: Optional[OllamaCppConfig] = None
    ollama_daemon: Optional[OllamaDaemonConfig] = None
    typical_vram_pct: float = 0.0
    peak_vram_mb: int = 0  # measured peak VRAM + safety margin; 0 = unknown/unchecked
    model_type: str = "llm"  # 'llm' | 'vl' | 'omni' | 'aigc' | 'embedding'
    modality: str = "text"  # 'text' | 'text-vision' | 'multimodal' | 'aigc' | 'embedding'
    quantization: str = ""  # quantization format: 'NVFP4', 'GPTQ-4bit', 'Q8_0', etc.

    # Fields excluded from config hash (runtime / non-startup)
    _HASH_EXCLUDE_FIELDS = frozenset({"typical_vram_pct", "peak_vram_mb", "startup_timeout"})

    def config_hash(self) -> str:
        """Deterministic hash of all config fields that affect startup behavior.

        Excludes _HASH_EXCLUDE_FIELDS and None values.  Used for drift detection
        so that a running service is automatically restarted when its YAML changes.
        """
        payload = {}
        for f in dataclasses.fields(self):
            if f.name.startswith("_"):
                continue
            if f.name in self._HASH_EXCLUDE_FIELDS:
                continue
            val = getattr(self, f.name)
            if val is None:
                continue
            # Recurse into nested dataclasses (VLLMConfig, ComfyUIConfig, etc.)
            if dataclasses.is_dataclass(val) and not isinstance(val, type):
                val = dataclasses.asdict(val)
                # Exclude runtime-only fields from nested dataclasses
                for excl in ("startup_timeout",):
                    val.pop(excl, None)
            payload[f.name] = val
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def port(self) -> Optional[int]:
        """Unified port accessor — eliminates per-backend if/else in proxy."""
        if self.vllm:
            return self.vllm.port
        if self.ollama_daemon:
            return self.ollama_daemon.port
        if self.ollama:
            return 11434  # Ollama daemon fixed port
        if self.ollama_cpp:
            return self.ollama_cpp.port
        if self.comfyui:
            return self.comfyui.port
        return None

    @property
    def served_name(self) -> Optional[str]:
        """Unified served_name for proxy routing."""
        if self.vllm:
            return self.vllm.served_name
        if self.ollama:
            return self.ollama.model_ref
        if self.ollama_cpp:
            return self.name
        return self.name

    @property
    def needs_gpu(self) -> bool:
        return self.gpu_role != "none"

    @property
    def is_exclusive(self) -> bool:
        return self.gpu_role == "exclusive"

    @property
    def is_shared(self) -> bool:
        return self.gpu_role == "shared"

    @property
    def is_gpu_none(self) -> bool:
        return self.gpu_role == "none"

    @property
    def is_vllm(self) -> bool:
        return self.type == "vllm" and self.vllm is not None

    @property
    def is_comfyui(self) -> bool:
        return self.type == "comfyui" and self.comfyui is not None

    @property
    def is_ollama(self) -> bool:
        return self.type == "ollama" and self.ollama is not None

    @property
    def is_ollama_cpp(self) -> bool:
        return self.type == "ollama_cpp" and self.ollama_cpp is not None

    @property
    def is_ollama_daemon(self) -> bool:
        return self.type == "ollama_daemon" and self.ollama_daemon is not None


# ─── Legacy Profile class (backward compat, will be removed in Phase 7) ──

@dataclass
class Profile:
    name: str
    description: str
    gpu_owner: str
    vllm: Optional[VLLMConfig] = None
    comfyui: Optional[ComfyUIConfig] = None
    switch_cost_sec: int = 0


# ─── Model Loading ───────────────────────────────────────────────

def load_models(models_dir: Path = MODELS_DIR) -> dict[str, ModelConfig]:
    """Load model configs from models.d/ directory.

    Each YAML file defines one model. The 'name' field must match the filename stem.
    Returns dict keyed by model name.
    """
    result: dict[str, ModelConfig] = {}
    if not models_dir.exists():
        return result

    for yaml_file in sorted(models_dir.glob("*.yaml")):
        raw = yaml.safe_load(yaml_file.read_text())
        model_name = yaml_file.stem

        # Skip empty or invalid YAML files
        if raw is None:
            log.warning("Skipping empty YAML: %s", yaml_file.name)
            continue

        # Skip non-model YAML files (e.g. model_affinity.yaml)
        if not isinstance(raw, dict) or "name" not in raw:
            log.debug("Skipping non-model YAML: %s", yaml_file.name)
            continue

        # Validate name matches filename
        if raw.get("name") != model_name:
            raise ValueError(
                f"Name mismatch in {yaml_file}: YAML name='{raw.get('name')}' "
                f"vs filename stem='{model_name}'"
            )

        # Parse type
        model_type = raw.get("type", "vllm")

        # Parse vllm config if present
        vllm_cfg = None
        if raw.get("vllm"):
            vllm_raw = dict(raw["vllm"])
            # Extract sleep_mode sub-config before passing to VLLMConfig
            sleep_cfg = None
            if "sleep_mode" in vllm_raw:
                sleep_raw = vllm_raw.pop("sleep_mode")
                if sleep_raw and sleep_raw.get("enabled"):
                    sleep_cfg = SleepModeConfig(**sleep_raw)
            vllm_cfg = VLLMConfig(**vllm_raw)
            vllm_cfg.sleep_mode = sleep_cfg
            # Parse startup_timeout from vllm section (overrides global)
            if "startup_timeout" in vllm_raw:
                vllm_cfg.startup_timeout = int(vllm_raw["startup_timeout"])

        # Parse comfyui config if present
        comfy_cfg = None
        if raw.get("comfyui"):
            comfy_cfg = ComfyUIConfig(**raw["comfyui"])

        # For type=comfyui, parse top-level comfyui fields
        if model_type == "comfyui" and not comfy_cfg:
            comfy_fields = {}
            for f in ("conda_env", "port", "working_dir", "health_url", "extra_flags"):
                if f in raw:
                    comfy_fields[f] = raw[f]
            if comfy_fields:
                comfy_cfg = ComfyUIConfig(**comfy_fields)

        # Parse ollama config if present
        ollama_cfg = None
        if raw.get("ollama"):
            ollama_cfg = OllamaModelConfig(**raw["ollama"])

        # Parse ollama_cpp config if present
        ollama_cpp_cfg = None
        if raw.get("ollama_cpp"):
            ollama_cpp_cfg = OllamaCppConfig(**raw["ollama_cpp"])

        # Parse ollama_daemon config if present
        ollama_daemon_cfg = None
        if raw.get("ollama_daemon"):
            ollama_daemon_cfg = OllamaDaemonConfig(**raw["ollama_daemon"])

        # For type=ollama, parse top-level ollama fields
        if model_type == "ollama" and not ollama_cfg:
            ollama_fields = {}
            for f in ("model_ref", "keep_alive"):
                if f in raw:
                    ollama_fields[f] = raw[f]
            if ollama_fields:
                ollama_cfg = OllamaModelConfig(**ollama_fields)

        # For type=ollama_cpp, parse top-level ollama_cpp fields
        if model_type == "ollama_cpp" and not ollama_cpp_cfg:
            cpp_fields = {}
            for f in ("model_path", "port", "threads", "context_size", "gpu_layers"):
                if f in raw:
                    cpp_fields[f] = raw[f]
            if cpp_fields:
                ollama_cpp_cfg = OllamaCppConfig(**cpp_fields)

        # For type=ollama_daemon, parse top-level ollama_daemon fields
        if model_type == "ollama_daemon" and not ollama_daemon_cfg:
            daemon_fields = {}
            for f in ("port", "health_url", "data_dir"):
                if f in raw:
                    daemon_fields[f] = raw[f]
            if daemon_fields:
                ollama_daemon_cfg = OllamaDaemonConfig(**daemon_fields)

        # Backward compat: YAML 'mode' → gpu_role
        mode_val = raw.get("mode", raw.get("gpu_role", "none"))
        result[model_name] = ModelConfig(
            name=model_name,
            description=raw.get("description", model_name),
            gpu_role=mode_val,
            type=model_type,
            vllm=vllm_cfg,
            comfyui=comfy_cfg,
            ollama=ollama_cfg,
            ollama_cpp=ollama_cpp_cfg,
            ollama_daemon=ollama_daemon_cfg,
            typical_vram_pct=float(raw.get("typical_vram_pct", 0)),
            peak_vram_mb=int(raw.get("peak_vram_mb", 0)),
            model_type=raw.get("model_type", "llm"),
            quantization=raw.get("quantization", ""),
            modality=raw.get("modality", "text"),
        )

    return result


# ─── Legacy Profile Loading (backward compat, will be removed in Phase 7) ──

def load_profiles(profiles_path: Path) -> dict[str, Profile]:
    """Load profiles from YAML configuration file. (Legacy, will be removed.)"""
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


# ─── Retry Constants (CCR-style) ─────────────────────────────────

UPSTREAM_RETRY_BASE_S = 0.5
UPSTREAM_RETRY_MAX_S = 2.0
UPSTREAM_LOCAL_RETRIES = 2  # 1 attempt + 2 retries = 3 total local attempts


def exponential_backoff(attempt: int) -> float:
    """CCR-style exponential backoff: base * 2^attempt, clamped to max."""
    return min(UPSTREAM_RETRY_MAX_S, UPSTREAM_RETRY_BASE_S * (2 ** attempt))


def should_retry_on_status(status: int) -> bool:
    """Should we retry on this HTTP status? CCR-style decision.

    - 5xx / 408 / 429 → retry with backoff
    - 4xx (non-retryable) → skip retry, fall back immediately
    - 2xx / 3xx → success, no retry needed
    """
    if status >= 500 or status in (408, 429):
        return True
    return False


def parse_retry_after_ms(headers: dict) -> float | None:
    """Parse retry-after header (seconds or date), CCR-style.
    Returns milliseconds to wait, or None."""
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        secs = float(raw.strip())
        return max(0, min(secs * 1000, 60_000))  # cap at 60s like CCR
    except ValueError:
        pass
    return None


# ─── Model Affinity (static routing) ──────────────────────────────

def load_model_affinity(models_dir: Path = MODELS_DIR) -> dict[str, str]:
    """Load model_affinity.yaml → {model_name_pattern: routing_target}.

    Cached with mtime invalidation — called on every request (hot path).
    Thread-safe: uses module-level lock for cache reads/writes.

    Example YAML:
      baidu:
        - "deepseek-v4-flash"
        - "glm-5"

    Returns: {"deepseek-v4-flash": "baidu", "glm-5": "baidu", ...}
    """
    affinity_file = models_dir / "model_affinity.yaml"
    if not affinity_file.exists():
        return {}

    # Cache with mtime invalidation (thread-safe)
    try:
        current_mtime = affinity_file.stat().st_mtime
    except OSError:
        return {}

    with _affinity_lock:
        if load_model_affinity._cache is not None and load_model_affinity._mtime == current_mtime:
            return load_model_affinity._cache

    # I/O outside lock
    raw = yaml.safe_load(affinity_file.read_text())
    if not raw or not isinstance(raw, dict):
        result = {}
    else:
        result = {}
        for target, patterns in raw.items():
            if isinstance(patterns, list):
                for p in patterns:
                    result[p] = target

    with _affinity_lock:
        load_model_affinity._cache = result
        load_model_affinity._mtime = current_mtime
    return result


_affinity_lock = threading.Lock()
load_model_affinity._cache = None
load_model_affinity._mtime = 0.0
