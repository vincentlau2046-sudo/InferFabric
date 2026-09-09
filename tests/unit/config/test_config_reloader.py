"""
unit/config/test_config_reloader.py — 配置热加载测试

测试对象: inferfabric.config_reloader.ConfigReloader
覆盖范围:
  - SIGHUP / SIGUSR1 信号注册（非主线程安全）
  - reload_all() 全量重载（models + auth + cloud + dashboard 缓存）
  - reload_models() 仅重载模型
  - cooldown 防抖（5s 内不重复重载）
  - 异常容错（单个组件失败不影响其他）
"""

import signal
import time
from unittest.mock import MagicMock, patch

import pytest


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
