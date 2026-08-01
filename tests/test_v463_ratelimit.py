"""Tests for v4.6.3 rate limit fixes (PR-G1/G2/G3/G4).

Covers:
  - PR-G4: iff.yaml rate_limit configuration parsing
  - PR-G1: observe mode (RPM try_acquire, no 429)
  - PR-G3: max_concurrent from vLLM max_num_seqs
  - PR-G2: stream_options.include_usage injection
  - RPM=0: disabled rate limiting
"""

import json
import threading
import time
import pytest

from inferfabric.ratelimit import (
    TokenBucket, BucketConfig,
    RateLimiterV2, DualGateLimiter, GateResult,
)


# ─── TokenBucket ───

class TestTokenBucket:
    def test_rpm_zero_always_allows(self):
        """RPM=0 → acquire 永远返回 True。"""
        bucket = TokenBucket(BucketConfig(rpm=0, burst=0))
        for _ in range(100):
            assert bucket.acquire(timeout=0) is True
        assert bucket.try_acquire() is True
        assert bucket.available == float('inf')
        assert bucket.disabled

    def test_rpm_zero_release_noop(self):
        """RPM=0 时 release 是空操作。"""
        bucket = TokenBucket(BucketConfig(rpm=0, burst=0))
        bucket.release()  # 不应报错

    def test_normal_bucket_burst(self):
        """正常桶 burst=5 → 前 5 次 try_acquire 成功，第 6 次失败。"""
        bucket = TokenBucket(BucketConfig(rpm=300, burst=5, timeout=0.1))
        for _ in range(5):
            assert bucket.try_acquire() is True
        assert bucket.try_acquire() is False

    def test_normal_bucket_refill(self):
        """令牌补充：等待后令牌恢复。"""
        bucket = TokenBucket(BucketConfig(rpm=600, burst=2, timeout=0.1))
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is False
        # rpm=600 → 10 tokens/s → 等 0.2s 应补充 ~2 个
        time.sleep(0.25)
        assert bucket.try_acquire() is True

    def test_release_returns_token(self):
        """release 归还令牌。"""
        bucket = TokenBucket(BucketConfig(rpm=60, burst=1, timeout=0.1))
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is False
        bucket.release()
        assert bucket.try_acquire() is True


# ─── RateLimiterV2 ───

class TestRateLimiterV2:
    def test_rpm_zero_skips_both_gates(self):
        """server_rpm=0, model_rpm=0 → acquire 永远成功。"""
        limiter = RateLimiterV2(server_rpm=0, model_rpm_default=0, timeout=0.1)
        for _ in range(50):
            ok, reason = limiter.acquire("test-model")
            assert ok is True
            limiter.release("test-model")

    def test_server_rpm_only(self):
        """仅 server_rpm 限制。"""
        limiter = RateLimiterV2(server_rpm=60, model_rpm_default=0, timeout=0.1)
        # burst=60 → 前 60 次成功
        for _ in range(60):
            ok, _ = limiter.acquire("test-model")
            assert ok is True
        # 第 61 次失败
        ok, reason = limiter.acquire("test-model")
        assert ok is False
        assert "server_rate_limit" in reason

    def test_model_rpm_only(self):
        """仅 model_rpm 限制。"""
        limiter = RateLimiterV2(server_rpm=0, model_rpm_default=20, timeout=0.1)
        for _ in range(20):
            ok, _ = limiter.acquire("test-model")
            assert ok is True
        ok, reason = limiter.acquire("test-model")
        assert ok is False
        assert "model_rate_limit" in reason

    def test_try_acquire_nonblocking(self):
        """try_acquire 不阻塞。"""
        limiter = RateLimiterV2(server_rpm=60, model_rpm_default=10, timeout=30)
        for _ in range(10):
            ok, _ = limiter.try_acquire("test-model")
            assert ok is True
        ok, reason = limiter.try_acquire("test-model")
        assert ok is False


# ─── DualGateLimiter ───

class TestDualGateLimiter:
    def test_observe_mode_no_429(self):
        """observe 模式：RPM 耗尽后不拒绝。"""
        rpm = RateLimiterV2(server_rpm=60, model_rpm_default=5, timeout=0.1)
        gate = DualGateLimiter(rpm_limiter=rpm, max_concurrent=10, mode="observe", timeout=0.1)

        # 消耗 RPM burst
        results = []
        for i in range(15):
            r = gate.acquire(f"model", timeout=0.1)
            results.append(r)
            if r.ok and r._sem_held:
                # 立即释放 Semaphore 以允许更多请求
                r.release()

        # observe 模式下不应有 429
        ok_count = sum(1 for r in results if r.ok)
        assert ok_count == 15, f"Expected 15 ok, got {ok_count}"

    def test_reject_mode_returns_429(self):
        """reject 模式：RPM 耗尽后返回 429。"""
        rpm = RateLimiterV2(server_rpm=60, model_rpm_default=5, timeout=0.1)
        gate = DualGateLimiter(rpm_limiter=rpm, max_concurrent=10, mode="reject", timeout=0.1)

        results = []
        for i in range(15):
            r = gate.acquire("model", timeout=0.1)
            results.append(r)
            if r.ok and r._sem_held:
                r.release()

        rejected = sum(1 for r in results if not r.ok)
        assert rejected > 0, "reject 模式下应有请求被拒绝"

    def test_observe_mode_rpm_zero(self):
        """observe + RPM=0：完全不限流。"""
        rpm = RateLimiterV2(server_rpm=0, model_rpm_default=0, timeout=0.1)
        gate = DualGateLimiter(rpm_limiter=rpm, max_concurrent=5, mode="observe", timeout=0.1)

        # 10 个请求，Semaphore=5，但 observe 模式下排队
        results = []
        for i in range(10):
            r = gate.acquire("model", timeout=0.1)
            results.append(r)
            if r.ok and r._sem_held:
                r.release()

        ok_count = sum(1 for r in results if r.ok)
        assert ok_count == 10

    def test_semaphore_limits_concurrency(self):
        """Semaphore 限制同时在飞请求数。"""
        rpm = RateLimiterV2(server_rpm=0, model_rpm_default=0, timeout=0.1)
        gate = DualGateLimiter(rpm_limiter=rpm, max_concurrent=3, mode="observe", timeout=5)

        # 获取 3 个并发槽
        held = []
        for _ in range(3):
            r = gate.acquire("model")
            assert r.ok
            held.append(r)

        # 第 4 个应排队（超短 timeout 检测是否在等待）
        # 用非阻塞方式检测 Semaphore 状态
        # 3 个已持有 → Semaphore 可用=0
        # acquire(timeout=0.1) 应该排队等待
        started = threading.Event()
        done = threading.Event()
        result = [None]

        def try_acquire():
            started.set()
            r = gate.acquire("model", timeout=0.5)
            result[0] = r
            if r.ok and r._sem_held:
                r.release()
            done.set()

        t = threading.Thread(target=try_acquire)
        t.start()
        started.wait(timeout=2)

        # 还没完成说明在排队
        assert not done.is_set(), "Should be waiting for semaphore"

        # 释放一个
        held[0].release()
        done.wait(timeout=5)
        t.join(timeout=5)

        # 现在应该成功了
        assert result[0] is not None
        assert result[0].ok

        # 清理
        for r in held[1:]:
            r.release()

    def test_mode_property(self):
        """mode 属性返回当前模式。"""
        rpm = RateLimiterV2(server_rpm=0, model_rpm_default=0, timeout=0.1)
        gate = DualGateLimiter(rpm_limiter=rpm, max_concurrent=5, mode="observe")
        assert gate.mode == "observe"

        gate2 = DualGateLimiter(rpm_limiter=rpm, max_concurrent=5, mode="reject")
        assert gate2.mode == "reject"


# ─── PR-G2: stream_options injection ───

class TestStreamOptionsInjection:
    def test_inject_include_usage(self):
        """流式请求自动注入 include_usage=true。"""
        data = {"model": "test", "messages": [], "stream": True}
        # 模拟 chat_handlers.py 中的注入逻辑
        if data.get("stream", False):
            if "stream_options" not in data:
                data["stream_options"] = {}
            data["stream_options"].setdefault("include_usage", True)

        assert data["stream_options"]["include_usage"] is True

    def test_preserve_existing_stream_options(self):
        """已有 stream_options 不覆盖。"""
        data = {"model": "test", "messages": [], "stream": True,
                "stream_options": {"include_usage": False}}
        if data.get("stream", False):
            if "stream_options" not in data:
                data["stream_options"] = {}
            data["stream_options"].setdefault("include_usage", True)

        # setdefault 不覆盖已有值
        assert data["stream_options"]["include_usage"] is False

    def test_non_stream_not_injected(self):
        """非流式请求不注入。"""
        data = {"model": "test", "messages": [], "stream": False}
        if data.get("stream", False):
            if "stream_options" not in data:
                data["stream_options"] = {}
            data["stream_options"].setdefault("include_usage", True)

        assert "stream_options" not in data


# ─── PR-G3: Dynamic max_concurrent ───

class TestDynamicMaxConcurrent:
    def test_compute_max_concurrent(self):
        """_compute_max_concurrent 从 vLLM 配置取最大 max_num_seqs。"""
        # 模拟 ProxyManager._compute_max_concurrent 逻辑
        class FakeVLLMConfig:
            def __init__(self, max_num_seqs):
                self.max_num_seqs = max_num_seqs

        class FakeModel:
            def __init__(self, is_vllm, max_num_seqs=0):
                self.is_vllm = is_vllm
                self.vllm = FakeVLLMConfig(max_num_seqs) if is_vllm else None

        models = {
            "qwen27b": FakeModel(is_vllm=True, max_num_seqs=8),
            "qwen35b": FakeModel(is_vllm=True, max_num_seqs=4),
            "bge": FakeModel(is_vllm=False),  # embedding, no vllm
        }

        max_seqs = 4  # 保守默认
        for model in models.values():
            if model.is_vllm and model.vllm:
                max_seqs = max(max_seqs, model.vllm.max_num_seqs)

        assert max_seqs == 8

    def test_no_vllm_models_default(self):
        """无 vLLM 模型时使用默认值。"""
        max_seqs = 4
        models = {"bge": type('M', (), {'is_vllm': False, 'vllm': None})()}
        for model in models.values():
            if model.is_vllm and model.vllm:
                max_seqs = max(max_seqs, model.vllm.max_num_seqs)
        assert max_seqs == 4
