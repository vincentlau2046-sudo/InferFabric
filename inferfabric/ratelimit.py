"""
inferfabric/ratelimit.py — Per-model vLLM concurrency rate limiter.

Extracted from proxy.py for modularity.
"""

import threading
import logging

log = logging.getLogger("inferfabric.ratelimit")


class _RateLimiter:
    """Per-model vLLM concurrency gate.

    Semaphore caps concurrent requests to max_num_seqs.
    Requests failing to acquire within timeout → 429 + Retry-After.

    Only applies to local vLLM forwarding; Baidu/Ollama bypass.
    """

    def __init__(self, max_concurrent: int = 6, timeout: float = 30.0):
        self._sem = threading.Semaphore(max_concurrent)
        self._timeout = timeout
        self._max_concurrent = max_concurrent

    def acquire(self) -> bool:
        return self._sem.acquire(timeout=self._timeout)

    def release(self):
        self._sem.release()


# P0: matches qwen36-27b max_num_seqs=8
# TODO: dynamically read from model YAML; Baidu/Ollama excluded
_VLLM_RATE_LIMITER = _RateLimiter(max_concurrent=6, timeout=30.0)
_MODEL_RATE_LIMITERS: dict[str, _RateLimiter] = {}
_MODEL_LIMITER_MAX = 50


def _get_model_rate_limiter(pm, model_name: str) -> _RateLimiter:
    """Get per-model rate limiter based on YAML max_num_seqs."""
    if model_name in _MODEL_RATE_LIMITERS:
        return _MODEL_RATE_LIMITERS[model_name]
    # Guard against unbounded growth
    if len(_MODEL_RATE_LIMITERS) >= _MODEL_LIMITER_MAX:
        _MODEL_RATE_LIMITERS.clear()
    try:
        model_obj = pm.mgr.get_model(model_name)
        if model_obj and model_obj.vllm and model_obj.vllm.max_num_seqs:
            max_concurrent = model_obj.vllm.max_num_seqs
        else:
            max_concurrent = 6
    except Exception:
        max_concurrent = 6
    # Enforce bounds: [2, 20] — Semaphore(0) would block forever; cap at 20
    max_concurrent = max(2, min(max_concurrent, 20))
    limiter = _RateLimiter(max_concurrent=max_concurrent, timeout=30.0)
    _MODEL_RATE_LIMITERS[model_name] = limiter
    return limiter
