# tests/unit/proxy/test_tune_handler.py
"""/admin/tune 端点：preview(GET) + apply(POST) + 路由注册。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
_deps = Path(__file__).parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))


def _make_handler_and_pm(model=None, models_dir=None):
    """构造 handler + 带真实模型注册表的 pm（models_dir → tune 请求前重读注册表，D1）。"""
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

        def clear_manual_stop(self, name):
            pass

    class FakeMgr:
        def __init__(self):
            self._models = {model.name: model}
            self.models_dir = models_dir  # None → 回退内存注册表
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
    from inferfabric import config as cfgmod
    d = Path(tempfile.mkdtemp())
    y = d / "M.yaml"
    y.write_text("name: M\ntype: ninfer\nninfer:\n  port: 8007\n  kv_capacity: 600000\npresets:\n  p1:\n    max_concurrency: 8\n")
    applied = d / "active_scenarios.yaml"
    monkeypatch.setattr(tune, "APPLIED_FILE", applied)
    monkeypatch.setattr(cfgmod, "APPLIED_SCENARIOS_FILE", applied)
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
    # 模型 YAML 字节级未动；状态全在应用层
    import yaml
    raw = yaml.safe_load(y.read_text())
    assert "active_preset" not in raw
    assert "max_concurrency" not in raw["ninfer"], "场景值不得写进模型 YAML"
    ap = yaml.safe_load(applied.read_text())
    assert ap["M"]["active_preset"] == "p1"
    assert ap["M"]["overrides"]["max_concurrency"] == 8


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


def test_tune_apply_already_default_is_200(monkeypatch):
    """default 无条目 no-op → HTTP 200（不是 500/400）。"""
    import tempfile
    from inferfabric import tune
    from inferfabric import config as cfgmod
    d = Path(tempfile.mkdtemp())
    y = d / "M.yaml"
    y.write_text("name: M\ntype: ninfer\nninfer:\n  port: 8007\n  kv_capacity: 600000\npresets:\n  p1:\n    max_concurrency: 8\n")
    applied = d / "active_scenarios.yaml"
    monkeypatch.setattr(tune, "APPLIED_FILE", applied)
    monkeypatch.setattr(cfgmod, "APPLIED_SCENARIOS_FILE", applied)
    from inferfabric.config import load_models
    m = load_models(d)["M"]
    h, pm, sent, GET, POST = _make_handler_and_pm(model=m)
    h.path = "/admin/tune"
    h._read_body = lambda: {"model": "M", "preset": "default", "restart": False}
    h._handle_tune(pm)
    assert sent["status"] == 200
    assert sent["data"]["status"] == "already_default"


def test_tune_scenarios_route_registered():
    h, pm, sent, GET, POST = _make_handler_and_pm()
    assert "/admin/tune/scenarios" in GET


def test_tune_scenarios_d5_reads_applied_layer(monkeypatch):
    """D5：active 直读应用层文件（≡ 容器实际值），不读代理内存。"""
    import tempfile
    from inferfabric import tune
    from inferfabric import config as cfgmod
    d = Path(tempfile.mkdtemp())
    y = d / "M.yaml"
    y.write_text("name: M\ntype: ninfer\nninfer:\n  port: 8007\n  kv_capacity: 600000\npresets:\n  p1:\n    max_concurrency: 8\n")
    applied = d / "active_scenarios.yaml"
    monkeypatch.setattr(tune, "APPLIED_FILE", applied)
    monkeypatch.setattr(cfgmod, "APPLIED_SCENARIOS_FILE", applied)
    from inferfabric.config import load_models
    m = load_models(d)["M"]
    h, pm, sent, GET, POST = _make_handler_and_pm(model=m, models_dir=str(d))

    h.path = "/admin/tune/scenarios?model=M"
    h._handle_tune_scenarios(pm)
    assert sent["status"] == 200
    # choices 含一等 default；active 无条目 = default
    assert sent["data"]["choices"] == ["default", "p1"]
    assert sent["data"]["active"] == "default"

    # 应用 p1 后（另一入口/CLI 写文件），端点立即反映（≡ 磁盘）
    applied.write_text("M:\n  active_preset: p1\n  overrides:\n    max_concurrency: 8\n")
    h._handle_tune_scenarios(pm)
    assert sent["data"]["active"] == "p1"


def test_tune_scenarios_unknown_model():
    h, pm, sent, GET, POST = _make_handler_and_pm()
    h.path = "/admin/tune/scenarios?model=nope"
    h._handle_tune_scenarios(pm)
    assert sent["status"] == 404


def test_tune_apply_failed_restart_maps_500(monkeypatch):
    """restart 失败（文件层已回滚）→ HTTP 500 + status=failed_*（≠ 200）。"""
    import tempfile
    import yaml
    from inferfabric import tune
    from inferfabric import config as cfgmod
    d = Path(tempfile.mkdtemp())
    y = d / "M.yaml"
    y.write_text("name: M\ntype: ninfer\nninfer:\n  port: 8007\n  kv_capacity: 600000\npresets:\n  p1:\n    max_concurrency: 8\n")
    applied = d / "active_scenarios.yaml"
    monkeypatch.setattr(tune, "APPLIED_FILE", applied)
    monkeypatch.setattr(cfgmod, "APPLIED_SCENARIOS_FILE", applied)
    from inferfabric.config import load_models
    m = load_models(d)["M"]
    h, pm, sent, GET, POST = _make_handler_and_pm(model=m)

    # 第一次重启（新配置）崩溃 → 回滚重启（第二次）成功
    def flaky_switch(n):
        pm.calls.append(("switch", n))
        if len([c for c in pm.calls if c[0] == "switch"]) == 1:
            raise RuntimeError("container exploded")
        return {"status": "switched"}
    pm.switch = flaky_switch

    h.path = "/admin/tune"
    h._read_body = lambda: {"model": "M", "preset": "p1", "restart": True}
    h._handle_tune(pm)
    assert sent["status"] == 500
    assert sent["data"]["status"] == "failed_rolled_back"
    assert yaml.safe_load(applied.read_text()) == {}, "文件层已回滚（单写者 D2 不变量）"


def test_tune_apply_restarted_maps_200(monkeypatch):
    """无条目 + 模型运行中 + apply(default) → restarted → HTTP 200。"""
    import tempfile
    from inferfabric import tune
    from inferfabric import config as cfgmod
    d = Path(tempfile.mkdtemp())
    y = d / "M.yaml"
    y.write_text("name: M\ntype: ninfer\nninfer:\n  port: 8007\n  kv_capacity: 600000\npresets:\n  p1:\n    max_concurrency: 8\n")
    applied = d / "active_scenarios.yaml"
    monkeypatch.setattr(tune, "APPLIED_FILE", applied)
    monkeypatch.setattr(cfgmod, "APPLIED_SCENARIOS_FILE", applied)
    from inferfabric.config import load_models
    m = load_models(d)["M"]
    h, pm, sent, GET, POST = _make_handler_and_pm(model=m)
    pm.active_services = ("M",)

    h.path = "/admin/tune"
    h._read_body = lambda: {"model": "M", "preset": "default", "restart": True}
    h._handle_tune(pm)
    assert sent["status"] == 200
    assert sent["data"]["status"] == "restarted"