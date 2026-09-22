import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))
import inferfabric.proxy.handler as handler_module
from inferfabric.proxy.handler import ProxyHandler


@pytest.fixture
def fresh_cache(monkeypatch):
    """每个测试用全新的 per-window 缓存：模块级 _lat_series_cache 会跨测试串缓存
    （否则 fallback/透传测试会被前一测试的 24h 缓存掩盖，实际路径未执行）。"""
    caches = {w: handler_module._ExpensiveCache(ttl=handler_module._LAT_CACHE_TTL[w])
              for w in handler_module._LAT_CACHE_TTL}
    monkeypatch.setattr(handler_module, "_lat_series_cache", caches)
    return caches


def _fake_series(window, bucket_ms=3600000, top_n=5, percentiles=(0.5, 0.95),
                 source_of=None):
    src = (source_of or {}).get("m1", "observed")
    return {"window": window, "bucket_ms": bucket_ms, "buckets": ["a", "b"],
            "percentiles": ["p50", "p95"],
            "series": {"m1": {"source": src, "requests": 3,
                               "ttft_p50": [1.0, 2.0], "ttft_p95": [1.0, 2.0],
                               "ttft_n": [2, 2], "tpot_p50": [3.0, 4.0],
                               "tpot_p95": [3.0, 4.0], "tpot_n": [2, 2]}},
            "total_models": 1}


def _mk():
    pm = SimpleNamespace()
    pm.metrics = SimpleNamespace(get_latency_series=_fake_series)
    pm.mgr = SimpleNamespace(_models={"m1": SimpleNamespace(name="m1")})
    pm.cloud = SimpleNamespace(cloud_models={})
    calls = []
    pm.metrics.get_latency_series = lambda *a, **k: (calls.append((a, k)), _fake_series(*a, **k))[1]
    pm._calls = calls
    return pm


def _handler(path):
    h = ProxyHandler.__new__(ProxyHandler)
    h.path = path
    h.command = "GET"
    h._sent = []
    h._send_json = lambda data, code=200: h._sent.append((code, data))
    return h


def test_latency_default_24h(fresh_cache):
    pm = _mk()
    h = _handler("/api/latency")
    h._handle_api_latency(pm)
    code, data = h._sent[-1]
    assert code == 200
    assert data["window"] == "24h"
    # source 由 _metrics_axis → source_of 派生：handler 丢 source_of 时 fake 返回
    # "observed" → 本断言失败（非空洞断言）。
    assert data["series"]["m1"]["source"] == "local"


def test_latency_bad_window_falls_back_24h(fresh_cache):
    pm = _mk()
    h = _handler("/api/latency?window=bogus")
    h._handle_api_latency(pm)
    data = h._sent[-1][1]
    assert data["window"] == "24h"
    # fallback 真实执行（非缓存掩盖）：底层调用收到归一后的 24h 参数
    a, k = pm._calls[-1]
    assert a[0] == "24h" and k["bucket_ms"] == 3600 * 1000 and k["top_n"] == 5


def test_latency_window_passthrough(fresh_cache):
    pm = _mk()
    h = _handler("/api/latency?window=7d")
    h._handle_api_latency(pm)
    assert h._sent[-1][1]["window"] == "7d"
    a, k = pm._calls[-1]
    assert a[0] == "7d" and k["bucket_ms"] == 6 * 3600 * 1000
    assert k["percentiles"] == (0.50, 0.95) and k["source_of"] == {"m1": "local"}
