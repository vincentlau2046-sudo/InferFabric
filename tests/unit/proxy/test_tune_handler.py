# tests/unit/proxy/test_tune_handler.py
"""/admin/tune 端点：preview(GET) + apply(POST) + 路由注册。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
_deps = Path(__file__).parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))


def _make_handler_and_pm(model=None, apply_result=None):
    """构造 handler + 带真实模型注册表的 pm。"""
    from inferfabric.proxy.handler import ProxyHandler, _GET_ROUTES, _POST_ROUTES
    if model is None:
        from inferfabric.config import ModelConfig
        m = ModelConfig(name="Qwen38-27B-TXT", description="d", type="ninfer")
        m.presets = {"short-parallel": {"max_concurrency": 8}}
        m.yaml_path = str(Path("/tmp/Qwen38-27B-TXT.yaml"))
        model = m

    class FakeState:
        def __init__(self):
            self.switching = ""

        def set(self, k, v):
            self.switching = v

    class FakeMgr:
        def __init__(self):
            self._models = {model.name: model}
            self._proc = self
            self.mgr = self
            self.state = FakeState()
            self.calls = []

        def stop_service(self, n):
            self.calls.append(("stop", n))
            return {"status": "stopped"}

        def switch(self, n):
            self.calls.append(("switch", n))
            return {"status": "switched"}
    pm = FakeMgr()

    h = ProxyHandler.__new__(ProxyHandler)
    h.headers = {"Content-Type": "application/json"}
    h.rfile = __import__("io").BytesIO(json.dumps({"model": "Qwen38-27B-TXT"}).encode())
    h._resp_status = 200
    h._resp_headers = {}
    h._headers_locked = False
    sent = {}

    def _send_json(data, status=200, extra_headers=None):
        sent["data"] = data
        sent["status"] = status
    h._send_json = _send_json
    return h, pm, sent, _GET_ROUTES, _POST_ROUTES


def test_routes_registered():
    h, pm, sent, GET, POST = _make_handler_and_pm()
    assert "/admin/tune/preview" in GET
    assert "/admin/tune" in POST


def test_tune_preview_ok():
    from inferfabric.config import ModelConfig
    from inferfabric import tune
    import tempfile, os
    d = Path(tempfile.mkdtemp())
    y = d / "M.yaml"
    y.write_text("name: M\ntype: ninfer\nninfer:\n  port: 8007\n  kv_capacity: 600000\npresets:\n  p1:\n    max_concurrency: 8\n")
    from inferfabric.config import load_models
    m = load_models(d)["M"]
    h, pm, sent, GET, POST = _make_handler_and_pm(model=m)
    h.path = "/admin/tune/preview?model=M&preset=p1"
    h._handle_tune_preview(pm)
    assert sent["status"] == 200
    assert sent["data"]["preset"] == "p1"
    assert sent["data"]["after"]["max_concurrency"] == 8
    assert "before" in sent["data"] and "issues" in sent["data"]


def test_tune_preview_unknown_model():
    h, pm, sent, GET, POST = _make_handler_and_pm()
    h.path = "/admin/tune/preview?model=nope&preset=p1"
    h._handle_tune_preview(pm)
    assert sent["status"] == 404


def test_tune_apply_ok(monkeypatch):
    import tempfile
    from inferfabric import tune
    d = Path(tempfile.mkdtemp())
    y = d / "M.yaml"
    y.write_text("name: M\ntype: ninfer\nninfer:\n  port: 8007\n  kv_capacity: 600000\npresets:\n  p1:\n    max_concurrency: 8\n")
    monkeypatch.setattr(tune, "BASELINE_FILE", d / "tune_baselines.yaml")
    monkeypatch.setattr(tune, "IFF_DATA_DIR", d)
    from inferfabric.config import load_models
    m = load_models(d)["M"]
    h, pm, sent, GET, POST = _make_handler_and_pm(model=m)
    h.path = "/admin/tune"
    h._read_body = lambda: {"model": "M", "preset": "p1", "restart": False}
    h._handle_tune(pm)
    assert sent["status"] == 200
    assert sent["data"]["status"] in ("applied", "applied_restart_pending")
    assert sent["data"]["active_preset"] == "p1"
    assert m.active_preset == "p1"
    import yaml
    assert yaml.safe_load(y.read_text())["active_preset"] == "p1"


def test_tune_apply_missing_fields():
    h, pm, sent, GET, POST = _make_handler_and_pm()
    h.path = "/admin/tune"
    h._read_body = lambda: {"model": "Qwen38-27B-TXT"}  # 缺 preset
    h._handle_tune(pm)
    assert sent["status"] == 400


def test_tune_apply_unknown_model():
    h, pm, sent, GET, POST = _make_handler_and_pm()
    h.path = "/admin/tune"
    h._read_body = lambda: {"model": "nope", "preset": "p1"}
    h._handle_tune(pm)
    assert sent["status"] == 404