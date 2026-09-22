"""
unit/proxy/test_pipeline_guard.py — embeddings/rerank 接入安全管道（A1/A7 修复）

A1: /v1/embeddings、/v1/rerank 绕过 auth / rate-limit / RequestLog /
    switch guard / AUTO_SWITCH 开关。
A7: 两接口直接 pm.mgr.switch() 拉起模型，无视 EDGE_AUTO_SWITCH=0，
    可能杀掉独占模型。

修复：提取共享 ProxyHandler._pipeline_guard()，按 chat 路径同款顺序
施加 auth(401) → switch guard(503) → model-type/port 校验 → AUTO_SWITCH
(503 when off & 模型未激活) → dual_gate(429)，并对每个阻断结果写
RequestLog（R1）。放行时返回 gate + 请求上下文供调用方转发与终态记录。
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import inferfabric.proxy.handler as handler_module
from inferfabric.state import ServiceState


# ─── 桩构造 ──────────────────────────────────────────────────


def _make_pm(*, active=None, profile_state="", switching_target="",
             model_type="embedding", port=8199, auth_enabled=False,
             auth_ok=True, rate_ok=True, rate_reason=None,
             auto_switch=True):
    """构造 guard 所需的最小 pm 桩。"""
    active = active if active is not None else set()
    model_obj = SimpleNamespace(name="emb", model_type=model_type, port=port)

    logs = []

    pm = SimpleNamespace()
    pm.auto_switch = auto_switch  # R-AS: guard 读实例态（UI 开关即时翻转）
    pm.auth = SimpleNamespace(
        enabled=auth_enabled,
        check=lambda hdr, m: (auth_ok, "ok" if auth_ok else "invalid key"),
        key_name=lambda hdr: "keyA" if auth_enabled else "anonymous",
    )
    pm.model_to_service = lambda m: "emb"
    pm.mgr = SimpleNamespace(
        get_model=lambda svc: model_obj,
        active_services=active,
        state={"profile_state": profile_state, "switching_target": switching_target},
    )

    gate = SimpleNamespace(ok=rate_ok, reason=rate_reason, release=MagicMock())
    acquire_calls = []

    def acquire(m, timeout=None):
        acquire_calls.append((m, timeout))
        return gate

    pm.dual_gate = SimpleNamespace(acquire=acquire)
    pm.new_request_id = lambda: "req-1"
    pm.logger = SimpleNamespace(log=MagicMock(side_effect=lambda *a, **k: logs.append((a, k))))
    pm.anomalies = SimpleNamespace(record=MagicMock())
    pm._logs = logs
    pm._gate = gate
    pm._acquire_calls = acquire_calls
    return pm


def _make_handler(headers=None):
    h = handler_module.ProxyHandler.__new__(handler_module.ProxyHandler)
    h.headers = headers or {}
    h.path = "/v1/embeddings"
    h.command = "POST"
    return h


def _blocked_status(guard):
    assert guard["blocked"], f"expected blocked, got pass: {guard}"
    return guard["status"]


def _logged_statuses(pm):
    """guard 经 pm.logger.log(RequestLog) 记录的状态列表（单位置参数）。"""
    out = []
    for args, _kw in pm._logs:
        for a in args:
            if hasattr(a, "status"):
                out.append(a.status)
    return out


# ═══════════════════════════════════════════════════════════════
# 阻断分支
# ═══════════════════════════════════════════════════════════════


class TestPipelineGuardBlocked:
    def test_unknown_model_404(self, monkeypatch):
        pm = _make_pm()
        pm.model_to_service = lambda m: None
        h = _make_handler()
        guard = h._pipeline_guard(pm, "ghost", {"model": "ghost"}, "embedding")
        assert _blocked_status(guard) == 404
        # A1: 阻断结果写 RequestLog
        assert _logged_statuses(pm) == [404]

    def test_auth_fail_401(self, monkeypatch):
        pm = _make_pm(active={"emb"}, auth_enabled=True, auth_ok=False)
        h = _make_handler({"Authorization": "Bearer bad"})
        guard = h._pipeline_guard(pm, "emb", {"model": "emb"}, "embedding")
        assert _blocked_status(guard) == 401
        assert _logged_statuses(pm) == [401]

    def test_switching_not_target_503(self, monkeypatch):
        pm = _make_pm(active={"emb"}, profile_state=ServiceState.SWITCHING,
                      switching_target="other")
        h = _make_handler()
        guard = h._pipeline_guard(pm, "emb", {"model": "emb"}, "embedding")
        assert _blocked_status(guard) == 503
        assert guard["headers"].get("Retry-After") == "30"

    def test_model_type_mismatch_400(self, monkeypatch):
        pm = _make_pm(active={"emb"}, model_type="llm")
        h = _make_handler()
        guard = h._pipeline_guard(pm, "emb", {"model": "emb"}, "embedding")
        assert _blocked_status(guard) == 400

    def test_no_port_500(self, monkeypatch):
        pm = _make_pm(active={"emb"}, port=None)
        h = _make_handler()
        guard = h._pipeline_guard(pm, "emb", {"model": "emb"}, "embedding")
        assert _blocked_status(guard) == 500


# ═══════════════════════════════════════════════════════════════
# A7: AUTO_SWITCH 关闭时不再强行拉起未激活模型
# ═══════════════════════════════════════════════════════════════


class TestAutoSwitchRespect:
    def test_autoswitch_off_inactive_503(self):
        """模型未激活且 AUTO_SWITCH=off → 503，不 switch（修复 A7）。"""
        pm = _make_pm(active=set(), auto_switch=False)  # emb 未激活
        h = _make_handler()
        guard = h._pipeline_guard(pm, "emb", {"model": "emb"}, "embedding")
        assert _blocked_status(guard) == 503
        assert guard["body"].get("status") == "not_active"

    def test_autoswitch_on_inactive_proceeds(self):
        """模型未激活但 AUTO_SWITCH=on → 放行（调用方负责拉起）。"""
        pm = _make_pm(active=set(), auto_switch=True)
        h = _make_handler()
        guard = h._pipeline_guard(pm, "emb", {"model": "emb"}, "embedding")
        assert not guard["blocked"]
        assert guard["ctx"]["svc_name"] == "emb"


# ═══════════════════════════════════════════════════════════════
# 限流 & 放行
# ═══════════════════════════════════════════════════════════════


class TestGateAndPass:
    def test_rate_limited_429(self, monkeypatch):
        pm = _make_pm(active={"emb"}, rate_ok=False, rate_reason="concurrency_limit")
        h = _make_handler()
        guard = h._pipeline_guard(pm, "emb", {"model": "emb"}, "embedding")
        assert _blocked_status(guard) == 429
        assert "concurrency_limit" in guard["body"]["error"]

    def test_pass_returns_ctx_and_gate(self):
        pm = _make_pm(active={"emb"}, auto_switch=True)
        h = _make_handler({"Authorization": "Bearer good"})
        guard = h._pipeline_guard(pm, "emb", {"model": "emb"}, "embedding")
        assert not guard["blocked"]
        ctx = guard["ctx"]
        assert ctx["svc_name"] == "emb"
        assert ctx["port"] == 8199
        assert guard["gate"] is pm._gate
        # 放行时 guard 不写阻断日志（终态由调用方记录）
        assert len(pm._logs) == 0
        # 限流确实被获取
        assert pm._acquire_calls == [("emb", 30)]


# ═══════════════════════════════════════════════════════════════
# 集成：_handle_embeddings / _handle_rerank 走 guard
# ═══════════════════════════════════════════════════════════════


class TestHandlersUseGuard:
    def test_embeddings_blocked_returns_404(self, monkeypatch):
        """未知模型 → guard 阻断 404，不再裸转发。"""
        pm = _make_pm()
        pm.model_to_service = lambda m: None
        h = _make_handler()
        h._send_json = MagicMock()
        h._read_body = MagicMock(return_value={"model": "ghost"})
        h._pipeline_guard = h._pipeline_guard  # real guard
        h._handle_embeddings(pm)
        h._send_json.assert_called_once()
        args = h._send_json.call_args[0]
        assert args[1] == 404  # status 位置参数

    def test_embeddings_autoswitch_off_503(self):
        """AUTO_SWITCH=off 且未激活 → 503，且不再调用 pm.mgr.switch。"""
        pm = _make_pm(active=set(), auto_switch=False)
        switch_calls = []
        pm.mgr.switch = lambda svc: switch_calls.append(svc) or {"status": "switched"}
        h = _make_handler()
        h._send_json = MagicMock()
        h._read_body = MagicMock(return_value={"model": "emb"})
        h._handle_embeddings(pm)
        h._send_json.assert_called_once()
        assert h._send_json.call_args[0][1] == 503
        assert switch_calls == [], "AUTO_SWITCH=off must NOT trigger pm.mgr.switch (A7)"
