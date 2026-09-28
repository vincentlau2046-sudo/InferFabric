"""unit/proxy/test_agent_stats.py — /api/agent-stats 纯聚合函数测试。

钉 Review Focus 3：历史行 agent='' 与 'unknown' 混存 → 同一个未识别桶。
"""
from inferfabric.agent_stats import AGENT_GRAN, aggregate_agent_stats
from inferfabric.metrics_aggregator import CloudModelPrice, cost_of_row

PRICES = {"glm-5.1": CloudModelPrice(price_input=5.0, price_output=15.0)}


def _row(**kw):
    base = dict(req_id="r", key_name="k", model="glm-5.1", status=200,
                tokens_in=1_000_000, tokens_out=0, timestamp=0.0,
                cloud_provider="baidu", agent="codex", ua="codex/1")
    base.update(kw)
    return base


class TestCostOfRow:
    def test_cloud_priced(self):
        assert cost_of_row(PRICES, _row(cloud_provider="baidu")) == 5.0  # 1M in @ ¥5/1M

    def test_local_zero(self):
        assert cost_of_row(PRICES, _row(cloud_provider=None)) == 0.0

    def test_unpriced_zero(self):
        assert cost_of_row({}, _row(cloud_provider="baidu")) == 0.0


NOW = 1000.0  # 注入的墙钟右缘：bucket 窗口相对它，测试可断言

class TestAggregate:
    def test_basic_bucketing(self):
        rows = [
            _row(timestamp=NOW - 50, agent="claude-code", ua="cc"),
            _row(timestamp=NOW - 60, agent="codex", ua="cx"),
        ]
        out = aggregate_agent_stats(rows, "hour", "all", PRICES,
                                    {"claude-code": {"name": "Claude Code", "color": "#1", "source": "builtin"}},
                                    now=NOW)
        assert out["window_requests"] == 2
        cc = next(t for t in out["totals"] if t["agent"] == "claude-code")
        assert cc["requests"] == 1 and cc["top_models"][0]["model"] == "glm-5.1"
        cx = next(t for t in out["totals"] if t["agent"] == "codex")
        assert cx["cost_yuan"] == 5.0
        # 两个请求都落在「最近」桶（idx = n-1）
        assert out["series"]["claude-code"][-1]["requests"] == 1

    def test_empty_and_unknown_normalized(self):
        """历史 '' 与 'unknown' 混存 → 单一未识别桶。"""
        rows = [_row(timestamp=NOW - 10, agent="", ua="curl/8"),
                _row(timestamp=NOW - 20, agent="unknown", ua="python-requests/2")]
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, {}, now=NOW)
        ids = [t["agent"] for t in out["totals"]]
        assert ids == ["unknown"]
        unk = out["totals"][0]
        assert unk["requests"] == 2
        assert {u["ua"] for u in out["unassigned"]} == {"curl/8", "python-requests/2"}

    def test_scope_local_filters_cloud(self):
        rows = [_row(timestamp=NOW - 10, cloud_provider=None),
                _row(timestamp=NOW - 10, cloud_provider="baidu")]
        lok = aggregate_agent_stats(rows, "hour", "local", PRICES, {}, now=NOW)
        assert lok["window_requests"] == 1
        clo = aggregate_agent_stats(rows, "hour", "cloud", PRICES, {}, now=NOW)
        assert clo["window_requests"] == 1

    def test_unassigned_top20_truncated(self):
        rows = [_row(timestamp=NOW - 10, agent="", ua="x" * 300 + "/1") for _ in range(25)]
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, {}, now=NOW)
        assert len(out["unassigned"]) <= 20
        assert all(len(u["ua"]) <= 120 for u in out["unassigned"])

    def test_unknown_row_kept_with_ua(self):
        rows = [_row(timestamp=NOW - 10, agent="unknown", ua="curl/8.5.2")]
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, {}, now=NOW)
        assert out["totals"][0]["agent"] == "unknown"
