"""EdgeLLM — Local LLM Profile Switcher"""

from .manager import ProfileManager
from .state import ProfileState, StateDB
from .gpu_lock import GPULock
from .process_manager import ProcessManager
from .config import (
    VLLMConfig,
    ComfyUIConfig,
    Profile,
)
from .health import (
    gpu_used_mb,
    gpu_total_mb,
    wait_http,
    check_http_status,
)

__version__ = "3.1.0"
