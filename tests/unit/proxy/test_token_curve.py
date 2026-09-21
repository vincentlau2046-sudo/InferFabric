"""
unit/proxy/test_token_curve.py — _handle_token_curve 桶拆分字段测试

根因修复（方案 B）：小时图复用 snapshot 的 limit=50 request_log 导致只显示
最新 ~4 根柱子。改为消费 /api/token-curve（服务端 60 桶聚合）。但图表是
Prompt/Completion 堆叠条，token-curve 每桶原只返回 tokens 合计 → 需加
prompt/completion 拆分字段。

本测试断言 _handle_token_curve 响应每桶含 prompt/completion，且正确分桶，
tokens == prompt + completion（向后兼容）。
"""

import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_deps = Path(__file__).parent.parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import pytest


def _make_handler():
    """ProxyHandler with minimal mocked state (no reload needed — no admin token)."""
    import inferfabric.proxy.handler as handler_module
    ProxyHandler = handler_module.ProxyHandler
    h = ProxyHandler.__new__(ProxyHandler)
    h.headers = {}
    h._send_json = MagicMock()
    return h, handler_module


def _row(ts, tokens_in, tokens_out, cloud_provider=None):
    """合成 request_log 行（_handle_token_curve 只读这几个字段）。"""
    return {
        "timestamp": ts,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cloud_provider": cloud_provider,
    }


class TestTokenCurveBucketSplit:
    def test_hour_buckets_have_prompt_completion_split(self):
        """每桶含 prompt/completion 字段，且 tokens == prompt + completion（向后兼容）。

        hour 桶按相对年龄 idx = 59 - minAgo（最旧 idx=0、最新 idx=59）。
        """
        h, _ = _make_handler()
        h.path = "/api/token-curve?granularity=hour"

        now = time.time()
        ts = now  # 0 分钟前 → idx = 59
        pm = MagicMock()
        pm.telemetry.query_request_log.return_value = [
            _row(ts, tokens_in=100, tokens_out=200),
            _row(ts, tokens_in=50, tokens_out=30),
        ]

        h._handle_token_curve(pm)

        payload = h._send_json.call_args.args[0]
        assert payload["granularity"] == "hour"
        local = payload["local"]
        assert len(local) == 60, "hour 应 60 个分钟桶"

        for b in local:
            assert "prompt" in b, f"桶缺 prompt: {b}"
            assert "completion" in b, f"桶缺 completion: {b}"
            assert "tokens" in b, f"桶缺 tokens（向后兼容）: {b}"
            assert b["tokens"] == b["prompt"] + b["completion"], \
                f"tokens 应 == prompt+completion: {b}"

        # 0 分钟前 → idx 59（最新，最右）
        target = local[59]
        assert target["prompt"] == 150, f"prompt 累计错误: {target}"
        assert target["completion"] == 230, f"completion 累计错误: {target}"
        assert target["tokens"] == 380
        assert target["requests"] == 2

    def test_hour_distributes_across_multiple_minute_buckets(self):
        """跨多分钟桶正确分布（修复核心：旧 limit=50 只画 ~4 根；token-curve 应覆盖整小时）。

        6 条请求每条相隔 10 分钟（0,10,20,30,40,50 分钟前）→ 6 个不同相对年龄桶。
        """
        h, _ = _make_handler()
        h.path = "/api/token-curve?granularity=hour"

        now = time.time()
        rows = []
        for i in range(6):
            ts = now - i * 600  # 0,10,20,30,40,50 分钟前
            rows.append(_row(ts, tokens_in=10 * (i + 1), tokens_out=5 * (i + 1)))
        pm = MagicMock()
        pm.telemetry.query_request_log.return_value = rows

        h._handle_token_curve(pm)
        local = h._send_json.call_args.args[0]["local"]

        nonempty = [b for b in local if b["requests"] > 0]
        assert len(nonempty) == 6, \
            f"应 6 个非空桶（整小时覆盖），got {len(nonempty)}: {nonempty}"
        for b in nonempty:
            assert b["prompt"] > 0 and b["completion"] > 0, f"拆分字段为零: {b}"
            assert b["tokens"] == b["prompt"] + b["completion"]

    def test_hour_relative_age_bucketing_not_clock_minute(self):
        """hour 桶按相对年龄分桶，不按时钟分钟 —— 验证不合并不连续时段。

        构造两条请求：ts1 = now（0 分钟前，idx 59）、ts2 = now - 50*60（50 分钟前，idx 9）。
        若按时钟分钟，二者 minute 不同也可能撞桶；按相对年龄 idx=59 vs idx=9 必分离。
        """
        h, _ = _make_handler()
        h.path = "/api/token-curve?granularity=hour"

        now = time.time()
        pm = MagicMock()
        pm.telemetry.query_request_log.return_value = [
            _row(now, tokens_in=100, tokens_out=200),               # idx 59
            _row(now - 50 * 60, tokens_in=7, tokens_out=8),         # idx 9
        ]
        h._handle_token_curve(pm)
        local = h._send_json.call_args.args[0]["local"]

        assert local[59]["requests"] == 1 and local[59]["prompt"] == 100
        assert local[9]["requests"] == 1 and local[9]["prompt"] == 7
        # 其余桶空
        nonempty = [i for i, b in enumerate(local) if b["requests"] > 0]
        assert sorted(nonempty) == [9, 59], f"应只有 idx 9 和 59 非空: {nonempty}"

    def test_cloud_rows_go_to_cloud_bucket_with_split(self):
        """cloud_provider 行落入 cloud 桶，同样有 prompt/completion 拆分。"""
        h, _ = _make_handler()
        h.path = "/api/token-curve?granularity=hour"

        now = time.time()
        pm = MagicMock()
        pm.telemetry.query_request_log.return_value = [
            _row(now, tokens_in=100, tokens_out=200, cloud_provider="openai"),
        ]

        h._handle_token_curve(pm)
        payload = h._send_json.call_args.args[0]
        local = payload["local"]
        cloud = payload["cloud"]

        assert all(b["requests"] == 0 for b in local)
        nonempty_cloud = [b for b in cloud if b["requests"] > 0]
        assert len(nonempty_cloud) == 1
        cb = nonempty_cloud[0]
        assert cb["prompt"] == 100 and cb["completion"] == 200, f"cloud 拆分错误: {cb}"
        assert cb["tokens"] == 300
