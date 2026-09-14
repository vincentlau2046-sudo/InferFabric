"""
unit/proxy/test_r0_switch_cooldown.py — R0: ensure_service cooldown 修复（无限 503 风暴根因）

测试对象:
  - inferfabric.proxy_manager.ProxyManager.ensure_service
  - inferfabric.proxy.handler.ProxyHandler._handle_messages (Anthropic /v1/messages)
  - inferfabric.proxy.chat_handlers.handle_chat (OpenAI /v1/chat/completions)

覆盖范围:
  - 失败的 switch 也会激活 10s cooldown（根因修复）
  - cooldown 窗口内第二次调用直接返回 False，不再触发 switch（风暴根治）
  - 失败的 switch 会清空 switching_target
  - 成功的 switch 同样激活 cooldown（回归保护）
  - 手动停止的目标被阻止，不触发 switch
  - cooldown 过期后允许再次尝试（有界，非永久锁死）
  - 503 响应携带 Retry-After: 10 头（两个协议路径）
"""

import itertools
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ═══════════════════════════════════════════════════════════════
# Fixtures / helpers
# ═══════════════════════════════════════════════════════════════


def make_pm(switch_status="error"):
    """构造一个轻量 ProxyManager（绕过 __init__ 的重依赖）。"""
    from inferfabric.proxy_manager import ProxyManager

    pm = ProxyManager.__new__(ProxyManager)
    pm.mgr = MagicMock()
    pm._last_switch = 0.0
    pm._cooldown = 10
    pm._switch_lock = threading.Lock()
    pm._req_counter = itertools.count()
    pm.mgr.active_services = []
    pm.mgr._models = {}
    pm.mgr.state.get.return_value = ""
    pm.mgr.state.is_manually_stopped.return_value = False
    pm.mgr.switch.return_value = {"status": switch_status, "message": "deploy failed"}
    pm._wait_healthy = MagicMock(return_value=True)
    pm.anomalies = MagicMock()  # R9: silence AnomalyEvent recording in handler
    return pm


class _WFile:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def flush(self):
        pass


class _RFile:
    def __init__(self, raw):
        self._raw = raw

    def read(self, n):
        return self._raw[:n]


class FakeHandler:
    """最小化的 HTTP handler 替身，记录 status/headers/wfile 输出。"""

    def __init__(self, body=None, extra_request_headers=None):
        raw = json.dumps(body or {}).encode("utf-8")
        self.rfile = _RFile(raw)
        self.wfile = _WFile()
        self.headers = {"Content-Length": str(len(raw)), "Authorization": ""}
        if extra_request_headers:
            self.headers.update(extra_request_headers)
        self.status = None
        self.sent_headers = {}
        self.headers_ended = False
        self._req_id = None
        self._req_start = 0.0
        self._key_name = "anonymous"
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.sent_headers[k] = str(v)

    def end_headers(self):
        self.headers_ended = True

    def _read_body(self):
        from inferfabric import forwarder
        return forwarder.read_body(self)

    def _send_json(self, data, status=200, extra_headers=None):
        from inferfabric import forwarder
        forwarder.send_json(self, data, status, extra_headers=extra_headers)

    def _safe_write(self, data):
        self.wfile.write(data)
        self.wfile.flush()


# ═══════════════════════════════════════════════════════════════
# 1. ensure_service cooldown 行为（根因修复）
# ═══════════════════════════════════════════════════════════════


def test_failed_switch_arms_cooldown():
    """失败的 switch 必须激活 cooldown（R0 根因修复点）。"""
    pm = make_pm(switch_status="error")
    assert pm.ensure_service("qwen38-27b-abliterated") is False
    assert pm._last_switch > 0, "failed switch must arm the 10s cooldown"
    # 失败后 switching_target 必须被清空
    pm.mgr.state.set.assert_any_call("switching_target", "")


def test_failed_switch_no_storm():
    """根因回归：cooldown 窗口内再次 ensure_service 应跳过 switch（不再风暴）。"""
    pm = make_pm(switch_status="error")
    assert pm.ensure_service("qwen38-27b-abliterated") is False
    assert pm.mgr.switch.call_count == 1
    # 立即第二次调用：处于 cooldown 内 → 直接 False，不重复触发 switch
    assert pm.ensure_service("qwen38-27b-abliterated") is False
    assert pm.mgr.switch.call_count == 1, "cooldown skip: no second switch attempt (storm root fixed)"


def test_failed_switch_clears_switching_target():
    """失败路径清空 switching_target，避免状态泄漏阻塞后续请求。"""
    pm = make_pm(switch_status="error")
    pm.ensure_service("qwen38-27b-abliterated")
    sets = [c.args for c in pm.mgr.state.set.call_args_list]
    assert ("switching_target", "") in sets


def test_successful_switch_arms_cooldown():
    """回归保护：成功的 switch 同样激活 cooldown（原有行为）。"""
    pm = make_pm(switch_status="switched")
    assert pm.ensure_service("qwen38-27b-abliterated") is True
    assert pm._last_switch > 0


def test_manually_stopped_blocks_switch():
    """用户手动停止的目标：ensure_service 返回 False 且不触发 switch。"""
    pm = make_pm()
    pm.mgr.state.is_manually_stopped.return_value = True
    assert pm.ensure_service("qwen38-27b-abliterated") is False
    pm.mgr.switch.assert_not_called()


def test_cooldown_expiry_retries_switch():
    """cooldown 是有界的：10s 后允许再次尝试（不是永久锁死）。"""
    pm = make_pm(switch_status="error")
    assert pm.ensure_service("qwen38-27b-abliterated") is False
    assert pm.mgr.switch.call_count == 1
    # 模拟 cooldown 已过期
    pm._last_switch = time.time() - (pm._cooldown + 1)
    assert pm.ensure_service("qwen38-27b-abliterated") is False
    assert pm.mgr.switch.call_count == 2, "after cooldown expiry the switch is retried"


# ═══════════════════════════════════════════════════════════════
# 2. 503 + Retry-After: 10 头（Agent 退避依据）
# ═══════════════════════════════════════════════════════════════


def test_send_json_emits_retry_after_header():
    """forwarder.send_json 的 extra_headers 机制：Retry-After 头确实发出。"""
    import inferfabric.forwarder as forwarder

    h = FakeHandler()
    forwarder.send_json(
        h,
        {"error": "Auto-switch failed, retry later", "status": "switch_failed", "retry_after": 10},
        503,
        extra_headers={"Retry-After": "10"},
    )
    assert h.status == 503
    assert h.sent_headers.get("Retry-After") == "10"
    assert h.headers_ended is True
    body = json.loads(h.wfile.data)
    assert body["status"] == "switch_failed"
    assert body["retry_after"] == 10


def test_messages_failed_switch_503_retry_after(monkeypatch):
    """Anthropic /v1/messages：auto-switch 失败 → 503 + Retry-After: 10。"""
    import inferfabric.proxy.handler as handler_mod
    import inferfabric.proxy_manager as pm_mod

    monkeypatch.setattr(pm_mod, "AUTO_SWITCH", True)

    pm = make_pm(switch_status="error")
    pm.auth = MagicMock()
    pm.auth.enabled = False
    pm.logger = MagicMock()
    model = MagicMock()
    model.name = "qwen38-27b-abliterated"
    model.port = 8002
    model.served_name = "deepseek-v4-flash"
    pm.mgr.find_model_by_served_name.return_value = model

    body = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    }
    h = FakeHandler(body)
    handler_mod.ProxyHandler._handle_messages(h, pm)
    assert h.status == 503
    assert h.sent_headers.get("Retry-After") == "10"
    resp_body = json.loads(h.wfile.data)
    assert resp_body["status"] == "switch_failed"
    assert resp_body["retry_after"] == 10


def test_chat_completions_failed_switch_503_retry_after(monkeypatch):
    """OpenAI /v1/chat/completions：auto-switch 失败 → 503 + Retry-After: 10。"""
    import inferfabric.proxy.chat_handlers as chat_handlers
    import inferfabric.proxy_manager as pm_mod

    monkeypatch.setattr(chat_handlers, "AUTO_SWITCH", True)

    pm = make_pm(switch_status="error")
    pm.auth = MagicMock()
    pm.auth.enabled = False
    pm.logger = MagicMock()
    pm.model_to_service = MagicMock(return_value="qwen38-27b-abliterated")
    pm.ensure_service = MagicMock(return_value=False)
    pm.mgr.state.is_manually_stopped.return_value = False

    body = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    h = FakeHandler(body)
    chat_handlers.handle_chat(h, pm, body)
    assert h.status == 503
    assert h.sent_headers.get("Retry-After") == "10"
    resp_body = json.loads(h.wfile.data)
    assert resp_body["status"] == "switch_blocked"
    assert resp_body["retry_after"] == 10


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
