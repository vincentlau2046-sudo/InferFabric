"""
unit/infra/test_health_monitor.py — 后台健康监控测试

测试对象: inferfabric.health_monitor.HealthMonitor
覆盖范围:
  - start/stop 生命周期（守护线程、重复 start no-op）
  - _clean_manual_stops: 过期条目清理、无条目 no-op
  - _reconcile: 调用 mgr.reconcile、异常不抛出
  - health_check: 正常返回、异常返回 error
"""

import time
import threading
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


class TestHealthMonitorLifecycle:
    """start/stop 生命周期"""

    def test_start_creates_thread(self):
        """start() 创建后台守护线程。"""
        from inferfabric.health_monitor import HealthMonitor

        mgr = MagicMock()
        state = MagicMock()
        state.get_active_services.return_value = []

        monitor = HealthMonitor(mgr, state, interval=0.1)
        monitor.start()

        assert monitor._thread is not None
        assert monitor._thread.is_alive()
        assert monitor._thread.daemon

        monitor.stop(timeout=2)

    def test_stop_joins_thread(self):
        """stop() 等待线程结束。"""
        from inferfabric.health_monitor import HealthMonitor

        mgr = MagicMock()
        state = MagicMock()
        state.get_active_services.return_value = []

        monitor = HealthMonitor(mgr, state, interval=0.1)
        monitor.start()
        time.sleep(0.2)
        monitor.stop(timeout=2)

        assert not monitor._thread.is_alive()

    def test_double_start_is_noop(self):
        """重复 start() 不创建新线程。"""
        from inferfabric.health_monitor import HealthMonitor

        mgr = MagicMock()
        state = MagicMock()
        state.get_active_services.return_value = []

        monitor = HealthMonitor(mgr, state, interval=0.1)
        monitor.start()
        thread1 = monitor._thread
        monitor.start()  # 再次 start
        thread2 = monitor._thread

        assert thread1 is thread2
        monitor.stop(timeout=2)


class TestHealthMonitorCleanManualStops:
    """手动停止清理"""

    def test_cleans_expired_entries(self):
        """过期的 manual_stop 条目被清理。"""
        from inferfabric.health_monitor import HealthMonitor

        mgr = MagicMock()
        state = MagicMock()
        state.get_active_services.return_value = []
        # 模拟 MANUAL_STOP_TTL = 3600
        state.MANUAL_STOP_TTL = 3600
        state.get_all_manual_stops.return_value = {
            "expired-model": time.time() - 7200,  # 2 小时前，超过 TTL
            "fresh-model": time.time() - 60,       # 1 分钟前，未过期
        }

        monitor = HealthMonitor(mgr, state, interval=60)
        monitor._clean_manual_stops()

        # 只应清理过期的
        state.clear_manual_stop.assert_called_once_with("expired-model")

    def test_no_manual_stops_noop(self):
        """无 manual_stop 条目时不操作。"""
        from inferfabric.health_monitor import HealthMonitor

        mgr = MagicMock()
        state = MagicMock()
        state.get_active_services.return_value = []
        state.get_all_manual_stops.return_value = {}

        monitor = HealthMonitor(mgr, state, interval=60)
        monitor._clean_manual_stops()

        state.clear_manual_stop.assert_not_called()


class TestHealthMonitorReconcile:
    """状态调和"""

    def test_calls_mgr_reconcile(self):
        """_reconcile 调用 mgr.reconcile()。"""
        from inferfabric.health_monitor import HealthMonitor

        mgr = MagicMock()
        mgr.reconcile.return_value = {"actions": ["cleaned dead pid"]}
        state = MagicMock()

        monitor = HealthMonitor(mgr, state, interval=60)
        monitor._reconcile()

        mgr.reconcile.assert_called_once()

    def test_reconcile_failure_does_not_raise(self):
        """mgr.reconcile() 失败不抛异常。"""
        from inferfabric.health_monitor import HealthMonitor

        mgr = MagicMock()
        mgr.reconcile.side_effect = Exception("reconcile failed")
        state = MagicMock()

        monitor = HealthMonitor(mgr, state, interval=60)
        # 不应抛异常
        monitor._reconcile()


class TestHealthMonitorHealthCheck:
    """单次健康检查"""

    def test_returns_status_ok(self):
        """health_check 返回正常状态。"""
        from inferfabric.health_monitor import HealthMonitor

        mgr = MagicMock()
        state = MagicMock()
        state.get_active_services.return_value = ["qwen36-27b"]
        state.gpu_mode = "exclusive"

        monitor = HealthMonitor(mgr, state, interval=60)
        result = monitor.health_check()

        assert result["status"] == "ok"
        assert result["gpu_mode"] == "exclusive"
        assert "qwen36-27b" in result["active_services"]

    def test_returns_error_on_exception(self):
        """health_check 异常时返回 error 状态。"""
        from inferfabric.health_monitor import HealthMonitor

        mgr = MagicMock()
        state = MagicMock()
        state.get_active_services.side_effect = Exception("db error")

        monitor = HealthMonitor(mgr, state, interval=60)
        result = monitor.health_check()

        assert result["status"] == "error"
        assert "db error" in result["message"]
