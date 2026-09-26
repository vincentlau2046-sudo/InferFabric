# tests/unit/test_tune.py
"""iff tune 场景预设调优 — 引擎无关编排器单元测试。

覆盖：preview diff / 白名单过滤 / 引擎上限钳制 / KV 池顶校验 / MTP 保护 /
baseline 快照 / default 回滚 / YAML 写回 / restart 走 GPU 状态机 / 引擎无感知。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
_deps = Path(__file__).parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

from inferfabric import tune
from inferfabric.config import load_models, ModelConfig
from inferfabric.engine_adapter import get_adapter, register
from inferfabric.engine_adapter.base import EngineAdapter


# ── fixtures ─────────────────────────────────────────────────────

FIXTURE = """\
name: Qwen38-27B-TXT
description: 测试模型
type: ninfer
quantization: NVFP4
peak_vram_mb: 30500
ninfer:
  served_name: Qwen38-27B-TXT
  weight_path: ~/models/x.ninfer
  docker_image: ninfer:auto
  port: 8007
  container_name: iff-test-ninfer
  max_context: 262144
  kv_capacity: 600000
  max_concurrency: 6
  default_max_tokens: 32000
  prefill_chunk: 2048
  enable_mtp: true
  draft_tokens: 1
presets:
  # 内联场景（与侧车同名 → 侧车 small 覆盖本定义，仅验证覆盖语义）
  small:
    max_concurrency: 1
    max_context: 65536
  short-parallel:
    max_concurrency: 8
    max_context: 98304
    default_max_tokens: 16384
    prefill_chunk: 2048
    enable_mtp: true
    draft_tokens: 1
  big:
    max_concurrency: 5
    max_context: 204800
    default_max_tokens: 32768
    prefill_chunk: 2048
    enable_mtp: true
    draft_tokens: 2
  wild:
    max_concurrency: 9
    weight_path: /evil
    prefill_chunk: 1000
    max_context: 98304
    default_max_tokens: 16384
    enable_mtp: true
    draft_tokens: 1
  # 池顶 < 固定 kv_capacity → 「满载余量」提示（非超卖）
  tiny:
    max_concurrency: 2
    max_context: 16384
    default_max_tokens: 8192
    prefill_chunk: 2048
    enable_mtp: true
    draft_tokens: 1
"""


# 侧车（手动配置单一真相源）：small 场景覆盖模型 YAML 内联同名场景。
# 注意：kv_capacity 不是场景字段（模型固定物理池，在 ninfer: 块），不在此出现。
SIDECAR = """\
Qwen38-27B-TXT:
  small:
    max_concurrency: 4
    max_context: 204800
    default_max_tokens: 32768
    prefill_chunk: 2048
    enable_mtp: true
    draft_tokens: 1
"""


@pytest.fixture
def model(tmp_path, monkeypatch):
    y = tmp_path / "Qwen38-27B-TXT.yaml"
    y.write_text(FIXTURE)
    (tmp_path / "scenarios.yaml").write_text(SIDECAR)
    monkeypatch.setattr(tune, "BASELINE_FILE", tmp_path / "tune_baselines.yaml")
    monkeypatch.setattr(tune, "IFF_DATA_DIR", tmp_path)
    models = load_models(tmp_path)
    return models["Qwen38-27B-TXT"]


class FakeState:
    def __init__(self):
        self.switching = ""

    def clear_manual_stop(self, name):
        pass

    def record_manual_stop(self, name):
        pass

    def set(self, k, v):
        self.switching = v


class FakeMgr:
    """模拟 ModelManager：adapter 经 _proc.mgr 看到的就是自己。"""
    def __init__(self):
        self._proc = self
        self.mgr = self
        self.state = FakeState()
        self.calls = []

    def stop_service(self, name):
        self.calls.append(("stop", name))
        return {"status": "stopped", "model": name}

    def switch(self, name):
        self.calls.append(("switch", name))
        return {"status": "switched", "message": "ok"}


# ── preview ──────────────────────────────────────────────────────

def test_preview_short_parallel_diff(model):
    p = tune.preview(model, "short-parallel")
    assert p["preset"] == "short-parallel"
    assert p["before"]["max_concurrency"] == 6
    assert p["after"]["max_concurrency"] == 8
    assert p["after"]["max_context"] == 98304
    assert "kv_capacity" not in p["after"], "kv 是固定物理池，不是场景字段"
    # 池顶 = 8×⌈98304/64⌉×64 = 786432；固定 kv 600000 → 超卖 (786432−600000)/786432 = 23.7%
    assert not [i for i in p["issues"] if i.startswith("❌")]
    assert any("超卖 23.7%" in i for i in p["issues"])


def test_sidecar_scenario_overrides_inline(model):
    """侧车 scenarios.yaml 的同名场景覆盖模型 YAML 内联定义。"""
    assert model.presets["small"]["max_concurrency"] == 4  # 侧车值（非内联 1）
    p = tune.preview(model, "small")
    assert p["after"]["max_concurrency"] == 4
    assert p["after"]["max_context"] == 204800
    # 池顶 = 4×204800 = 819200；超卖 (819200−600000)/819200 = 26.8%
    assert any("超卖 26.8%" in i for i in p["issues"])


def test_preview_tiny_shows_surplus(model):
    """池顶 < 固定 kv_capacity → 展示满载余量（非超卖）。"""
    p = tune.preview(model, "tiny")
    # 池顶 = 2×16384 = 32768 < kv 600000 → 余量 (1−32768/600000) = 94.5%
    assert not [i for i in p["issues"] if i.startswith("❌")]
    assert any("满载余量 94.5%" in i for i in p["issues"])


def test_preview_wild_clamp_and_whitelist(model):
    """越界钳制 + 白名单外字段忽略（weight_path 不应污染）。"""
    p = tune.preview(model, "wild")
    assert p["after"]["max_concurrency"] == 8        # 9 → 8
    assert p["after"]["prefill_chunk"] == 896        # 1000 → 128 倍数
    assert "weight_path" not in p["after"]
    assert any("钳制" in n or "上限" in n for n in p["clamp_notes"])


def test_preview_mtp_warning(model):
    p = tune.preview(model, "big")
    assert any("draft" in i.lower() and "实测" in i for i in p["issues"])


def test_preview_unknown_preset(model):
    with pytest.raises(tune.TuneError):
        tune.preview(model, "nope")


def test_preview_unsupported_engine_model(tmp_path, monkeypatch):
    """适配器未声明 scenario_fields → 明确报"不支持"。"""
    class MinimalAdapter:
        engine_type = "minimal"
        scenario_fields = lambda self, m: []   # 继承思路：空白名单=不支持
        engine_caps = lambda self, m: {}
        validate_scenario = lambda self, m, v: []
        restart = lambda self, m: {"status": "ok"}

    register("minimal", MinimalAdapter)

    class Cfg:  # 引擎块
        gpu_mem = 4096
        max_batch = 2

    m = ModelConfig(name="mini", description="d", type="minimal")
    m.mycfg = Cfg()
    monkeypatch.setitem(ModelConfig._ENGINE_ATTR, "minimal", "mycfg")
    m.presets = {"p1": {"max_batch": 4}}
    m.yaml_path = str(tmp_path / "mini.yaml")
    with pytest.raises(tune.TuneError, match="不支持"):
        tune.preview(m, "p1")


# ── dry / apply / write-back ───────────────────────────────────

def test_apply_dry_no_write(model, tmp_path):
    path = Path(model.yaml_path)
    before_bytes = path.read_bytes()
    r = tune.apply(model, "short-parallel", dry=True)
    assert r["status"] == "preview"
    assert path.read_bytes() == before_bytes, "dry 不应写盘"
    assert model.active_preset == ""
    assert not (tmp_path / "tune_baselines.yaml").exists(), "dry 不应建基线"


def test_apply_no_restart_writes_yaml_and_baseline(model, tmp_path):
    r = tune.apply(model, "short-parallel", restart=False)
    assert r["status"] == "applied_restart_pending"
    assert model.active_preset == "short-parallel"
    assert model.ninfer.max_concurrency == 8
    # YAML 写回（只写场景字段；固定 kv_capacity 保持原值）
    import yaml as _y
    raw = _y.safe_load(Path(model.yaml_path).read_text())
    assert raw["ninfer"]["max_concurrency"] == 8
    assert raw["ninfer"]["kv_capacity"] == 600000, "固定物理池不被场景写回"
    assert raw["active_preset"] == "short-parallel"
    # 基线快照 = 应用前 live 值（只含场景字段）
    bl = _y.safe_load((tmp_path / "tune_baselines.yaml").read_text())
    assert bl["Qwen38-27B-TXT"]["max_concurrency"] == 6
    assert "kv_capacity" not in bl["Qwen38-27B-TXT"], "kv 不是场景字段，不进基线"


def test_apply_restart_goes_through_gpu_state_machine(model, tmp_path):
    """restart 走 stop_service→clear_manual_stop→switch (503 Switch Guard)。"""
    mgr = FakeMgr()
    r = tune.apply(model, "short-parallel", restart=True, mgr=mgr)
    assert r["status"] == "applied"
    assert r["restart"]["status"] == "switched"
    assert mgr.calls == [("stop", "Qwen38-27B-TXT"), ("switch", "Qwen38-27B-TXT")]
    assert mgr.state.switching == "", "switch 后应清空 switching_target"


def test_apply_default_rollback(model, tmp_path):
    tune.apply(model, "short-parallel", restart=False)
    assert model.ninfer.max_concurrency == 8
    r = tune.apply(model, "default", restart=False)
    assert r["preset"] == "default"
    assert model.active_preset == ""
    assert model.ninfer.max_concurrency == 6
    assert model.ninfer.kv_capacity == 600000
    import yaml as _y
    raw = _y.safe_load(Path(model.yaml_path).read_text())
    assert raw["ninfer"]["max_concurrency"] == 6
    assert "active_preset" not in raw


def test_apply_default_without_baseline(model, tmp_path):
    with pytest.raises(tune.TuneError, match="基线"):
        tune.apply(model, "default", restart=False)


def test_apply_oversell_not_blocked(model, tmp_path):
    """超卖是预期行为（preempt 兜底）——⚠ 告警不阻塞 apply。"""
    r = tune.apply(model, "short-parallel", restart=False)
    assert r["status"] == "applied_restart_pending"
    assert any("超卖" in i for i in r["diff"]["issues"])


def test_apply_restart_rollback_on_failure(model, tmp_path):
    """启动失败 → 还原 YAML + 重启旧配置。"""
    class FailMgr(FakeMgr):
        def switch(self, name):
            self.calls.append(("switch-fail", name))
            return {"status": "error", "message": "container crashed"}

    mgr = FailMgr()
    r = tune.apply(model, "short-parallel", restart=True, mgr=mgr)
    assert r["status"] in ("failed_rolled_back", "failed_rollback_failed")
    # YAML 已被还原
    import yaml as _y
    raw = _y.safe_load(Path(model.yaml_path).read_text())
    assert raw["ninfer"]["max_concurrency"] == 6, "失败后应还原原配置"


# ── 引擎无感知（stub 引擎全流程）────────────────────────────────
# 注意：注册 key 必须唯一（tune-stub），不要占用其他测试文件已有的 "stub" 注册项。

class StubAdapter(EngineAdapter):
    """只实现钩子、字段完全不同的"别家引擎"。"""
    engine_type = "tune-stub"

    def check_health(self, m):
        return "✅"

    def get_context_window(self, m):
        return None

    def validate_config(self, m):
        return []

    def start(self, m):
        return {"status": "ok"}

    def stop(self, m):
        return {"status": "ok"}

    def is_alive(self, m):
        return True

    def scenario_fields(self, m):
        return ["gpu_mem_mib", "max_batch"]

    def engine_caps(self, m):
        return {"max_batch": {"min": 1, "max": 4}}

    def validate_scenario(self, m, v):
        out = []
        if v.get("gpu_mem_mib") and v["gpu_mem_mib"] > 30000:
            out.append("⚠ 显存超 30GB")
        return out

    def restart(self, m):
        return {"status": "ok", "message": "stub restarted"}


register("tune-stub", StubAdapter)


def test_stub_engine_drives_tune_without_importing_ninfer(monkeypatch, tmp_path):
    """编排器对引擎无感知的核心证明：stub 字段驱动 preview + apply。"""
    import yaml as _y
    class StubCfg:
        gpu_mem_mib = 24576
        max_batch = 2

    y = tmp_path / "stub.yaml"
    y.write_text("name: stub\npresets:\n  p1:\n    max_batch: 9\n    gpu_mem_mib: 32768\n")
    m = ModelConfig(name="stub", description="d", type="tune-stub")
    m.mycfg = StubCfg()
    monkeypatch.setitem(ModelConfig._ENGINE_ATTR, "tune-stub", "mycfg")
    m.presets = _y.safe_load(y.read_text())["presets"]
    m.yaml_path = str(y)

    monkeypatch.setattr(tune, "BASELINE_FILE", tmp_path / "bl.yaml")
    p = tune.preview(m, "p1")
    assert p["after"]["max_batch"] == 4          # 引擎 caps 钳制
    assert p["after"]["gpu_mem_mib"] == 32768
    assert any("30GB" in i for i in p["issues"])

    r = tune.apply(m, "p1", restart=False)
    assert r["status"] == "applied_restart_pending"
    raw = _y.safe_load(y.read_text())
    assert raw["tune-stub"]["max_batch"] == 4    # YAML 顶层写回用的是 model.type 块
    assert raw["active_preset"] == "p1"
    assert m.mycfg.max_batch == 4