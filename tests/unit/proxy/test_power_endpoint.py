"""GET /api/power?gran=hour|day|month — 功耗/电费分桶序列端点测试。

参照 test_latency_endpoint.py 范式：per-gran 缓存 fresh + SimpleNamespace 桩。
"""

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
    """每个测试用全新的 per-gran 缓存：模块级 _power_series_cache 会跨测试串缓存。"""
    caches = {g: handler_module._ExpensiveCache(ttl=handler_module._POWER_CACHE_TTL_GRANS)
              for g in handler_module._POWER_CACHE_TTL_GRANS}
    monkeypatch.setattr(handler_module, "_power_series_cache", caches)
    return caches


def _fake_series(gran):
    return {"gran": gran, "price_yuan_per_kwh": 1.0,
            "buckets": [{"t": 1000, "avg_w": 500.0, "kwh": 0.4,
                         "cum_kwh": 0.4, "cum_yuan": 0.4}],
            "totals": {"kwh": 0.4, "yuan": 0.4, "avg_w": 500.0, "count": 1},
            "available": True}


def _mk():
    pm = SimpleNamespace()
    pm.telemetry = SimpleNamespace(get_power_series=_fake_series)
    calls = []
    pm.telemetry.get_power_series = lambda g: (calls.append(g), _fake_series(g))[1]
    pm._calls = calls
    return pm


def _handler(path):
    h = ProxyHandler.__new__(ProxyHandler)
    h.path = path
    h.command = "GET"
    h._sent = []
    h._send_json = lambda data, code=200: h._sent.append((code, data))
    return h


def test_power_default_hour(fresh_cache):
    pm = _mk()
    h = _handler("/api/power")
    h._handle_api_power(pm)
    code, data = h._sent[-1]
    assert code == 200
    assert data["gran"] == "hour"
    # 底层调用收到归一后的 gran 参数（非缓存掩盖）
    assert pm._calls[-1] == "hour"


def test_power_gran_passthrough(fresh_cache):
    pm = _mk()
    h = _handler("/api/power?gran=day")
    h._handle_api_power(pm)
    assert h._sent[-1][1]["gran"] == "day"
    assert pm._calls[-1] == "day"


def test_power_bad_gran_falls_back_hour(fresh_cache):
    pm = _mk()
    h = _handler("/api/power?gran=bogus")
    h._handle_api_power(pm)
    assert h._sent[-1][1]["gran"] == "hour"
    assert pm._calls[-1] == "hour"


def test_power_telemetry_error_500(fresh_cache):
    pm = _mk()
    pm.telemetry.get_power_series = lambda g: (_ for _ in ()).throw(RuntimeError("boom"))
    h = _handler("/api/power")
    h._handle_api_power(pm)
    assert h._sent[-1][0] == 500