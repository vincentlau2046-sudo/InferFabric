"""v2 限流器 — 二级令牌桶 (server + model)

替代 v1 的 Semaphore 方案，提供：
  - 服务级 RPM 限流（单用户总体上限）
  - 模型级 RPM 限流（按 max_num_seqs 配置）
  - asyncio 兼容（handler 同步桥接用 acquire_sync）
  - 向后兼容：import 路径不变，旧接口仍可用

配置 iff.yaml:
  rate_limit:
    server_rpm: 60        # 服务级每分钟请求数上限
    model_rpm_default: 20 # 模型级默认 RPM
    timeout: 30           # acquire 超时秒数
"""

import asyncio
import threading
import time
import logging
from dataclasses import dataclass

log = logging.getLogger("inferfabric.ratelimit")


@dataclass
class BucketConfig:
    """单个令牌桶配置。"""
    rpm: float          # 每分钟请求数
    burst: int          # 突发上限（= rpm 上取整，至少 1）
    timeout: float = 30.0


class TokenBucket:
    """线程安全的令牌桶。

    - refill_rate: 每秒补充的令牌数 = rpm / 60
    - burst: 桶容量上限
    - acquire 超时返回 False
    """

    def __init__(self, config: BucketConfig):
        self._rate = config.rpm / 60.0       # tokens per second
        self._burst = max(1, config.burst)
        self._timeout = config.timeout
        self._tokens: float = float(self._burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float | None = None) -> bool:
        """尝试获取一个令牌，超时返回 False。"""
        deadline = time.monotonic() + (timeout or self._timeout)
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(remaining, 0.1))

    def try_acquire(self) -> bool:
        """非阻塞尝试获取令牌。"""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def release(self):
        """归还一个令牌（用于并发计数模式）。"""
        with self._lock:
            self._tokens = min(self._burst, self._tokens + 1.0)

    @property
    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    def _refill(self):
        """补充令牌（调用者需持锁）。"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now


class RateLimiterV2:
    """二级限流器：server + model。

    用法：
        limiter = RateLimiterV2(server_rpm=60, model_rpm_default=20)
        ok = limiter.acquire("qwen36-35b")
        limiter.release("qwen36-35b")
    """

    def __init__(
        self,
        server_rpm: float = 60,
        model_rpm_default: float = 20,
        timeout: float = 30.0,
    ):
        self._server_bucket = TokenBucket(BucketConfig(
            rpm=server_rpm, burst=max(1, int(server_rpm)), timeout=timeout,
        ))
        self._model_buckets: dict[str, TokenBucket] = {}
        self._model_rpm_default = model_rpm_default
        self._timeout = timeout
        self._lock = threading.Lock()

    def register_model(self, model_name: str, rpm: float | None = None):
        """注册模型级限流桶。rpm=None 使用默认值。"""
        effective_rpm = rpm or self._model_rpm_default
        with self._lock:
            self._model_buckets[model_name] = TokenBucket(BucketConfig(
                rpm=effective_rpm, burst=max(1, int(effective_rpm)), timeout=self._timeout,
            ))
        log.debug("Registered model rate bucket: %s → %.0f RPM", model_name, effective_rpm)

    def acquire(self, model_name: str, timeout: float | None = None) -> tuple[bool, str]:
        """二级 acquire：先 server，再 model。

        Returns:
            (通过, 原因) — True/False + 限流层级
        """
        # Level 1: server
        if not self._server_bucket.acquire(timeout):
            return False, "server_rate_limit"
        # Level 2: model
        bucket = self._get_model_bucket(model_name)
        if not bucket.acquire(timeout):
            # 归还 server 令牌
            self._server_bucket.release()
            return False, f"model_rate_limit:{model_name}"
        return True, "ok"

    def try_acquire(self, model_name: str) -> tuple[bool, str]:
        """非阻塞二级 acquire。"""
        if not self._server_bucket.try_acquire():
            return False, "server_rate_limit"
        bucket = self._get_model_bucket(model_name)
        if not bucket.try_acquire():
            self._server_bucket.release()
            return False, f"model_rate_limit:{model_name}"
        return True, "ok"

    def release(self, model_name: str):
        """归还 server + model 令牌。"""
        self._server_bucket.release()
        bucket = self._get_model_bucket(model_name)
        bucket.release()

    @property
    def server_available(self) -> float:
        return self._server_bucket.available

    def model_available(self, model_name: str) -> float:
        return self._get_model_bucket(model_name).available

    def _get_model_bucket(self, model_name: str) -> TokenBucket:
        with self._lock:
            if model_name not in self._model_buckets:
                effective_rpm = self._model_rpm_default
                self._model_buckets[model_name] = TokenBucket(BucketConfig(
                    rpm=effective_rpm, burst=max(1, int(effective_rpm)), timeout=self._timeout,
                ))
                log.debug("Auto-registered model rate bucket: %s → %.0f RPM", model_name, effective_rpm)
            return self._model_buckets[model_name]

    # ── 向后兼容旧接口 ──

    def clear(self):
        """清空模型级桶缓存。"""
        with self._lock:
            self._model_buckets.clear()


# ── v1 兼容层（渐进迁移，PR-D 后删除）──

_v1_lock = threading.Lock()


class _RateLimiter:
    """v1 兼容：Semaphore 模式。PR-D 后删除。"""
    def __init__(self, max_concurrent: int = 6, timeout: float = 30.0):
        self._sem = threading.Semaphore(max_concurrent)
        self._timeout = timeout

    def acquire(self) -> bool:
        return self._sem.acquire(timeout=self._timeout)

    def release(self):
        self._sem.release()


_VLLM_RATE_LIMITER = _RateLimiter(max_concurrent=6, timeout=30.0)
_MODEL_RATE_LIMITERS: dict[str, _RateLimiter] = {}
_MODEL_LIMITER_MAX = 50


def _get_model_rate_limiter(pm, model_name: str) -> _RateLimiter:
    """v1 兼容接口。PR-D 后删除。线程安全。"""
    with _v1_lock:
        if model_name in _MODEL_RATE_LIMITERS:
            return _MODEL_RATE_LIMITERS[model_name]
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
        max_concurrent = max(2, min(max_concurrent, 20))
        limiter = _RateLimiter(max_concurrent=max_concurrent, timeout=30.0)
        _MODEL_RATE_LIMITERS[model_name] = limiter
        return limiter
