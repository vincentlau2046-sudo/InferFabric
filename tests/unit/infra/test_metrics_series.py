import sys, time
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

from inferfabric.metrics_aggregator import MetricsAggregator


def _mk():
    return MetricsAggregator(model_name_map={"qwen38": "Qwen38-27B-TXT"},
                             price_config={})


def _sample(model, ts, ttft, dur, out, status=200):
    return {"model": model, "timestamp": ts, "ttft_ms": ttft,
            "duration_ms": dur, "tokens_out": out, "tokens_in": 1,
            "status": status, "error": ""}


def test_series_buckets_and_p50():
    agg = _mk()
    t0 = time.time() - 7200          # 2h 内
    # 两个 1h 桶各塞样本：桶0 ttft=[100,200] → p50≈150；桶1 ttft=[300,400] → p50≈350
    agg._samples.append(_sample("qwen38", t0 + 1800, 100.0, 500.0, 10))   # 桶0
    agg._samples.append(_sample("qwen38", t0 + 1800, 200.0, 600.0, 12))   # 桶0
    agg._samples.append(_sample("qwen38", t0 + 5400, 300.0, 700.0, 8))    # 桶1
    agg._samples.append(_sample("qwen38", t0 + 5400, 400.0, 800.0, 9))    # 桶1
    r = agg.get_latency_series("hour", bucket_ms=3600000, top_n=5)
    assert r["bucket_ms"] == 3600000
    n_buckets = len(r["buckets"])
    s = r["series"]["Qwen38-27B-TXT"]
    assert len(s["ttft_p50"]) == n_buckets
    # 找非 None 的两个桶，验证 p50 计算
    vals = [v for v in s["ttft_p50"] if v is not None]
    assert len(vals) == 2
    assert min(vals) == 150.0 and max(vals) == 350.0
    assert s["source"] == "observed"    # 未给 source_of → 默认 observed


def test_series_tpot_and_exclusions():
    agg = _mk()
    t0 = time.time() - 3000  # 窗口内（-3600 会恰在边界外被排除）
    # 成功流式：ttft=100 dur=400 out=101 → tpot=(400-100)/100=3.0
    agg._samples.append(_sample("m1", t0, 100.0, 400.0, 101))
    # 失败样本不进 ttft/tpot
    agg._samples.append(_sample("m1", t0, 50.0, 60.0, 5, status=500))
    r = agg.get_latency_series("minute", bucket_ms=3600000, top_n=5)
    s = r["series"]["m1"]
    tp = [v for v in s["tpot_p50"] if v is not None]
    assert tp == [3.0]
    # 失败样本被排除 → ttft_n 里对应桶只有 1 个成功样本
    assert sum(s["ttft_n"]) == 1


def test_series_top_n_and_order():
    agg = _mk()
    t0 = time.time() - 3000  # 窗口内（-3600 会恰在边界外被排除）
    for i in range(5):
        agg._samples.append(_sample("hot", t0, 100.0, 400.0, 10))
    for i in range(3):
        agg._samples.append(_sample("mid", t0, 100.0, 400.0, 10))
    for i in range(1):
        agg._samples.append(_sample("cold", t0, 100.0, 400.0, 10))
    r = agg.get_latency_series("minute", bucket_ms=3600000, top_n=2)
    keys = list(r["series"].keys())
    assert keys == ["hot", "mid"]      # 请求数降序，前 2
    assert r["total_models"] == 2
    assert r["series"]["hot"]["requests"] == 5


def test_series_empty_window():
    agg = _mk()
    r = agg.get_latency_series("minute", bucket_ms=3600000, top_n=5)
    assert r["series"] == {}
    assert r["total_models"] == 0
    # 桶轴 = 墙钟对齐范围（即使无样本也返回完整桶轴，空桶由 None 表示）
    assert len(r["buckets"]) >= 1


def test_series_excludes_zero_ttft_models():
    """v6.0: 404/unknown_model 刷量（0 条 ttft）不占位，零数据模型不进 series。"""
    agg = _mk()
    t0 = time.time() - 3000
    for _ in range(50):                                   # 404 刷量：请求多、零 ttft
        agg._samples.append(_sample("claude-404", t0, None, 2.0, 0, status=404))
    agg._samples.append(_sample("m1", t0, 100.0, 400.0, 10))
    agg._samples.append(_sample("m2", t0, 200.0, 500.0, 10))
    r = agg.get_latency_series("minute", bucket_ms=3600000, top_n=5)
    assert "claude-404" not in r["series"]
    assert list(r["series"].keys()) == ["m1", "m2"]   # 平局按名称升序
    assert r["total_models"] == 2


def test_series_all_active_models_under_cap():
    """v6.0: 在用模型 ≤5 个 → 全量返回（不强行取 4）；>5 个 → 按请求数取前 5。"""
    agg = _mk()
    t0 = time.time() - 3000
    for i, name in enumerate(["a", "b", "c"]):
        for _ in range(3 - i):
            agg._samples.append(_sample(name, t0, 100.0, 400.0, 10))
    r = agg.get_latency_series("minute", bucket_ms=3600000, top_n=5)
    assert list(r["series"].keys()) == ["a", "b", "c"]   # 3 个在用全返回

    agg2 = _mk()
    for i, name in enumerate(["h1", "h2", "h3", "h4", "h5", "h6", "h7"]):
        for _ in range(7 - i):
            agg2._samples.append(_sample(name, t0, 100.0, 400.0, 10))
    r2 = agg2.get_latency_series("minute", bucket_ms=3600000, top_n=5)
    assert list(r2["series"].keys()) == ["h1", "h2", "h3", "h4", "h5"]
    assert r2["total_models"] == 5


def test_series_source_of():
    agg = _mk()
    t0 = time.time() - 3000  # 窗口内（-3600 会恰在边界外被排除）
    agg._samples.append(_sample("m1", t0, 100.0, 400.0, 10))
    r = agg.get_latency_series("minute", bucket_ms=3600000, top_n=5,
                               source_of={"m1": "local"})
    assert r["series"]["m1"]["source"] == "local"
