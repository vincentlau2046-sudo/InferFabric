"""
unit/config/test_config_reloader.py — 配置热加载测试

测试对象: inferfabric.config_reloader.ConfigReloader
覆盖范围:
  - SIGHUP / SIGUSR1 信号注册（非主线程安全）
  - reload_all() 全量重载（models + auth + cloud + dashboard 缓存）
  - reload_models() 仅重载模型
  - cooldown 防抖（5s 内不重复重载）
  - 异常容错（单个组件失败不影响其他）
  - B2：无参 cloud.reload() 走实例自身 config path（SIGHUP 云热加载链路）

B2 根因：CloudDiscovery.reload(config_path) 的 config_path 曾是必需参数，而
config_reloader.reload_all() 调用无参 self._cloud.reload() → TypeError 被
except 吞掉只打 log → SIGHUP / POST /reload-config 的云配置热加载静默失效
（端点仍返回 "reloaded"，但 cloud_provider.yaml 的修改从未生效）。
"""

import signal
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[3]  # tests/unit/config → repo root
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import pytest


class _StubMgr:
    """ModelManager 桩 — reload_models 无副作用。"""
    def reload_models(self):
        pass


def _write_cfg(path: Path, v2: bool):
    if v2:
        path.write_text(textwrap.dedent("""
            providers:
              prov_a:
                openai_base: http://localhost:19999/v1
                api_key: sk-new
                discovery: {enabled: false}
              prov_b:
                openai_base: http://localhost:29999/v1
                discovery: {enabled: false}
        """))
    else:
        path.write_text(textwrap.dedent("""
            providers:
              prov_a:
                openai_base: http://localhost:19999/v1
                api_key: sk-old
                discovery: {enabled: false}
        """))


class TestConfigReloaderSetup:
    """信号注册"""

    def test_setup_registers_handlers(self):
        """setup() 注册 SIGHUP 和 SIGUSR1 handler。"""
        from inferfabric.config_reloader import ConfigReloader

        mgr = MagicMock()
        reloader = ConfigReloader(mgr)

        with patch("inferfabric.config_reloader.signal.signal") as mock_sig:
            reloader.setup()
            registered_sigs = [c[0][0] for c in mock_sig.call_args_list]
            assert signal.SIGHUP in registered_sigs
            assert signal.SIGUSR1 in registered_sigs

    def test_setup_skips_in_non_main_thread(self):
        """非主线程中 setup 不抛异常（ValueError 被捕获）。"""
        from inferfabric.config_reloader import ConfigReloader

        mgr = MagicMock()
        reloader = ConfigReloader(mgr)

        with patch("inferfabric.config_reloader.signal.signal",
                   side_effect=ValueError("signal only works in main thread")):
            reloader.setup()  # 不应抛异常


class TestConfigReloaderReloadAll:
    """reload_all 全量重载"""

    def test_calls_mgr_reload_models(self):
        """reload_all 调用 mgr.reload_models()。"""
        from inferfabric.config_reloader import ConfigReloader

        mgr = MagicMock()
        auth = MagicMock()
        cloud = MagicMock()
        reloader = ConfigReloader(mgr, auth=auth, cloud=cloud)
        reloader._last_reload = 0.0

        reloader.reload_all()

        mgr.reload_models.assert_called_once()
        auth.reload.assert_called_once()
        cloud.reload.assert_called_once()

    def test_cooldown_prevents_rapid_reload(self):
        """cooldown 期间的 reload_all 被跳过。"""
        from inferfabric.config_reloader import ConfigReloader

        mgr = MagicMock()
        reloader = ConfigReloader(mgr)
        reloader._last_reload = time.time() - 1.0  # 1 秒前，cooldown=5s
        reloader.reload_all()

        mgr.reload_models.assert_not_called()

    def test_cooldown_expired_allows_reload(self):
        """cooldown 过期后允许重载。"""
        from inferfabric.config_reloader import ConfigReloader

        mgr = MagicMock()
        reloader = ConfigReloader(mgr)
        reloader._last_reload = time.time() - 10.0  # 10 秒前，超过 cooldown=5s
        reloader.reload_all()

        mgr.reload_models.assert_called_once()

    def test_auth_reload_failure_does_not_block(self):
        """auth.reload() 失败不影响 cloud 和 models 重载。"""
        from inferfabric.config_reloader import ConfigReloader

        mgr = MagicMock()
        auth = MagicMock()
        auth.reload.side_effect = Exception("auth error")
        cloud = MagicMock()
        reloader = ConfigReloader(mgr, auth=auth, cloud=cloud)
        reloader._last_reload = 0.0

        reloader.reload_all()  # 不应抛异常

        mgr.reload_models.assert_called_once()
        cloud.reload.assert_called_once()

    def test_no_auth_cloud_still_works(self):
        """auth 和 cloud 为 None 时 reload_all 仍正常工作。"""
        from inferfabric.config_reloader import ConfigReloader

        mgr = MagicMock()
        reloader = ConfigReloader(mgr, auth=None, cloud=None)
        reloader._last_reload = 0.0

        reloader.reload_all()
        mgr.reload_models.assert_called_once()


class TestConfigReloaderReloadModels:
    """reload_models 仅重载模型"""

    def test_calls_mgr_reload_models_only(self):
        """reload_models 只调用 mgr.reload_models()，不调用 auth/cloud。"""
        from inferfabric.config_reloader import ConfigReloader

        mgr = MagicMock()
        auth = MagicMock()
        cloud = MagicMock()
        reloader = ConfigReloader(mgr, auth=auth, cloud=cloud)

        reloader.reload_models()

        mgr.reload_models.assert_called_once()
        auth.reload.assert_not_called()
        cloud.reload.assert_not_called()

    def test_mgr_failure_does_not_raise(self):
        """mgr.reload_models() 失败不抛异常。"""
        from inferfabric.config_reloader import ConfigReloader

        mgr = MagicMock()
        mgr.reload_models.side_effect = Exception("reload failed")
        reloader = ConfigReloader(mgr)

        reloader.reload_models()


class TestSighupCloudReload:
    """B2：SIGHUP/reload-config 云配置热加载链路"""

    def test_reload_all_no_arg_uses_instance_config_path(self, tmp_path):
        """B2 核心回归：SIGHUP/reload-config 后 cloud_provider.yaml 修改真实生效。"""
        from inferfabric.cloud_discovery import CloudDiscovery
        from inferfabric.config_reloader import ConfigReloader

        cfg = tmp_path / "cloud_provider.yaml"
        _write_cfg(cfg, v2=False)
        cd = CloudDiscovery(cfg)
        assert "prov_a" in cd.providers
        assert cd.providers["prov_a"].api_key == "sk-old"

        # 模拟用户编辑配置（SIGHUP / /reload-config 的真实触发场景）
        _write_cfg(cfg, v2=True)

        reloader = ConfigReloader(_StubMgr(), None, cd)
        failed = reloader.reload_all()

        assert cd.providers["prov_a"].api_key == "sk-new", \
            f"reload_all 后云配置未生效: {cd.providers['prov_a'].api_key}"
        assert "prov_b" in cd.providers, "新增 provider 未被 reload 拾取"
        assert failed == [], f"不应有失败域: {failed}"

    def test_cloud_reload_no_arg_uses_ctor_path(self, tmp_path):
        """CloudDiscovery.reload() 无参 = reload(self._config_path)（缺省回退构造时路径）。"""
        from inferfabric.cloud_discovery import CloudDiscovery

        cfg = tmp_path / "cloud_provider.yaml"
        _write_cfg(cfg, v2=False)
        cd = CloudDiscovery(cfg)

        _write_cfg(cfg, v2=True)
        cd.reload()  # 无参 — 修复前此处 TypeError: missing config_path

        assert cd.providers["prov_a"].api_key == "sk-new"
        assert "prov_b" in cd.providers

    def test_cloud_reload_explicit_path_still_wins(self, tmp_path):
        """显式传参路径优先于构造时路径（既有 handler._handle_cloud_reload 行为保持）。"""
        from inferfabric.cloud_discovery import CloudDiscovery

        cfg1 = tmp_path / "cfg1.yaml"
        cfg2 = tmp_path / "cfg2.yaml"
        _write_cfg(cfg1, v2=False)
        _write_cfg(cfg2, v2=True)
        cd = CloudDiscovery(cfg1)

        cd.reload(cfg2)

        assert cd.providers["prov_a"].api_key == "sk-new"
        assert cd._config_path == cfg2

    def test_reload_all_reports_failed_domains(self, tmp_path):
        """某域 reload 抛异常 → reload_all 返回失败域列表（不再静默假成功）。"""
        from inferfabric.config_reloader import ConfigReloader

        class _BadCloud:
            def reload(self, config_path=None):
                raise RuntimeError("boom")

        reloader = ConfigReloader(_StubMgr(), None, _BadCloud())
        failed = reloader.reload_all()
        assert failed == ["cloud"]

    def test_reload_all_no_domains_returns_empty(self, tmp_path):
        """只有 models 域 → 全部成功返回 []。"""
        from inferfabric.config_reloader import ConfigReloader

        failed = ConfigReloader(_StubMgr(), None, None).reload_all()
        assert failed == []
