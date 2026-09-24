"""unit/infra/test_metrics_aggregator.py — MetricsAggregator v6.0

覆盖：TPOT 推导（零 schema 变更，从 ttft_ms/duration_ms/tokens_out 计算）、
配置驱动 x 轴（axis_models 零请求占位 + source 标记 + 稳定排序）。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from inferfabric.metrics_aggregator import MetricsAggregator


class _Entry:
    """RequestLog duck-type（record() 是 duck-typed）。"""
    def __init__(self, model, status=200, ttft=None, dur=0.0,
                 tin=0, tout=0, route="local", error=None, ts=None):
        self.model = model
        self.status = status
        self.ttft_ms = ttft
        self.duration_ms = dur
        self.tokens_in = tin
        self.tokens_out = tout
        self.route = route
        self.cloud_provider = None
        self.error = error
        self.timestamp = ts or time.time()


def _agg_with(entries):
    agg = MetricsAggregator()
    for e in entries:
        agg.record(e)
    return agg


def test_tpot_derivation():
    """tpot_ms = (duration_ms - ttft_ms) / (tokens_out - 1)，逐请求后取分位。"""
    agg = _agg_with([
        _Entry("m", ttft=1000.0, dur=5000.0, tout=5),    # (5000-1000)/4 = 1000
        _Entry("m", ttft=2000.0, dur=10000.0, tout=5),   # (10000-2000)/4 = 2000
    ])
    m = agg.get_metrics("24h")["models"]["m"]
    assert m["tpot_p50"] == 1500.0
    assert m["tpot_p95"] == 1950.0      # quantile 线性插值：1000 + 0.95*1000
    assert m["tpot_p99"] == 1990.0
    assert m["tpot_samples"] == 2


def test_tpot_excludes_nonstream_short_and_failed():
    """非流式（duration<=ttft）、tokens_out<2、失败请求不进 TPOT 分位。"""
    agg = _agg_with([
        _Entry("m", ttft=5000.0, dur=4000.0, tout=5),    # duration < ttft → 排除
        _Entry("m", ttft=100.0, dur=200.0, tout=1),      # tokens_out<2 → 排除
        _Entry("m", status=500, ttft=100.0, dur=300.0, tout=3),  # 失败 → 排除
    ])
    m = agg.get_metrics("24h")["models"]["m"]
    assert "tpot_p50" not in m
    assert m["tpot_samples"] == 0


def test_tpot_floor_excludes_near_zero():
    """近零 TPOT（非流式 ttft≈duration → gap 极小）是无意义值：不统计、当空值处理。"""
    agg = _agg_with([
        _Entry("m", ttft=5000.0, dur=5000.5, tout=300),   # 0.5/299 ≈ 0.0017 < 1.0 → 排除
        _Entry("m", ttft=1000.0, dur=1030.0, tout=3),     # 30/2 = 15.0 → 保留
    ])
    m = agg.get_metrics("24h")["models"]["m"]
    assert m["tpot_samples"] == 1
    assert m["tpot_p50"] == 15.0


def test_latency_series_near_zero_tpot_bucket_is_none():
    """桶内 TPOT 全是近零值 → 该桶 None（前端 connectNulls 亮点直连），不产出 0.0 点。"""
    now = time.time()
    agg = _agg_with([
        _Entry("m", ttft=5000.0, dur=5000.35, tout=300, ts=now - 60),  # ≈0.0012 → 排除
        _Entry("m", ttft=6000.0, dur=6000.5, tout=400, ts=now - 30),   # ≈0.0013 → 排除
    ])
    s = agg.get_latency_series("hour", bucket_ms=3600000)["series"]["m"]
    assert all(v is None for v in s["tpot_p50"])
    assert all(v == 0 for v in s["tpot_n"])
    # TTFT 序列不受影响（样本有 ttft>0）
    assert any(v is not None for v in s["ttft_p50"])


def test_ttft_samples_counted():
    """ttft_samples = 有 ttft 的成功请求数；ttft 为 None/0 的不计。"""
    agg = _agg_with([
        _Entry("m", ttft=100.0, dur=500.0, tout=3),
        _Entry("m", ttft=None, dur=500.0, tout=3),
        _Entry("m", ttft=0.0, dur=500.0, tout=3),
        _Entry("m", status=502, ttft=100.0, dur=500.0),
    ])
    m = agg.get_metrics("24h")["models"]["m"]
    assert m["ttft_samples"] == 1


def test_axis_zero_entries_and_stable_order():
    """axis_models 中无请求的模型补零占位；键序 = requests 降序 → 名称升序。"""
    agg = _agg_with([
        _Entry("alpha", ttft=100.0, dur=500.0, tout=3),
        _Entry("alpha", ttft=200.0, dur=600.0, tout=3),
    ])
    res = agg.get_metrics("24h", axis_models=[("alpha", "local"), ("beta", "cloud")])
    assert list(res["models"].keys()) == ["alpha", "beta"]
    beta = res["models"]["beta"]
    assert beta["requests"] == 0 and beta["success"] == 0 and beta["fail"] == 0
    assert beta["source"] == "cloud"
    assert res["models"]["alpha"]["source"] == "local"


def test_observed_model_marked_observed():
    """窗口内出现但不在 axis 的模型 → source=observed（不丢、不冒充已配置）。"""
    agg = _agg_with([_Entry("ghost", ttft=100.0, dur=500.0, tout=3)])
    res = agg.get_metrics("24h", axis_models=[("alpha", "local")])
    assert res["models"]["ghost"]["source"] == "observed"
    # 无请求的 axis 模型也在
    assert res["models"]["alpha"]["requests"] == 0


def test_no_axis_keeps_all_sampled_models():
    """不传 axis_models 时行为向后兼容：models 只含窗口内出现过的模型，source=observed。"""
    agg = _agg_with([_Entry("solo", ttft=100.0, dur=500.0, tout=3)])
    res = agg.get_metrics("24h")
    assert list(res["models"].keys()) == ["solo"]
    assert res["models"]["solo"]["source"] == "observed"


def test_window_cutoff_unchanged():
    """1h 窗口过滤旧样本（回归守卫）。"""
    now = time.time()
    agg = _agg_with([
        _Entry("m", ttft=100.0, dur=500.0, tout=3, ts=now - 7200),   # 2h 前
        _Entry("m", ttft=200.0, dur=600.0, tout=3, ts=now - 60),     # 1min 前
    ])
    m1h = agg.get_metrics("1h")["models"]["m"]
    assert m1h["requests"] == 1
    assert m1h["ttft_p50"] == 200.0


def test_e2e_tps_derivation():
    """v6.1: E2E 端到端速率 = tokens_out/(duration_ms/1000)，不依赖 ttft（流式/非流式通用）。"""
    agg = _agg_with([
        _Entry("m", ttft=1000.0, dur=4000.0, tout=40),   # 40/4s = 10
        _Entry("m", ttft=None, dur=2000.0, tout=20),     # 无 ttft 也可算：20/2s = 10
    ])
    m = agg.get_metrics("24h")["models"]["m"]
    assert m["e2e_tps_samples"] == 2
    assert m["e2e_tps_p50"] == 10.0
    assert m["e2e_tps_p95"] == 10.0


def test_e2e_tps_excludes_failed_zero_duration_and_no_tokens():
    """失败 / 零时长 / 零输出样本不进 E2E 分位。"""
    agg = _agg_with([
        _Entry("m", status=500, dur=1000.0, tout=10),   # 失败 → 排除
        _Entry("m", dur=0.0, tout=10),                   # 零时长 → 排除
        _Entry("m", dur=1000.0, tout=0),                 # 零输出 → 排除
        _Entry("m", ttft=100.0, dur=1000.0, tout=10),   # 10/1s = 10 → 保留
    ])
    m = agg.get_metrics("24h")["models"]["m"]
    assert m["e2e_tps_samples"] == 1
    assert m["e2e_tps_p50"] == 10.0


def test_rps_and_avg_out_len():
    """v6.1: rps = 请求数/窗口秒（24h=86400）；avg_out_len = Σtokens_out/请求数。"""
    n = 864  # 24h 内 864 请求 → 0.01 req/s
    agg = _agg_with([_Entry("m", ttft=100.0, dur=1000.0, tout=10) for _ in range(n)])
    m = agg.get_metrics("24h")["models"]["m"]
    assert m["requests"] == n
    assert m["rps"] == 0.01
    assert m["avg_out_len"] == 10.0


def test_rps_zero_padding_and_all_window():
    """无请求 axis 模型 → rps/avg_out_len 零占位；window="all"（win=inf）→ rps=0.0。"""
    agg = _agg_with([_Entry("m", ttft=100.0, dur=1000.0, tout=10)])
    res = agg.get_metrics("24h", axis_models=[("m", "local"), ("beta", "cloud")])
    beta = res["models"]["beta"]
    assert beta["rps"] == 0.0
    assert beta["avg_out_len"] == 0.0
    assert "e2e_tps_p50" not in beta
    assert agg.get_metrics("all")["models"]["m"]["rps"] == 0.0


def test_tpot_floor_1ms_excludes_artifacts():
    """v6.1: floor 0.005→1.0 — 历史 0.2–0.3ms/token 伪值（8k–50M t/s 级）被排除，
    真实 decode（~47ms/token）保留。"""
    agg = _agg_with([
        _Entry("m", ttft=4900.0, dur=5000.0, tout=300),   # 100/299 ≈ 0.33 < 1.0 → 伪值排除
        _Entry("m", ttft=1000.0, dur=15200.0, tout=300),  # 14200/299 ≈ 47.49 → 保留
    ])
    m = agg.get_metrics("24h")["models"]["m"]
    assert m["tpot_samples"] == 1
    assert m["tpot_p50"] == 47.49
