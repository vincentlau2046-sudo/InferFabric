"""unit/proxy/test_agent_capture.py — 请求入口 agent 采集接线测试。

走真实生产入口（handler._handle_messages / chat_handlers.handle_chat 的 401
分支）：enabled auth → check False → 记 RequestLog 并返回。断言：
  1. 入口 classify 一次并缓存到 handler._agent_hit
  2. 401 站点的 RequestLog 携带 agent/ua
自述性弱测试（复述 classify 语句、断言已有原语）不能证实「接线」，
故直接驱动生产调用链。RED→GREEN 真实成立。
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

from inferfabric.agent_registry import AgentRegistry, request_protocol
from inferfabric.proxy.handler import ProxyHandler
from inferfabric.proxy import chat_handlers


def _reg(tmp_path):
    b = tmp_path / "builtin"; b.mkdir()
    (b / "cc.yaml").write_text("""
id: claude-code
name: Claude Code
color: "#d97757"
match:
  - { header: x-app, value: cli }
""", encoding="utf-8")
    return AgentRegistry(b, tmp_path / "user")


def test_messages_entry_classifies_and_logs_agent(tmp_path):
    """真实 _handle_messages：401 分支 → _agent_hit 缓存 + RequestLog 带 agent。"""
    reg = _reg(tmp_path)
    logged = []
    pm = SimpleNamespace(
        new_request_id=lambda: "rid1",
        auth=SimpleNamespace(enabled=True,
                             key_name=lambda h: "primary",
                             check=lambda h, m: (False, "bad key")),
        logger=SimpleNamespace(log=lambda e: logged.append(e)),
        agent_registry=reg,
        _runtime_config={},
    )
    h = ProxyHandler.__new__(ProxyHandler)
    h.path = "/v1/messages"
    h.command = "POST"
    h.headers = {"X-App": "cli"}
    h._read_body = lambda: {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    h._send_json = lambda data, code=200: None
    h._handle_messages(pm)

    assert h._agent_hit.agent == "claude-code"
    assert logged and logged[0].agent == "claude-code"
    assert logged[0].status == 401


def test_chat_entry_classifies_and_logs_agent(tmp_path):
    """真实 handle_chat：401 分支 → handler._agent_hit + RequestLog 带 agent。"""
    reg = _reg(tmp_path)
    logged = []
    pm = SimpleNamespace(
        new_request_id=lambda: "rid2",
        auth=SimpleNamespace(enabled=True,
                             key_name=lambda h: "primary",
                             check=lambda h, m: (False, "bad key")),
        logger=SimpleNamespace(log=lambda e: logged.append(e)),
        agent_registry=reg,
    )
    h = ProxyHandler.__new__(ProxyHandler)
    h.path = "/v1/chat/completions"
    h.command = "POST"
    h.headers = {"X-App": "cli"}
    h._send_json = lambda data, code=200: None
    chat_handlers.handle_chat(h, pm, {"model": "qwen38"})

    assert h._agent_hit.agent == "claude-code"
    assert logged and logged[0].agent == "claude-code"
    assert logged[0].status == 401


def test_request_protocol_known_paths():
    assert request_protocol("/v1/messages") == "anthropic"
    assert request_protocol("/v1/chat/completions") == "openai"


def test_reloader_reloads_registry():
    import inferfabric.config_reloader as cr
    from inferfabric.agent_registry import AgentRegistry
    r = AgentRegistry.__new__(AgentRegistry)
    calls = []
    r.reload = lambda: (calls.append(1), True)[1]
    rel = cr.ConfigReloader(mgr=None, auth=None, cloud=None, registry=r)
    rel._last_reload = 0  # 跳过 5s 冷却
    failed = rel.reload_all()
    # reload_all 会先跑 mgr.reload_models（mgr=None → N/A），registry 必被调
    assert calls  # registry.reload 至少被调用一次


# ── Final-fix regression: 两条服务路径共用 ConfigReloader 装配（防再分叉） ──

def test_build_config_reloader_wires_registry():
    """装配 helper 必须接手 ProxyManager 并把 agent_registry 穿给 ConfigReloader。"""
    from types import SimpleNamespace
    from inferfabric.config_reloader import build_config_reloader
    mgr = SimpleNamespace(mgr="MM", auth="A", cloud="C", agent_registry="REG")
    rel = build_config_reloader(mgr, auth=mgr.auth, cloud=mgr.cloud)
    assert rel._registry == "REG"
    assert rel._mgr == "MM"
    assert rel._auth == "A"
    assert rel._cloud == "C"


def test_async_server_uses_build_config_reloader():
    """生产 async 路径必须走装配 helper——否则 SIGHUP//reload-config 的 agents 热重载静默失效。"""
    from pathlib import Path
    import inferfabric.proxy.async_server as asrv
    src = Path(asrv.__file__).read_text(encoding="utf-8")
    assert "build_config_reloader" in src
    assert "ConfigReloader(mgr.mgr" not in src
