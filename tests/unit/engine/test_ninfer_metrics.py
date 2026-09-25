"""
unit/engine/test_ninfer_metrics.py — NInferAdapter.fetch_engine_metrics 引擎 /metrics 权威值

覆盖:
  - 引擎 GET /metrics（Prometheus 文本）经 VllmMetricsCollector(prefix="ninfer_")
    取 KV / running batch / 吞吐（EMA），覆盖日志派生兜底
  - /metrics 不可达时静默回退到日志派生值（KV 缺省，seq/TTFT/TPOT 仍在）
  - 日志空 + 无 /metrics → 仅返回 sleep_state
"""

import time

import pytest
from unittest.mock import MagicMock

from inferfabric.config import ModelConfig, NInferConfig
from inferfabric.engine_adapter.ninfer import NInferAdapter
from inferfabric.prometheus import VllmMetricsCollector

PORT = 8017  # 测试专用端口：避免污染真实 VllmMetricsCollector 的类级 EMA 状态


def _metrics_text(kp=0.42, running=3, tokens=1000):
    return (
        "# HELP ninfer_kv_cache_usage_perc Fraction of device KV pages in use (0.0-1.0).\n"
        "# TYPE ninfer_kv_cache_usage_perc gauge\n"
        f"ninfer_kv_cache_usage_perc {kp}\n"
        "# HELP ninfer_num_requests_running Requests currently running on the device.\n"
        "# TYPE ninfer_num_requests_running gauge\n"
        f"ninfer_num_requests_running {running}\n"
        "# HELP ninfer_generation_tokens_total Monotonic count of generated decode tokens.\n"
        "# TYPE ninfer_generation_tokens_total counter\n"
        f"ninfer_generation_tokens_total {tokens}\n"
    )


def _model(log_file):
    cfg = NInferConfig(port=PORT, max_concurrency=4, log_file=str(log_file))
    return ModelConfig(name="t", description="d", type="ninfer", ninfer=cfg)


def _patch_urlopen(monkeypatch, text=None, raise_exc=False):
    """替换 urllib.request.urlopen（adapter 在函数体内 import，故此处 patch 生效）。"""

    def _factory(*args, **kwargs):
        if raise_exc:
            raise ConnectionError("no /metrics endpoint")
        resp = MagicMock()
        resp.read.return_value = (text or "").encode("utf-8")
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    monkeypatch.setattr("urllib.request.urlopen", _factory)


@pytest.fixture(autouse=True)
def _clean_collector():
    # 类级 EMA 状态 keyed by port；清掉本测试端口，避免跨用例污染。
    yield
    VllmMetricsCollector.gen_counters.pop(PORT, None)
    VllmMetricsCollector.throughput_ema.pop(PORT, None)


def test_engine_metrics_override_log_fallback(tmp_path, monkeypatch):
    log = tmp_path / "ninfer.log"
    log.write_text("")  # 空日志 → 引擎 /metrics 是 KV/Batch/Throughput 的唯一来源
    _patch_urlopen(monkeypatch, text=_metrics_text(kp=0.42, running=3, tokens=1000))
    # 预置 EMA 前值，使单次调用即产出 throughput（首个样本无前值 → 无法求 delta）。
    VllmMetricsCollector.gen_counters[PORT] = (time.time() - 10.0, 500)

    result = NInferAdapter().fetch_engine_metrics(_model(log))

    assert result is not None
    assert result["kv_cache_usage_perc"] == 42.0  # 0.42 * 100
    assert result["running_batch"] == 3           # live running gauge
    assert result["throughput"] is not None and result["throughput"] > 0  # EMA 平滑
    assert result["max_batch"] == 4


def test_engine_metrics_absent_falls_back_to_log(tmp_path, monkeypatch):
    log = tmp_path / "ninfer.log"
    # 一条 req#done 行 → seq/TTFT/TPOT 仍由日志派生；/metrics 不可达 → KV 缺省。
    log.write_text("req#7 done prompt 1,024 | output 512 TTFT 250ms decode 18.0k tok/s\n")
    _patch_urlopen(monkeypatch, raise_exc=True)

    result = NInferAdapter().fetch_engine_metrics(_model(log))

    assert result is not None
    assert "kv_cache_usage_perc" not in result  # 回退：无引擎值
    assert result["seq_length"] == 1024 + 512   # 日志派生 seq 仍在
    assert result["max_batch"] == 4


def test_no_metrics_and_empty_log_returns_sleep_only(tmp_path, monkeypatch):
    log = tmp_path / "ninfer.log"
    log.write_text("")
    _patch_urlopen(monkeypatch, raise_exc=True)

    result = NInferAdapter().fetch_engine_metrics(_model(log))

    assert result == {"sleep_state": 0}
