"""
unit/infra/test_auto_switch.py — 自动切换 & 端口绑定重试测试

测试对象: inferfabric.proxy_manager.AUTO_SWITCH, proxy.handler._create_server, manager.switch
覆盖范围:
  - EDGE_AUTO_SWITCH 环境变量默认关闭、显式开启/关闭
  - _create_server() EADDRINUSE 重试（有界退避）
  - 非 EADDRINUSE 错误立即抛出
  - switch() 端口占用守卫（fuser 交叉检查，防止 stale idle 状态）
"""

import errno
import importlib
import os
import sys
import time as time_mod
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


# ═══════════════════════════════════════════════════════════════
# 1. AUTO_SWITCH default
# ═══════════════════════════════════════════════════════════════

def test_auto_switch_default_off(monkeypatch):
    """Without EDGE_AUTO_SWITCH, auto-switch is disabled."""
    import inferfabric.proxy_manager as pm
    monkeypatch.delenv("EDGE_AUTO_SWITCH", raising=False)
    importlib.reload(pm)
    try:
        assert pm.AUTO_SWITCH is False
    finally:
        importlib.reload(pm)


def test_auto_switch_env_on(monkeypatch):
    """EDGE_AUTO_SWITCH=1 enables auto-switching."""
    import inferfabric.proxy_manager as pm
    monkeypatch.setenv("EDGE_AUTO_SWITCH", "1")
    importlib.reload(pm)
    try:
        assert pm.AUTO_SWITCH is True
    finally:
        importlib.reload(pm)


def test_auto_switch_env_explicit_off(monkeypatch):
    """EDGE_AUTO_SWITCH=0 keeps it disabled."""
    import inferfabric.proxy_manager as pm
    monkeypatch.setenv("EDGE_AUTO_SWITCH", "0")
    importlib.reload(pm)
    try:
        assert pm.AUTO_SWITCH is False
    finally:
        importlib.reload(pm)


# ═══════════════════════════════════════════════════════════════
# 2. Bind retry (crash-loop fix)
# ═══════════════════════════════════════════════════════════════

def test_create_server_retries_on_eaddrinuse(monkeypatch):
    """EADDRINUSE is retried with backoff, not fatal on first hit."""
    import inferfabric.proxy.handler as handler

    calls = {"n": 0}

    class _FakeServer:
        pass

    def _flaky_server(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            e = OSError("Address already in use")
            e.errno = errno.EADDRINUSE
            raise e
        return _FakeServer()

    class _FakeTime:
        def __init__(self):
            self.slept = []
        def sleep(self, s):
            self.slept.append(s)

    fake_time = _FakeTime()
    monkeypatch.setattr(handler, "ThreadedHTTPServer", _flaky_server)
    monkeypatch.setattr(handler, "time", fake_time)

    server = handler._create_server(retries=5, retry_delay=2.0)
    assert calls["n"] == 3  # two failures, third attempt succeeds
    assert fake_time.slept == [2.0, 2.0]  # backoff waited between retries
    assert isinstance(server, _FakeServer)


def test_create_server_gives_up_after_retries(monkeypatch):
    """After `retries` failures the OSError propagates (systemd restarts)."""
    import inferfabric.proxy.handler as handler

    def _always_in_use(*a, **k):
        e = OSError("Address already in use")
        e.errno = errno.EADDRINUSE
        raise e

    class _FakeTime:
        def __init__(self):
            self.slept = []
        def sleep(self, s):
            self.slept.append(s)

    fake_time = _FakeTime()
    monkeypatch.setattr(handler, "ThreadedHTTPServer", _always_in_use)
    monkeypatch.setattr(handler, "time", fake_time)

    import pytest
    with pytest.raises(OSError) as excinfo:
        handler._create_server(retries=3, retry_delay=1.0)
    assert excinfo.value.errno == errno.EADDRINUSE
    assert len(fake_time.slept) == 2  # retried retries-1 times


def test_create_server_non_eaddrinuse_immediate_raise(monkeypatch):
    """Non-EADDRINUSE bind errors are not retried."""
    import inferfabric.proxy.handler as handler

    calls = {"n": 0}

    def _perm_denied(*a, **k):
        calls["n"] += 1
        e = OSError("Permission denied")
        e.errno = errno.EACCES
        raise e

    monkeypatch.setattr(handler, "ThreadedHTTPServer", _perm_denied)

    import pytest
    with pytest.raises(OSError) as excinfo:
        handler._create_server(retries=5, retry_delay=1.0)
    assert excinfo.value.errno == errno.EACCES
    assert calls["n"] == 1  # no retry for non-EADDRINUSE


# ═══════════════════════════════════════════════════════════════
# 3. Occupancy guard port cross-check
# ═══════════════════════════════════════════════════════════════

def _make_mgr(monkeypatch):
    import inferfabric.manager as manager_mod
    from inferfabric.state import GPUMode

    mgr = MagicMock()
    qwen = MagicMock()
    qwen.name = "qwen38-27b-abliterated"
    qwen.gpu_role = "exclusive"
    qwen.is_gpu_none = False
    gemma = MagicMock()
    gemma.name = "gemma4-31b-vl"
    gemma.gpu_role = "exclusive"
    gemma.is_gpu_none = False
    mgr._models = {
        "qwen38-27b-abliterated": qwen,
        "gemma4-31b-vl": gemma,
    }
    mgr.gpu_mode = GPUMode.IDLE
    mgr.active_services = ["bge-m3"]
    mgr.state.get.return_value = ""  # switching_target
    mgr.state.get_active_services.return_value = ["bge-m3"]
    # Health scan comes up empty (false negative) but fuser sees the port owner
    mgr._gpu_state._scan_actual_services.return_value = []
    mgr._gpu_state._scan_port_owners.return_value = {"gemma4-31b-vl": (8005, 1782116)}
    # _deploy_model returns a real dict so switch() can read result["status"]
    mgr._lifecycle._deploy_model.return_value = {
        "status": "switched", "model": "qwen38-27b-abliterated"
    }
    # gpu_used_mb is a module-level function in manager.py (imported from .health),
    # so patch the module attribute (not a ModelManager class attribute).
    monkeypatch.setattr(manager_mod, "gpu_used_mb", lambda: 25074)
    return mgr


def test_occupancy_guard_blocks_on_port_owner(monkeypatch):
    """DB says idle, but gemma4's port is owned → switch must be blocked."""
    from inferfabric.manager import ModelManager
    mgr = _make_mgr(monkeypatch)

    result = ModelManager.switch(mgr, "qwen38-27b-abliterated")
    assert result["status"] == "error"
    assert "gemma4-31b-vl" in result["message"]
    assert "8005" in result["message"]
    # deploy path must NOT have been taken
    mgr._lifecycle._deploy_model.assert_not_called()
    mgr._lifecycle._switch_exclusive.assert_not_called()


def test_switch_proceeds_when_port_free(monkeypatch):
    """No port owner + idle DB → normal deploy proceeds."""
    from inferfabric.manager import ModelManager
    mgr = _make_mgr(monkeypatch)
    mgr._gpu_state._scan_port_owners.return_value = {}

    result = ModelManager.switch(mgr, "qwen38-27b-abliterated")
    assert result["status"] == "switched"
    mgr._lifecycle._deploy_model.assert_called_once()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
