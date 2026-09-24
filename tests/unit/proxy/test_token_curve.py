"""
unit/proxy/test_token_curve.py — _handle_token_curve 四档（分钟/小时/天/周）桶语义测试

统一单位约定（用户拍板）：
  minute = 近 60min · 12×5min（请求级 1min 太碎，5min 桶对齐一次请求量级）
  hour   = 近 24h  · 24×1h
  day    = 近 30 天 · 30×1d
  week   = 近 90 天 · ~13×1-week（7 天周桶）
  月单位整体弃用（端点删除 month，UI 不再提供）。

全档统一「相对年龄」分桶：idx = n-1 - floor(age/width)，最旧在左 idx=0、
最新在右 idx=n-1 —— 与图 x 轴「近 X」语义一致，避免墙钟把间隔 > 窗口宽的
离散时段合并不连续桶。

每桶含 prompt/completion 拆分（Prompt/Completion 堆叠条用）：
tokens == prompt + completion（向后兼容）。dual-scope {local, cloud}。
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
    def _bucket_fields_present(self, buckets, n):
        assert len(buckets) == n, f"应 {n} 个桶，got {len(buckets)}"
        for b in buckets:
            assert "prompt" in b, f"桶缺 prompt: {b}"
            assert "completion" in b, f"桶缺 completion: {b}"
            assert "tokens" in b, f"桶缺 tokens（向后兼容）: {b}"
            assert b["tokens"] == b["prompt"] + b["completion"], \
                f"tokens 应 == prompt+completion: {b}"

    def test_minute_buckets_have_prompt_completion_split(self):
        """分钟档：近 60min · 12×5min 桶。now（0-5min 前）→ idx 11（最新，最右）。"""
        h, _ = _make_handler()
        h.path = "/api/token-curve?granularity=minute"

        now = time.time()
        pm = MagicMock()
        pm.telemetry.query_request_log.return_value = [
            _row(now, tokens_in=100, tokens_out=200),
            _row(now, tokens_in=50, tokens_out=30),
        ]
        h._handle_token_curve(pm)

        payload = h._send_json.call_args.args[0]
        assert payload["granularity"] == "minute"
        local = payload["local"]
        self._bucket_fields_present(local, 12)
        target = local[11]
        assert target["prompt"] == 150, f"prompt 累计错误: {target}"
        assert target["completion"] == 230, f"completion 累计错误: {target}"
        assert target["tokens"] == 380
        assert target["requests"] == 2

    def test_minute_distributes_all_12_five_min_buckets(self):
        """12 条请求每条相隔 5 分钟（0,5,…,55 分钟前）→ 12 个 5min 桶全覆盖。"""
        h, _ = _make_handler()
        h.path = "/api/token-curve?granularity=minute"

        now = time.time()
        rows = []
        for i in range(12):
            rows.append(_row(now - i * 300, tokens_in=10, tokens_out=20))
        pm = MagicMock()
        pm.telemetry.query_request_log.return_value = rows

        h._handle_token_curve(pm)
        local = h._send_json.call_args.args[0]["local"]

        nonempty = [b for b in local if b["requests"] > 0]
        assert len(nonempty) == 12, \
            f"应 12 个非空 5min 桶（整小时覆盖），got {len(nonempty)}" \
            f"idx: {[i for i,b in enumerate(local) if b['requests']>0]}"
        for b in nonempty:
            assert b["prompt"] == 10 and b["completion"] == 20
            assert b["tokens"] == 30

    def test_minute_relative_age_bucketing_not_clock_minute(self):
        """minute 桶按相对年龄分桶（5min 宽），不按墙钟分钟。

        构造两条请求：ts1 = now（0min 前→idx 11）、ts2 = now - 50*60（50min 前→idx 1）。
        若按墙钟分钟，二者 minute 也可能相同；按相对年龄 idx 11 vs 1 必分离。
        """
        h, _ = _make_handler()
        h.path = "/api/token-curve?granularity=minute"

        now = time.time()
        pm = MagicMock()
        pm.telemetry.query_request_log.return_value = [
            _row(now, tokens_in=100, tokens_out=200),               # idx 11
            _row(now - 50 * 60, tokens_in=7, tokens_out=8),         # idx 1
        ]
        h._handle_token_curve(pm)
        local = h._send_json.call_args.args[0]["local"]

        assert local[11]["requests"] == 1 and local[11]["prompt"] == 100
        assert local[1]["requests"] == 1 and local[1]["prompt"] == 7
        nonempty = [i for i, b in enumerate(local) if b["requests"] > 0]
        assert sorted(nonempty) == [1, 11], f"应只有 idx 1 和 11 非空: {nonempty}"

    def test_hour_buckets_24h(self):
        """小时档：近 24h · 24×1h 桶。now → idx 23（最新），23h 前 → idx 0。"""
        h, _ = _make_handler()
        h.path = "/api/token-curve?granularity=hour"

        now = time.time()
        rows = []
        for i in range(24):
            rows.append(_row(now - i * 3600, tokens_in=100 + i, tokens_out=200 + i))
        pm = MagicMock()
        pm.telemetry.query_request_log.return_value = rows

        h._handle_token_curve(pm)
        local = h._send_json.call_args.args[0]["local"]
        self._bucket_fields_present(local, 24)
        nonempty = [i for i, b in enumerate(local) if b["requests"] > 0]
        assert sorted(nonempty) == list(range(24))
        assert local[23]["prompt"] == 100      # 最新（0-1h 前）
        assert local[0]["prompt"] == 123       # 最旧（23-24h 前）

    def test_day_buckets_30d(self):
        """天档：近 30 天 · 30×1d 桶。now → idx 29（最新），29 天前 → idx 0。"""
        h, _ = _make_handler()
        h.path = "/api/token-curve?granularity=day"

        now = time.time()
        rows = []
        for i in range(30):
            rows.append(_row(now - i * 86400, tokens_in=7, tokens_out=8))
        pm = MagicMock()
        pm.telemetry.query_request_log.return_value = rows

        h._handle_token_curve(pm)
        local = h._send_json.call_args.args[0]["local"]
        self._bucket_fields_present(local, 30)
        assert [b["requests"] for b in local] == [1] * 30
        assert local[29]["prompt"] == 7 and local[0]["prompt"] == 7

    def test_week_buckets_90d(self):
        """周档（新增）：近 90 天 · 13×7d 周桶。now → idx 12（最新），12 周前 → idx 0。"""
        h, _ = _make_handler()
        h.path = "/api/token-curve?granularity=week"

        now = time.time()
        rows = []
        for i in range(13):
            rows.append(_row(now - i * 7 * 86400, tokens_in=10 + i, tokens_out=30 + i))
        pm = MagicMock()
        pm.telemetry.query_request_log.return_value = rows

        h._handle_token_curve(pm)
        local = h._send_json.call_args.args[0]["local"]
        self._bucket_fields_present(local, 13)
        assert [b["requests"] for b in local] == [1] * 13
        assert local[12]["prompt"] == 10       # 最新（0-7 天前）
        assert local[0]["prompt"] == 22        # 最旧（84-91 天前）
        assert local[12]["tokens"] == 40

    def test_cloud_rows_go_to_cloud_bucket_with_split(self):
        """cloud_provider 行落入 cloud 桶，同样有 prompt/completion 拆分。"""
        h, _ = _make_handler()
        h.path = "/api/token-curve?granularity=minute"

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
        cb = cloud[11]
        assert cb["prompt"] == 100 and cb["completion"] == 200, f"cloud 拆分错误: {cb}"
        assert cb["tokens"] == 300

    def test_invalid_granularity_400(self):
        """month 已整体弃用 → 无效档位 400。"""
        h, _ = _make_handler()
        h.path = "/api/token-curve?granularity=month"
        pm = MagicMock()
        h._handle_token_curve(pm)
        body, code = h._send_json.call_args.args
        assert code == 400 and "invalid granularity" in body["error"]