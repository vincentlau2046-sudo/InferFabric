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
