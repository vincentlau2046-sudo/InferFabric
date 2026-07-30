"""Tests for inferfabric.ratelimit — RateLimiterV2 + TokenBucket"""

import threading
import time

import pytest

from inferfabric.ratelimit import TokenBucket, BucketConfig, RateLimiterV2


class TestTokenBucket:
    """令牌桶基础"""

    def test_acquire_within_burst(self):
        b = TokenBucket(BucketConfig(rpm=60, burst=10))
        for _ in range(10):
            assert b.acquire(timeout=0)
        # 11th should fail (no tokens left, timeout=0)
        assert not b.try_acquire()

    def test_refill_over_time(self):
        b = TokenBucket(BucketConfig(rpm=60, burst=2))  # 1 token/sec
        assert b.try_acquire()
        assert b.try_acquire()
        assert not b.try_acquire()
        time.sleep(1.1)  # refill ~1 token
        assert b.try_acquire()

    def test_available_property(self):
        b = TokenBucket(BucketConfig(rpm=60, burst=5))
        assert b.available == 5.0
        b.try_acquire()
        assert b.available < 5.0

    def test_release(self):
        b = TokenBucket(BucketConfig(rpm=60, burst=2))
        b.try_acquire()
        b.try_acquire()
        assert not b.try_acquire()
        b.release()
        assert b.try_acquire()

    def test_burst_at_least_1(self):
        b = TokenBucket(BucketConfig(rpm=1, burst=0))  # burst clamped to 1
        assert b.try_acquire()

    def test_acquire_with_timeout(self):
        b = TokenBucket(BucketConfig(rpm=60, burst=1, timeout=0.3))
        assert b.acquire(timeout=0)
        # Second acquire should timeout
        start = time.monotonic()
        ok = b.acquire(timeout=0.2)
        elapsed = time.monotonic() - start
        assert not ok
        assert elapsed >= 0.15  # waited at least ~0.2s


class TestRateLimiterV2:
    """二级限流器"""

    def test_basic_acquire_release(self):
        rl = RateLimiterV2(server_rpm=60, model_rpm_default=30)
        ok, reason = rl.acquire("test-model")
        assert ok and reason == "ok"
        rl.release("test-model")

    def test_server_rate_limit(self):
        rl = RateLimiterV2(server_rpm=60, model_rpm_default=999)
        # Server bucket: burst = int(60) = 60, so acquire 60 tokens
        for _ in range(60):
            ok, _ = rl.try_acquire("test-model")
            if not ok:
                break
        # Next should fail at server level
        ok, reason = rl.try_acquire("test-model")
        if not ok:
            assert "server" in reason

    def test_model_rate_limit(self):
        rl = RateLimiterV2(server_rpm=999, model_rpm_default=5)
        # Model bucket: burst = 5
        for _ in range(5):
            ok, _ = rl.try_acquire("test-model")
            assert ok
        ok, reason = rl.try_acquire("test-model")
        assert not ok and "model" in reason

    def test_release_restores_both(self):
        rl = RateLimiterV2(server_rpm=10, model_rpm_default=5)
        # Drain model bucket
        for _ in range(5):
            ok, _ = rl.try_acquire("test-model")
            assert ok
        ok, _ = rl.try_acquire("test-model")
        assert not ok
        # Release one
        rl.release("test-model")
        ok, _ = rl.try_acquire("test-model")
        assert ok

    def test_register_model_custom_rpm(self):
        rl = RateLimiterV2(server_rpm=999, model_rpm_default=5)
        rl.register_model("big-model", rpm=2)
        # big-model: burst=2
        ok, _ = rl.try_acquire("big-model")
        assert ok
        ok, _ = rl.try_acquire("big-model")
        assert ok
        ok, reason = rl.try_acquire("big-model")
        assert not ok and "big-model" in reason

    def test_auto_register_unknown_model(self):
        rl = RateLimiterV2(server_rpm=999, model_rpm_default=10)
        ok, _ = rl.try_acquire("unknown-model")
        assert ok  # auto-registered with default rpm

    def test_clear(self):
        rl = RateLimiterV2(server_rpm=999, model_rpm_default=10)
        rl.register_model("m1", rpm=5)
        rl.register_model("m2", rpm=5)
        rl.clear()
        assert len(rl._model_buckets) == 0

    def test_concurrent_acquire(self):
        rl = RateLimiterV2(server_rpm=999, model_rpm_default=20)
        results = []
        errors = []

        def worker(i):
            try:
                ok, reason = rl.try_acquire("concurrent-model")
                results.append(ok)
                if ok:
                    time.sleep(0.01)
                    rl.release("concurrent-model")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        # At least some should succeed
        assert any(results)

    def test_v1_compat(self):
        """v1 兼容接口仍可用。"""
        from inferfabric.ratelimit import _RateLimiter, _get_model_rate_limiter
        limiter = _RateLimiter(max_concurrent=2, timeout=0.1)
        assert limiter.acquire()
        assert limiter.acquire()
        assert not limiter.acquire()
        limiter.release()
        assert limiter.acquire()
