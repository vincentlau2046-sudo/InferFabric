"""
unit/infra/test_token_stats.py — Token 统计采集器测试

测试对象: inferfabric.token_stats.TokenStatsCollector
覆盖范围:
  - 初始化状态、manager_ref 注入
  - _today_key: YYYY-MM-DD 格式、一致性
  - _compute_deltas: 首次基线、正增量、计数器重置、多端口独立
  - _aggregate: 状态更新、累加、多模型独立
  - _cleanup: 30 天过期清理、近期保留
  - start/stop: 守护线程、重复 start no-op
"""

import time
import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest


class TestTokenStatsCollectorInit:
    """初始化"""

    def test_initial_state(self):
        """初始状态正确。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector(manager_ref=None, interval=60, db=None)

        assert collector._state == {}
        assert collector._snapshots == {}
        assert collector.interval == 60
        assert collector._thread is None

    def test_with_manager_ref(self):
        """传入 manager_ref 后可正常初始化。"""
        from inferfabric.token_stats import TokenStatsCollector

        ref = MagicMock()
        collector = TokenStatsCollector(manager_ref=ref, interval=300)

        assert collector.manager_ref is ref
        assert collector.interval == 300


class TestTokenStatsTodayKey:
    """日期键"""

    def test_today_key_format(self):
        """_today_key 返回 YYYY-MM-DD 格式。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector()
        key = collector._today_key()

        assert len(key) == 10
        assert key[4] == "-"
        assert key[7] == "-"

    def test_today_key_consistent(self):
        """同一天内多次调用返回相同 key。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector()
        k1 = collector._today_key()
        k2 = collector._today_key()
        assert k1 == k2


class TestTokenStatsDeltas:
    """增量计算（基于端口快照）"""

    def test_first_collection_stores_baseline(self):
        """首次采集存储基线，返回 None。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector()

        current = {"prompt_sum": 100, "gen_sum": 200, "req_total": 5}
        delta = collector._compute_deltas(port=8000, current=current)

        assert delta is None  # 首次无增量
        assert 8000 in collector._snapshots

    def test_second_collection_returns_delta(self):
        """第二次采集返回正增量。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector()

        # 首次采集
        collector._compute_deltas(8000, {"prompt_sum": 100, "gen_sum": 200, "req_total": 5})
        # 第二次采集
        delta = collector._compute_deltas(8000, {"prompt_sum": 150, "gen_sum": 300, "req_total": 8})

        assert delta is not None
        assert delta["prompt_sum"] == 50
        assert delta["gen_sum"] == 100
        assert delta["req_total"] == 3

    def test_counter_reset_returns_none(self):
        """计数器重置时（当前值 < 前值）返回 None。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector()

        collector._compute_deltas(8000, {"prompt_sum": 1000, "gen_sum": 2000, "req_total": 50})
        delta = collector._compute_deltas(8000, {"prompt_sum": 50, "gen_sum": 30, "req_total": 2})

        assert delta is None  # 计数器重置，丢弃本轮

    def test_multiple_ports_independent(self):
        """不同端口的快照独立维护。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector()

        collector._compute_deltas(8000, {"prompt_sum": 100, "gen_sum": 200, "req_total": 5})
        collector._compute_deltas(8001, {"prompt_sum": 50, "gen_sum": 80, "req_total": 3})

        d1 = collector._compute_deltas(8000, {"prompt_sum": 150, "gen_sum": 300, "req_total": 8})
        d2 = collector._compute_deltas(8001, {"prompt_sum": 70, "gen_sum": 100, "req_total": 5})

        assert d1["prompt_sum"] == 50
        assert d2["prompt_sum"] == 20


class TestTokenStatsAggregate:
    """聚合逻辑"""

    def test_aggregate_updates_state(self):
        """聚合更新内部状态。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector()
        today = collector._today_key()

        collector._aggregate("test-model", {"prompt_sum": 100, "gen_sum": 200, "req_total": 5})

        assert today in collector._state
        assert "test-model" in collector._state[today]
        assert collector._state[today]["test-model"]["prompt_tokens"] == 100
        assert collector._state[today]["test-model"]["generation_tokens"] == 200
        assert collector._state[today]["test-model"]["requests"] == 5

    def test_aggregate_accumulates(self):
        """多次聚合累加。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector()
        today = collector._today_key()

        collector._aggregate("m1", {"prompt_sum": 10, "gen_sum": 20, "req_total": 1})
        collector._aggregate("m1", {"prompt_sum": 5, "gen_sum": 10, "req_total": 1})

        assert collector._state[today]["m1"]["prompt_tokens"] == 15
        assert collector._state[today]["m1"]["generation_tokens"] == 30
        assert collector._state[today]["m1"]["requests"] == 2

    def test_aggregate_multiple_models(self):
        """不同模型独立聚合。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector()
        today = collector._today_key()

        collector._aggregate("m1", {"prompt_sum": 10, "gen_sum": 20, "req_total": 1})
        collector._aggregate("m2", {"prompt_sum": 30, "gen_sum": 40, "req_total": 2})

        assert collector._state[today]["m1"]["prompt_tokens"] == 10
        assert collector._state[today]["m2"]["prompt_tokens"] == 30


class TestTokenStatsCleanup:
    """清理过期数据"""

    def test_cleanup_removes_old_dates(self):
        """_cleanup 删除 30 天前的数据。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector()

        old_date = (datetime.now(tz=timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%d")
        today = collector._today_key()

        collector._state[old_date] = {"m1": {"prompt_tokens": 100}}
        collector._state[today] = {"m1": {"prompt_tokens": 200}}

        collector._cleanup()

        assert old_date not in collector._state
        assert today in collector._state

    def test_cleanup_preserves_recent_data(self):
        """_cleanup 保留近期数据。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector()

        recent_date = (datetime.now(tz=timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%d")
        today = collector._today_key()

        collector._state[recent_date] = {"m1": {"prompt_tokens": 100}}
        collector._state[today] = {"m1": {"prompt_tokens": 200}}

        collector._cleanup()

        assert recent_date in collector._state
        assert today in collector._state


class TestTokenStatsLifecycle:
    """start/stop 生命周期"""

    def test_start_creates_thread(self):
        """start() 创建后台线程。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector(manager_ref=None, interval=60)

        with patch.object(collector, "_load_from_file"):
            collector.start()

        assert collector._thread is not None
        assert collector._thread.is_alive()
        assert collector._thread.daemon

        with patch.object(collector, "_persist"):
            collector.stop()

    def test_stop_joins_thread(self):
        """stop() 等待线程结束。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector(manager_ref=None, interval=0.1)

        with patch.object(collector, "_load_from_file"):
            with patch.object(collector, "_collect_once"):
                collector.start()

        time.sleep(0.3)

        with patch.object(collector, "_persist"):
            collector.stop()

        assert not collector._thread.is_alive()

    def test_double_start_is_noop(self):
        """重复 start() 不创建新线程。"""
        from inferfabric.token_stats import TokenStatsCollector

        collector = TokenStatsCollector(manager_ref=None, interval=60)

        with patch.object(collector, "_load_from_file"):
            collector.start()
            t1 = collector._thread
            collector.start()
            t2 = collector._thread

        assert t1 is t2

        with patch.object(collector, "_persist"):
            collector.stop()
