"""unit/proxy/test_agent_endpoints.py — /api/agent-* 端点测试（SimpleNamespace 桩）。"""
from types import SimpleNamespace
from inferfabric.proxy.handler import ProxyHandler


def _handler(path):
    h = ProxyHandler.__new__(ProxyHandler)
    h.path = path
    h.command = "GET"
    h._sent = []
    h._send_json = lambda data, code=200: h._sent.append((code, data))
    return h


def test_agent_stats_invalid_gran_400():
    h = _handler("/api/agent-stats?granularity=bogus")
    pm = SimpleNamespace()
    h._handle_agent_stats(pm)
    assert h._sent[-1][0] == 400
