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
        """v6.6: agent='' 行（采集未跑，历史存量）不进聚合；agent='unknown'（分类器
        跑过未命中）保留为唯一未识别桶。采集后 curl/urllib 应被 classify 为 'unknown' 而非 ''。"""
        rows = [_row(timestamp=NOW - 10, agent="", ua="curl/8"),           # 历史存量→排除
                _row(timestamp=NOW - 20, agent="unknown", ua="python-requests/2")]  # 采集后未命中→保留
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, {}, now=NOW)
        ids = [t["agent"] for t in out["totals"]]
        assert ids == ["unknown"]
        unk = out["totals"][0]
        assert unk["requests"] == 1  # 仅 unknown 行计入
        assert {u["ua"] for u in out["unassigned"]} == {"python-requests/2"}

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


# ── v6.6 优化：历史空行不进聚合 + 用户认领 agent 走 palette 补色 ──

class TestHistoricalFilter:
    def test_empty_agent_rows_excluded(self):
        """agent='' 的行（采集上线前历史存量 / 采集漏点）不进聚合——
        否则 14 万历史空行淹没真实识别率。agent='unknown'（分类器跑过未命中）保留。"""
        rows = [
            _row(timestamp=NOW - 50, agent="claude-code", ua="cc"),
            _row(timestamp=NOW - 50, agent="", ua=""),          # 历史存量
            _row(timestamp=NOW - 50, agent="", ua=""),          # 历史存量
            _row(timestamp=NOW - 50, agent="unknown", ua="curl/8"),  # 采集后未命中
        ]
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, {}, now=NOW)
        # claude-code 1 + unknown 1 = 2（两条历史空行不计）
        assert out["window_requests"] == 2
        agents = {t["agent"] for t in out["totals"]}
        assert agents == {"claude-code", "unknown"}

    def test_empty_agent_with_ua_still_excluded(self):
        """agent='' 即使带 ua 也不计——agent='' 意味采集未跑，无可靠信号。"""
        rows = [_row(timestamp=NOW - 50, agent="", ua="something")]
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, {}, now=NOW)
        assert out["window_requests"] == 0


class TestPaletteForClaimed:
    def test_user_source_gets_palette_color(self):
        """source='user'（用户认领）的 agent 按 palette rank 取色，
        不再用 YAML 里的 #94a3b8 灰。builtin 保留品牌色。"""
        from inferfabric.agent_stats import PALETTE_SERIES
        rows = [
            _row(timestamp=NOW - 50, agent="claude-code", ua="cc"),
            _row(timestamp=NOW - 50, agent="my-claim", ua="mc"),
        ]
        meta = {
            "claude-code": {"name": "Claude Code", "color": "#d97757", "source": "builtin"},
            "my-claim": {"name": "my-claim", "color": "#94a3b8", "source": "user"},
        }
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, meta, now=NOW)
        cc = next(t for t in out["totals"] if t["agent"] == "claude-code")
        mc = next(t for t in out["totals"] if t["agent"] == "my-claim")
        assert cc["color"] == "#d97757"  # builtin 品牌色不变
        assert mc["color"] == PALETTE_SERIES[0]  # 用户认领走 palette[0]

    def test_palette_cycles_and_skips_amber(self):
        """多个 user agent 按 palette 系列槽 [0,2,3,4] 循环，跳过琥珀[1]（累计线专用槽）。"""
        from inferfabric.agent_stats import PALETTE_SERIES
        rows = [_row(timestamp=NOW - 50, agent=f"u{i}", ua=f"u{i}") for i in range(5)]
        meta = {f"u{i}": {"name": f"u{i}", "color": "#fff", "source": "user"} for i in range(5)}
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, meta, now=NOW)
        colors = {t["agent"]: t["color"] for t in out["totals"]}
        # 5 个 user agent → palette [0,2,3,4,0]（跳过 [1]=琥珀）
        assert colors["u0"] == PALETTE_SERIES[0]
        assert colors["u1"] == PALETTE_SERIES[1]  # = palette[2]
        assert "#b45309" not in colors.values()   # 琥珀不出现


class TestHistoricalBucket:
    def test_historical_displayed_not_in_unassigned(self):
        """historical 行在 totals 显示（token/cost 有统计意义），不进 unassigned。"""
        rows = [
            _row(timestamp=NOW - 50, agent="historical", ua=""),         # 历史无信号
            _row(timestamp=NOW - 50, agent="claude-code", ua="cc"),      # 正常
            _row(timestamp=NOW - 50, agent="unknown", ua="curl/8"),      # 待认领
        ]
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, {}, now=NOW)
        agents = {t["agent"]: t for t in out["totals"]}
        assert "historical" in agents
        assert agents["historical"]["name"] == "历史（无信号）"
        assert agents["historical"]["requests"] == 1
        # historical 不进 unassigned（没有 ua 可认领）
        uas = [u["ua"] for u in out["unassigned"]]
        assert "" not in uas
        assert "curl/8" in uas  # unknown 的 ua 仍进 unassigned

    def test_historical_not_pinned_top(self):
        """historical 不置顶（非待处理）；unknown 仍置顶。"""
        rows = [
            _row(timestamp=NOW - 50, agent="historical", ua=""),
            _row(timestamp=NOW - 50, agent="claude-code", ua="cc"),
            _row(timestamp=NOW - 50, agent="unknown", ua="x"),
        ]
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, {}, now=NOW)
        # unknown 置顶
        assert out["totals"][0]["agent"] == "unknown"
        # historical 不在第一位
        assert out["totals"][1]["agent"] != "historical" or out["totals"][0]["agent"] == "unknown"
