"""
inferfabric/health_checker.py — Unified model health checking.

Extracted from manager.py (v4.1 P4): consolidates the duplicated if/elif
health-check chains (~45 lines × 4 locations) into a single function.

Usage:
    from .health_checker import check_model_health
    health = check_model_health(model)   # returns "✅", "⏳", "❌", or "?"

New model types only need to be added in this one function.
"""

from .health import check_http_status


class DefaultHealthChecker:
    """Concrete IHealthChecker implementation that delegates to check_model_health().

    Used as the default fallback when no IHealthChecker is injected.
    Implements check() / wait() / gpu_used_mb() for the protocol,
    plus check_model() for ModelManager internal dispatch.
    """

    def check(self, url: str, timeout: int = 3) -> str:
        return check_http_status(url)

    def wait(self, url: str, timeout: int = 300) -> bool:
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.check(url) == "✅":
                return True
            time.sleep(1)
        return False

    def gpu_used_mb(self) -> int:
        from .health import gpu_used_mb
        return gpu_used_mb()

    def check_model(self, model) -> str:
        """Check health of a ModelConfig by type, delegating to check_model_health()."""
        return check_model_health(model)


def check_model_health(model) -> str:
    """Check health of a model service by type, returning emoji status.

    Returns:
        "✅" — healthy, "⏳" — starting, "❌" — unhealthy, "?" — unknown type
    """
    if model.is_vllm:
        return check_http_status(f"http://localhost:{model.vllm.port}/health")
    elif model.is_comfyui:
        url = model.comfyui.health_url or f"http://localhost:{model.comfyui.port}/system_stats"
        return check_http_status(url)
    elif model.is_ollama_daemon:
        return check_http_status(f"http://localhost:{model.ollama_daemon.port}/api/tags")
    elif model.is_ollama:
        return check_http_status("http://localhost:11434/api/tags")
    elif model.is_ollama_cpp:
        return check_http_status(f"http://localhost:{model.ollama_cpp.port}/health")
    return "?"