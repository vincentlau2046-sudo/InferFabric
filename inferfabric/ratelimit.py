"""v2 限流器 — 二级令牌桶 (server + model) + 可配置模式

v4.6.3 重构：
  - mode="observe": RPM 门仅记录不拒绝，Semaphore 排队（单用户默认）
  - mode="reject": RPM 门拒绝(429)，Semaphore 排队（多用户场景）
  - rpm=0: 跳过 RPM 门
  - max_concurrent: auto=从 vLLM max_num_seqs 动态获取
  - 超时可配置，默认 5s（reject 模式）/ 无限等待（observe 模式）

配置 iff.yaml:
  rate_limit:
    mode: observe           # observe=记录不拒绝, reject=429拒绝
    server_rpm: 0           # 0=不限流
    model_rpm_default: 0    # 0=不限流
    max_concurrent: auto    # auto=从 max_num_seqs 获取
    timeout: 5              # acquire 超时秒数
"""

import threading
import time
import logging
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger("inferfabric.ratelimit")

# 限流模式
RateLimitMode = Literal["observe", "reject"]


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
    - rpm=0 时 burst=0，acquire 永远返回 True（无限流）
    """

    def __init__(self, config: BucketConfig):
        if config.rpm <= 0:
            # RPM=0: 无限流模式，令牌桶不生效
            self._rate = 0.0
            self._burst = 0
            self._tokens = 0.0
            self._disabled = True
        else:
            self._rate = config.rpm / 60.0
            self._burst = max(1, config.burst)
            self._tokens = float(self._burst)
            self._disabled = False
        self._timeout = config.timeout
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float | None = None) -> bool:
        """尝试获取一个令牌，超时返回 False。RPM=0 时直接返回 True。"""
        if self._disabled:
            return True
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
        """非阻塞尝试获取令牌。RPM=0 时直接返回 True。"""
        if self._disabled:
            return True
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def release(self):
        """归还一个令牌（用于并发计数模式）。RPM=0 时无效。"""
        if self._disabled:
            return
        with self._lock:
            self._tokens = min(self._burst, self._tokens + 1.0)

    @property
    def available(self) -> float:
        """当前可用令牌数。RPM=0 时返回 inf。"""
        if self._disabled:
            return float('inf')
        with self._lock:
            self._refill()
            return self._tokens

    @property
    def disabled(self) -> bool:
        return self._disabled

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
        ok = limiter.acquire("qwen36-35b-vl")
        limiter.release("qwen36-35b-vl")
    """

    def __init__(
        self,
        server_rpm: float = 0,
        model_rpm_default: float = 0,
        timeout: float = 5.0,
    ):
        self._server_bucket = TokenBucket(BucketConfig(
            rpm=server_rpm, burst=max(1, int(server_rpm)) if server_rpm > 0 else 0, timeout=timeout,
        ))
        self._model_buckets: dict[str, TokenBucket] = {}
        self._model_rpm_default = model_rpm_default
        self._timeout = timeout
        self._lock = threading.Lock()

    def register_model(self, model_name: str, rpm: float | None = None):
        """注册模型级限流桶。rpm=None 使用默认值。"""
        effective_rpm = rpm if rpm is not None else self._model_rpm_default
        with self._lock:
            self._model_buckets[model_name] = TokenBucket(BucketConfig(
                rpm=effective_rpm,
                burst=max(1, int(effective_rpm)) if effective_rpm > 0 else 0,
                timeout=self._timeout,
            ))
        log.debug("Registered model rate bucket: %s → %.0f RPM", model_name, effective_rpm)

    def acquire(self, model_name: str, timeout: float | None = None) -> tuple[bool, str]:
        """二级 acquire：先 server，再 model。RPM=0 时跳过对应门。

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
        """非阻塞二级 acquire。RPM=0 时跳过对应门。"""
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
                    rpm=effective_rpm,
                    burst=max(1, int(effective_rpm)) if effective_rpm > 0 else 0,
                    timeout=self._timeout,
                ))
                log.debug("Auto-registered model rate bucket: %s → %.0f RPM", model_name, effective_rpm)
            return self._model_buckets[model_name]

    def clear(self):
        """清空模型级桶缓存。"""
        with self._lock:
            self._model_buckets.clear()


# ── DualGate ──

class DualGateLimiter:
    """二级嵌套限流门 — RPM 软门 + 并发硬门

    Gate 1: RateLimiterV2 (TokenBucket, RPM) — 限每分钟请求总量
    Gate 2: Semaphore (max_concurrent) — 限同时在飞请求数

    模式:
      observe (默认): RPM 门仅观测记录，不拒绝请求。Semaphore 排队等待。
                      单用户系统中，排队比拒绝好——流控为了稳定生产，不是拒绝请求。
      reject: RPM 门拒绝超限请求(429)。Semaphore 排队等待超时后也拒绝。
              适用于多用户场景，保护系统不被过载。

    RPM=0: 跳过 RPM 门（等效于关闭速率限流，仅保留并发限流）。

    cloud 路由不经过此门（单用户 + 云端自有配额）。

    使用 Releasable 句柄确保 acquire/release 对称：
        gate = pm.dual_gate.acquire(model_name)
        if not gate.ok:
            return 429
        try: ...
        finally: gate.release()
    """

    def __init__(
        self,
        rpm_limiter: RateLimiterV2,
        max_concurrent: int = 8,
        mode: RateLimitMode = "observe",
        timeout: float = 5.0,
    ):
        self._rpm = rpm_limiter
        self._concurrency = threading.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._mode = mode
        self._timeout = timeout

    def acquire(self, model: str, timeout: float | None = None) -> 'GateResult':
        """尝试通过双门。返回 GateResult 句柄。

        observe 模式:
          - RPM try_acquire 失败 → 仅记录日志，不拒绝
          - Semaphore 阻塞等待（不超时），确保请求最终被处理
        reject 模式:
          - RPM acquire 超时 → 拒绝(429)
          - Semaphore acquire 超时 → 拒绝(429)
        """
        effective_timeout = timeout or self._timeout

        # Gate 1: RPM
        rpm_held = False
        if self._mode == "observe":
            # 观测模式：非阻塞尝试，失败只记日志
            rpm_ok, rpm_reason = self._rpm.try_acquire(model)
            if rpm_ok:
                rpm_held = True
            else:
                log.warning("RPM observe: %s would be rate-limited (%s) — allowing", model, rpm_reason)
                # 不拒绝，RPM 令牌不消耗 → rpm_held=False
        else:
            # reject 模式：阻塞等待令牌
            rpm_ok, rpm_reason = self._rpm.acquire(model, effective_timeout)
            if not rpm_ok:
                return GateResult(self, model, ok=False, reason=f"rpm_limit: {rpm_reason}", rpm_held=False)
            rpm_held = True

        # Gate 2: Concurrency (Semaphore)
        if self._mode == "observe":
            # 观测模式：Semaphore 不超时，排队等待确保请求被处理
            # 但设置一个合理的上限防止死锁（5 分钟）
            acquired = self._concurrency.acquire(timeout=300)
            if not acquired:
                # 极端情况：5 分钟都没拿到并发槽 — 仍然不拒绝，记录警告
                log.error("Concurrency observe: %s waited 300s for semaphore — this indicates a bug", model)
                # 放行请求，不持 Semaphore（可能超载，但拒绝更糟）
                return GateResult(self, model, ok=True, reason="", rpm_held=rpm_held, sem_held=False)
        else:
            # reject 模式：Semaphore 超时拒绝
            acquired = self._concurrency.acquire(timeout=effective_timeout)
            if not acquired:
                # Gate 2 失败，归还 RPM 令牌
                if rpm_held:
                    self._rpm.release(model)
                return GateResult(self, model, ok=False, reason="concurrency_limit", rpm_held=False)

        return GateResult(self, model, ok=True, reason="", rpm_held=rpm_held, sem_held=True)

    def _release(self, model: str, rpm_held: bool, sem_held: bool = True):
        """内部：释放资源。

        RPM 令牌在 observe 模式下可能未被消耗（try_acquire 失败时）。
        Semaphore 总是释放（如果持有）。
        """
        if sem_held:
            self._concurrency.release()

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def mode(self) -> str:
        return self._mode


class GateResult:
    """acquire 返回的句柄 — 确保 acquire/release 对称。

    用法:
        gate = pm.dual_gate.acquire(model_name)
        if not gate.ok:
            return 429
        try:
            ...  # 业务逻辑
        finally:
            gate.release()
    """

    __slots__ = ('_limiter', '_model', 'ok', 'reason', '_rpm_held', '_sem_held')

    def __init__(
        self,
        limiter: DualGateLimiter,
        model: str,
        ok: bool,
        reason: str,
        rpm_held: bool,
        sem_held: bool = True,
    ):
        self._limiter = limiter
        self._model = model
        self.ok = ok
        self.reason = reason
        self._rpm_held = rpm_held
        self._sem_held = sem_held

    def release(self):
        """释放所有持有的资源。"""
        self._limiter._release(self._model, self._rpm_held, self._sem_held)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.ok:
            self.release()
        return False
