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

def _mk_pm(tmp_path):
    from inferfabric.agent_registry import AgentRegistry
    reg = AgentRegistry(tmp_path / "builtin", tmp_path / "user")
    pm = SimpleNamespace(agent_registry=reg,
                         telemetry=SimpleNamespace(query_request_log=lambda since, limit=1: []),
                         metrics=SimpleNamespace(price_config={}))
    return pm


def test_agents_get_lists(tmp_path):
    pm = _mk_pm(tmp_path)
    h = _handler("/api/agents")
    h._handle_agents(pm)
    code, data = h._sent[-1]
    assert code == 200 and data == {"agents": []}


def test_agents_post_creates(tmp_path):
    pm = _mk_pm(tmp_path)
    h = _handler("/api/agents")
    import json
    h._read_body = lambda: {"id": "curl-cli", "name": "curl-cli",
                            "header": "user-agent", "pattern": "^curl", "color": "#94a3b8"}
    h._handle_post_agents(pm)
    code, data = h._sent[-1]
    assert code == 200 and data["agent"]["id"] == "curl-cli"
    assert h._sent[-1][0] == 200
    # 已落盘并热重载 → classify 命中
    assert pm.agent_registry.classify("openai", {"User-Agent": "curl/8"}).agent == "curl-cli"


def test_agents_post_duplicate_409(tmp_path):
    pm = _mk_pm(tmp_path)
    pm.agent_registry.add_from_ui("curl-cli", "curl-cli", "user-agent", "^curl", "#000")
    h = _handler("/api/agents")
    h._read_body = lambda: {"id": "curl-cli", "name": "x", "header": "user-agent", "pattern": "^x", "color": "#000"}
    h._handle_post_agents(pm)
    assert h._sent[-1][0] == 409


def test_agents_post_invalid_400(tmp_path):
    pm = _mk_pm(tmp_path)
    h = _handler("/api/agents")
    h._read_body = lambda: {"id": "Bad ID!", "name": "x", "header": "user-agent", "pattern": "^x", "color": "#000"}
    h._handle_post_agents(pm)
    assert h._sent[-1][0] == 400


def test_agents_delete_user_ok(tmp_path):
    pm = _mk_pm(tmp_path)
    pm.agent_registry.add_from_ui("curl-cli", "curl-cli", "user-agent", "^curl", "#000")
    h = _handler("/api/agents?id=curl-cli")
    h._handle_delete_agent(pm)
    assert h._sent[-1][0] == 200
    assert pm.agent_registry.classify("openai", {"User-Agent": "curl/8"}).agent == "unknown"


def test_agents_delete_builtin_400(tmp_path):
    from inferfabric.agent_registry import AgentRegistry
    b = tmp_path / "builtin"; b.mkdir()
    (b / "cc.yaml").write_text("id: claude-code\nname: CC\ncolor: '#000'\nmatch:\n  - { header: x-app, value: cli }\n", encoding="utf-8")
    pm = SimpleNamespace(agent_registry=AgentRegistry(b, tmp_path / "user"))
    h = _handler("/api/agents?id=claude-code")
    h._handle_delete_agent(pm)
    assert h._sent[-1][0] == 400
