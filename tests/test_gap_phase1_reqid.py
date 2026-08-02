"""tests/test_gap_phase1_reqid.py — D-1: req_id thread safety + collision fix"""

import threading
import itertools
import uuid

import pytest

import inferfabric.proxy  # noqa: F401 — resolve circular import
from inferfabric.proxy_manager import ProxyManager


class TestReqId:
    """Verify req_id generation is thread-safe and collision-free."""

    def test_format(self):
        """req_id matches {8-hex-counter}-{8-hex-uuid} format."""
        pm = ProxyManager.__new__(ProxyManager)
        pm._req_counter = itertools.count()
        rid = pm.new_request_id()
        parts = rid.split("-")
        assert len(parts) == 2, f"Expected 2 parts separated by '-', got {rid!r}"
        try:
            int(parts[0], 16)
            assert len(parts[0]) == 8, f"Counter part should be 8 hex chars, got {parts[0]!r}"
        except ValueError:
            pytest.fail(f"Counter part not valid hex: {parts[0]!r}")
        try:
            int(parts[1], 16)
            assert len(parts[1]) == 8, f"UUID part should be 8 hex chars, got {parts[1]!r}"
        except ValueError:
            pytest.fail(f"UUID part not valid hex: {parts[1]!r}")

    def test_counter_increments(self):
        """Sequential calls produce monotonically increasing counter."""
        pm = ProxyManager.__new__(ProxyManager)
        pm._req_counter = itertools.count()
        ids = [pm.new_request_id() for _ in range(10)]
        counters = [int(r.split("-")[0], 16) for r in ids]
        for i in range(1, len(counters)):
            assert counters[i] > counters[i - 1], \
                f"Counter should monotonically increase: {counters}"

    def test_thread_safety_no_collisions(self):
        """100 threads concurrently generate req_ids — zero collisions."""
        pm = ProxyManager.__new__(ProxyManager)
        pm._req_counter = itertools.count()
        results = set()
        lock = threading.Lock()

        def generate(n):
            for _ in range(n):
                rid = pm.new_request_id()
                with lock:
                    results.add(rid)

        threads = []
        for _ in range(100):
            t = threading.Thread(target=generate, args=(100,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        expected = 100 * 100  # 10,000
        assert len(results) == expected, \
            f"Expected {expected} unique IDs, got {len(results)} — {expected - len(results)} collisions"


class TestReqIdNoConflict:
    """Ensure the new req_id format does not clash with usage in handler.py."""

    def test_contains_hyphen(self):
        pm = ProxyManager.__new__(ProxyManager)
        pm._req_counter = itertools.count()
        rid = pm.new_request_id()
        assert "-" in rid, f"Expected '-' in req_id, got {rid!r}"

    def test_length(self):
        pm = ProxyManager.__new__(ProxyManager)
        pm._req_counter = itertools.count()
        rid = pm.new_request_id()
        # 8 hex + '-' + 8 hex = 17 chars
        assert len(rid) == 17, f"Expected 17 chars, got {len(rid)}: {rid!r}"
