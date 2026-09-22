"""unit/proxy/test_metrics_axis.py — _metrics_axis(pm) 配置驱动 x 轴构建

覆盖：local+cloud 组装、同名去重（local 优先）、pm.cloud 缺失降级、
_handle_api_metrics 确实把 axis 传给 get_metrics。
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# 添加 _deps 路径
_deps = Path(__file__).parent.parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import importlib

import inferfabric.proxy.handler as handler_module
from inferfabric.proxy.handler import _metrics_axis


def _pm(model_names=("Qwen38-27B-TXT", "NI-Qwen38-27B-VL"),
        cloud_ids=("glm-5.1", "deepseek-v4-flash")):
    pm = MagicMock()
    pm.mgr._models = {n: SimpleNamespace(name=n) for n in model_names}
    pm.cloud.cloud_models = {c: SimpleNamespace() for c in cloud_ids}
    return pm


def _make_handler(monkeypatch, path="/"):
    """Create a ProxyHandler with minimal mocked state（与 test_handler.py 惯例一致）。"""
    monkeypatch.setenv("IFF_ADMIN_TOKEN", "")
    # Force re-import to pick up new _ADMIN_TOKEN
    importlib.reload(handler_module)
    ProxyHandler = handler_module.ProxyHandler

    h = ProxyHandler.__new__(ProxyHandler)
    h.headers = {}
    h.path = path
    h.command = "GET"
    h._send_json = MagicMock()
    h._read_body = MagicMock(return_value=None)
    h._serve_dashboard = MagicMock()
    return h


def test_axis_local_first_cloud_second():
    axis = _metrics_axis(_pm())
    assert ("Qwen38-27B-TXT", "local") in axis
    assert ("glm-5.1", "cloud") in axis
    assert axis[0][1] == "local"
    assert axis[-1][1] == "cloud"
    names = [n for n, _ in axis]
    assert len(names) == len(set(names))


def test_axis_dedup_local_wins():
    pm = _pm(model_names=("glm-5.1",), cloud_ids=("glm-5.1", "deepseek-v4-flash"))
    axis = _metrics_axis(pm)
    assert ("glm-5.1", "local") in axis
    assert ("glm-5.1", "cloud") not in axis
    assert len([a for a in axis if a[0] == "glm-5.1"]) == 1


def test_axis_cloud_none_degrades():
    pm = _pm()
    pm.cloud = None                      # 云端发现不可用 → 只剩 local，不抛
    axis = _metrics_axis(pm)
    assert all(src == "local" for _, src in axis)


def test_api_metrics_passes_axis(monkeypatch):
    """回归守卫：/api/metrics 必须把 axis_models 传进 get_metrics。"""
    pm = _pm()
    captured = {}

    def fake_get_metrics(window="24h", axis_models=None):
        captured["window"] = window
        captured["axis"] = axis_models
        return {}

    pm.metrics.get_metrics.side_effect = fake_get_metrics
    h = _make_handler(monkeypatch, path="/api/metrics?window=1h")
    h._handle_api_metrics(pm)
    assert captured["window"] == "1h"
    assert ("Qwen38-27B-TXT", "local") in (captured["axis"] or [])
