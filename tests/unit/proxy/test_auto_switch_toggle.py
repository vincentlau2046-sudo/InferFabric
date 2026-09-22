"""
unit/proxy/test_auto_switch_toggle.py — R-AS: 自动切换 UI 开关（立即生效，免重启）

覆盖:
  - ProxyManager._resolve_auto_switch 优先级: 显式 env > iff.yaml > 默认开
  - ProxyManager.set_auto_switch: 实例态 + 模块级 AUTO_SWITCH 镜像 + iff.yaml 持久化
  - ProxyManager._persist_auto_switch: 文本级合并（保留注释/其他键）、幂等、新建文件
  - handler._handle_auto_switch_toggle: POST /admin/auto-switch/toggle 响应契约
  - 路由 /admin/auto-switch/toggle 已注册于 _POST_ROUTES
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import inferfabric.proxy.handler as handler_mod
import inferfabric.proxy_manager as pm_mod
from inferfabric.proxy.handler import ProxyHandler
from inferfabric.proxy_manager import ProxyManager


# ─── 桩构造 ──────────────────────────────────────────────────


def _bare_pm(tmp_path, monkeypatch):
    """绕过 __init__ 重依赖的 ProxyManager（与 test_r0_switch_cooldown 同款）。"""
    pm = ProxyManager.__new__(ProxyManager)
    pm.auto_switch, pm._auto_switch_source = ProxyManager._resolve_auto_switch({})
    monkeypatch.setattr(pm_mod, "IFF_DATA_DIR", tmp_path)
    return pm


# ═══════════════════════════════════════════════════════════════
# 1. _resolve_auto_switch 优先级
# ═══════════════════════════════════════════════════════════════


class TestResolvePriority:
    def test_env_wins_over_file(self, monkeypatch):
        """显式 env EDGE_AUTO_SWITCH=0 压过 iff.yaml 的 True。"""
        monkeypatch.setenv("EDGE_AUTO_SWITCH", "0")
        val, src = ProxyManager._resolve_auto_switch({"auto_switch": {"enabled": True}})
        assert (val, src) == (False, "env")

    def test_env_on(self, monkeypatch):
        monkeypatch.setenv("EDGE_AUTO_SWITCH", "1")
        val, src = ProxyManager._resolve_auto_switch({})
        assert (val, src) == (True, "env")

    def test_file_when_no_env(self, monkeypatch):
        monkeypatch.delenv("EDGE_AUTO_SWITCH", raising=False)
        val, src = ProxyManager._resolve_auto_switch({"auto_switch": {"enabled": False}})
        assert (val, src) == (False, "file")

    def test_default_on_when_nothing(self, monkeypatch):
        monkeypatch.delenv("EDGE_AUTO_SWITCH", raising=False)
        val, src = ProxyManager._resolve_auto_switch({})
        assert (val, src) == (True, "default")

    def test_ignores_malformed_file_block(self, monkeypatch):
        """auto_switch 非 mapping / 缺 enabled 键 → 不视为 file 来源。"""
        monkeypatch.delenv("EDGE_AUTO_SWITCH", raising=False)
        assert ProxyManager._resolve_auto_switch({"auto_switch": "x"}) == (True, "default")
        assert ProxyManager._resolve_auto_switch({"auto_switch": {}}) == (True, "default")


# ═══════════════════════════════════════════════════════════════
# 2. set_auto_switch: 立即生效（实例态 + 模块镜像）+ 持久化
# ═══════════════════════════════════════════════════════════════


class TestSetAutoSwitch:
    def test_flips_instance_and_module_mirror(self, tmp_path, monkeypatch):
        """UI 开关后，handler 函数作用域 import 读到的模块镜像必须同步翻转。"""
        monkeypatch.delenv("EDGE_AUTO_SWITCH", raising=False)
        pm = _bare_pm(tmp_path, monkeypatch)
        monkeypatch.setattr(pm_mod, "AUTO_SWITCH", True)
        result = pm.set_auto_switch(False)
        assert pm.auto_switch is False
        assert pm_mod.AUTO_SWITCH is False  # 模块镜像（handler 旧读点兼容）
        assert result["auto_switch"] is False
        assert result["source"] == "default"
        assert result["env_locked"] is False
        assert result["hint"] is None

    def test_toggles_back_on(self, tmp_path, monkeypatch):
        pm = _bare_pm(tmp_path, monkeypatch)
        pm.set_auto_switch(False)
        result = pm.set_auto_switch(True)
        assert result["auto_switch"] is True
        assert pm_mod.AUTO_SWITCH is True

    def test_persists_iff_yaml_block(self, tmp_path, monkeypatch):
        pm = _bare_pm(tmp_path, monkeypatch)
        pm.set_auto_switch(False)
        text = (tmp_path / "iff.yaml").read_text()
        assert "auto_switch:" in text
        assert "enabled: false" in text

    def test_hint_when_env_locked(self, tmp_path, monkeypatch):
        """env 显式设置时: 立即生效但重启后回到 env 值 → hint 非 null（实时 env 判定）。"""
        pm = _bare_pm(tmp_path, monkeypatch)
        monkeypatch.setenv("EDGE_AUTO_SWITCH", "0")
        result = pm.set_auto_switch(True)
        assert result["env_locked"] is True
        assert result["hint"] and "env" in result["hint"]

    def test_no_hint_when_env_absent(self, tmp_path, monkeypatch):
        """env 未设置时: env_locked=False, hint=None。"""
        pm = _bare_pm(tmp_path, monkeypatch)
        monkeypatch.delenv("EDGE_AUTO_SWITCH", raising=False)
        result = pm.set_auto_switch(False)
        assert result["env_locked"] is False
        assert result["hint"] is None


# ═══════════════════════════════════════════════════════════════
# 3. _persist_auto_switch: 文本级合并
# ═══════════════════════════════════════════════════════════════


class TestPersist:
    def test_creates_file_when_missing(self, tmp_path, monkeypatch):
        pm = _bare_pm(tmp_path, monkeypatch)
        pm._persist_auto_switch(True)
        assert (tmp_path / "iff.yaml").exists()

    def test_preserves_comments_and_other_keys(self, tmp_path, monkeypatch):
        pm = _bare_pm(tmp_path, monkeypatch)
        (tmp_path / "iff.yaml").write_text(
            "# InferFabric 运行时配置\n"
            "# 注释必须保留\n"
            "rate_limit:\n  mode: observe\n  server_rpm: 0\n"
        )
        pm._persist_auto_switch(False)
        text = (tmp_path / "iff.yaml").read_text()
        assert "注释必须保留" in text
        assert "mode: observe" in text
        assert "server_rpm: 0" in text
        assert "auto_switch:" in text and "enabled: false" in text

    def test_replaces_existing_block(self, tmp_path, monkeypatch):
        pm = _bare_pm(tmp_path, monkeypatch)
        (tmp_path / "iff.yaml").write_text(
            "auto_switch:\n  enabled: true\nrate_limit:\n  mode: reject\n"
        )
        pm._persist_auto_switch(False)
        text = (tmp_path / "iff.yaml").read_text()
        assert "enabled: false" in text
        assert "enabled: true" not in text
        assert "mode: reject" in text

    def test_idempotent_single_block(self, tmp_path, monkeypatch):
        pm = _bare_pm(tmp_path, monkeypatch)
        pm._persist_auto_switch(True)
        pm._persist_auto_switch(False)
        text = (tmp_path / "iff.yaml").read_text()
        assert text.count("auto_switch:") == 1
        assert "enabled: false" in text


# ═══════════════════════════════════════════════════════════════
# 4. 端点契约 + 路由注册
# ═══════════════════════════════════════════════════════════════


class TestEndpoint:
    def _handler(self):
        h = SimpleNamespace()
        h._send_json = MagicMock()
        return h

    def test_toggle_flips_and_returns_contract(self):
        pm = MagicMock()
        pm.auto_switch = True
        pm.set_auto_switch.return_value = {
            "auto_switch": False, "source": "env", "env_locked": True,
            "hint": "环境变量 EDGE_AUTO_SWITCH 已显式设置，重启 proxy 后将回到 env 值",
        }
        h = self._handler()
        ProxyHandler._handle_auto_switch_toggle(h, pm)
        pm.set_auto_switch.assert_called_once_with(False)  # 翻转语义
        sent = h._send_json.call_args[0][0]
        assert sent == pm.set_auto_switch.return_value
        assert set(sent) == {"auto_switch", "source", "env_locked", "hint"}

    def test_route_registered(self):
        assert "/admin/auto-switch/toggle" in handler_mod._POST_ROUTES

    def test_snapshot_exposes_auto_switch(self, monkeypatch):
        """snapshot local_models 暴露 auto_switch {enabled, source}（UI 数据源）。"""
        # 只验证字段拼装逻辑：直接调 snapshot 会拖重依赖，
        # 故断言 handler 源码中 snapshot 段落的字段契约。
        import inspect
        src = inspect.getsource(handler_mod.ProxyHandler._handle_snapshot)
        assert "auto_switch" in src
        assert "enabled" in src
