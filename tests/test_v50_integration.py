#!/usr/bin/env python3
"""IFF v5.0 Data Architecture & Engine Adapter — Targeted Integration Tests."""

import json, sqlite3, threading, time
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest, tempfile


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def tmp_d():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def iffdb(tmp_d):
    from inferfabric.db import IFFDB
    db = IFFDB(tmp_d)
    yield db
    try:
        db.close()
    except Exception:
        pass


@pytest.fixture
def statedb(tmp_d):
    from inferfabric.state import StateDB
    yield StateDB(tmp_d / "test.db")


@pytest.fixture
def telemetry(tmp_d):
    from inferfabric.telemetry import TelemetryHub
    th = TelemetryHub(tmp_d)
    yield th
    try:
        th.close()
    except Exception:
        pass


@pytest.fixture
def vllm_cfg():
    from inferfabric.config import ModelConfig, VLLMConfig
    return ModelConfig(
        name="test-vllm", description="T", type="vllm",
        vllm=VLLMConfig(
            model_dir="/m", served_name="t", port=8000,
            conda_env="e", gpu_memory_utilization=0.9,
            max_model_len=4096,
        ),
    )


# ── D1: IFFDB ────────────────────────────────────────────────────

class TestIFFDB:
    def test_tables_created(self, iffdb):
        for name in ("state", "request_log"):
            with iffdb.connect(name) as c:
                assert c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone()

    def test_pr(self, iffdb):
        assert iffdb.get_current_profile() == "idle"
        iffdb.set_current_profile("p")
        assert iffdb.get_current_profile() == "p"

    def test_active(self, iffdb):
        iffdb.add_active_service("m1")
        assert "m1" in iffdb.get_active_services()
        iffdb.remove_active_service("m1")
        assert not iffdb.get_active_services()

    def test_sleep(self, iffdb):
        assert iffdb.get_sleep_state("x") is None
        iffdb.set_sleep_state("x", 2)
        assert iffdb.get_sleep_state("x") == "l2"

    def test_history(self, iffdb):
        iffdb.add_switch_history("a", "b", 1.0)
        assert len(iffdb.get_switch_history()) >= 1

    def test_manual(self, iffdb):
        iffdb.record_manual_stop("m")
        assert iffdb.is_manually_stopped("m") is True
        iffdb.MANUAL_STOP_TTL = -1
        assert iffdb.is_manually_stopped("m") is False

    def test_log_crud(self, iffdb):
        e = {
            "req_id": "r1", "key_name": "k1", "model": "m1",
            "status": 200, "ttft_ms": 1.0, "tokens_in": 1,
            "tokens_out": 1, "duration_ms": 1.0, "route": "local",
            "cloud_provider": None, "error": None,
            "timestamp": time.time(), "ts": "",
        }
        iffdb.insert_request_log([e])
        assert len(iffdb.query_request_log(since=0)) == 1

    def test_prune(self, iffdb):
        old, now = time.time() - 99999, time.time()
        e = lambda rid, ts: {
            "req_id": rid, "key_name": "k1", "model": "m1",
            "status": 200, "ttft_ms": 1.0, "tokens_in": 1,
            "tokens_out": 1, "duration_ms": 1.0, "route": "local",
            "cloud_provider": None, "error": None,
            "timestamp": ts, "ts": "",
        }
        iffdb.insert_request_log([e("old", old), e("new", now)])
        iffdb.prune_request_log(now - 1)
        remaining = iffdb.query_request_log(since=0)
        assert len(remaining) == 1
        assert remaining[0]["req_id"] == "new"

    def test_concurrent(self, iffdb):
        errs = []
        def w(n):
            for i in range(30):
                try:
                    iffdb.set(f"k-{n}-{i}", "v")
                except Exception as e:
                    errs.append(e)
        tt = [threading.Thread(target=w, args=(i,)) for i in range(5)]
        for t in tt: t.start()
        for t in tt: t.join()
        assert not errs

    def test_legacy_get(self, iffdb):
        assert iffdb.get("gpu_mode") == "idle"
        assert iffdb.get("x") is None

    def test_pid(self, iffdb):
        iffdb.set_vllm_pid(42)
        assert iffdb.get_vllm_pid() == 42
        iffdb.set_vllm_pid(None)
        assert iffdb.get_vllm_pid() is None

    def test_gpu_mode_lifecycle(self, iffdb):
        assert iffdb.get_gpu_mode() == "idle"
        iffdb.set_gpu_mode("shared")
        assert iffdb.get_gpu_mode() == "shared"
        iffdb.set_gpu_mode("exclusive")
        assert iffdb.get_gpu_mode() == "exclusive"

    def test_switch_target(self, iffdb):
        assert iffdb.get_switch_target() == ""
        iffdb.set_switch_target("p2")
        assert iffdb.get_switch_target() == "p2"


# ── D2: StateDB compat ──────────────────────────────────────────

class TestStateDB:
    def test_get_set(self, statedb):
        statedb.set("k", "v")
        assert statedb.get("k") == "v"

    def test_multi(self, statedb):
        statedb.set_multi({"a": "1", "b": "2"})
        assert statedb.get("a") == "1"

    def test_sleep(self, statedb):
        statedb.set_sleep_state("m", 2)
        assert statedb.get_sleep_state("m") == "l2"



    def test_defaults(self, statedb):
        assert statedb.get("gpu_mode") == "idle"
        assert statedb.get("current_profile") == "idle"

    def test_concurrent(self, statedb):
        errs = []
        def w(n):
            for i in range(30):
                try:
                    statedb.set(f"k-{n}-{i}", "v")
                except Exception as e:
                    errs.append(e)
        tt = [threading.Thread(target=w, args=(i,)) for i in range(5)]
        for t in tt: t.start()
        for t in tt: t.join()
        assert not errs


class TestGPUMode:
    def test_transitions(self):
        from inferfabric.state import GPUMode, validate_transition
        assert validate_transition(GPUMode.IDLE, GPUMode.EXCLUSIVE)
        assert not validate_transition(GPUMode.EXCLUSIVE, GPUMode.SHARED)
        assert not validate_transition(GPUMode.SHARED, GPUMode.EXCLUSIVE)
        assert validate_transition(GPUMode.IDLE, GPUMode.IDLE)
        assert validate_transition(GPUMode.SHARED, GPUMode.SHARED)
        # exclusive->exclusive is not valid (different model swap must idle first)
        assert not validate_transition(GPUMode.EXCLUSIVE, GPUMode.EXCLUSIVE)


# ── D3: TelemetryHub ────────────────────────────────────────────

class TestTelemetryHub:
    def test_init(self, telemetry):
        assert telemetry.logger is not None
        assert telemetry.metrics is not None

    def test_record_persist(self, telemetry, tmp_d):
        from inferfabric.proxy.request_logger import RequestLog
        telemetry.record(RequestLog(
            req_id="r1", key_name="k", model="m",
            status=200, route="local", timestamp=time.time(),
        ))
        telemetry.close()
        from inferfabric.telemetry import TelemetryHub
        th2 = TelemetryHub(tmp_d)
        logs = th2.query_request_log(since=0)
        assert len(logs) >= 1
        assert logs[0]["model"] == "m"
        th2.close()

    def test_batch(self, telemetry, tmp_d):
        from inferfabric.proxy.request_logger import RequestLog
        for i in range(5):
            telemetry.record(RequestLog(
                req_id=f"r{i}", key_name="k", model=f"m{i}",
                status=200, route="local", timestamp=time.time(),
            ))
        telemetry.close()
        from inferfabric.telemetry import TelemetryHub
        th2 = TelemetryHub(tmp_d)
        assert len(th2.query_request_log(since=0)) == 5
        th2.close()

    def test_metrics(self, telemetry):
        assert isinstance(telemetry.get_metrics(), dict)

    def test_token_stats(self, telemetry):
        assert isinstance(telemetry.get_token_stats(), list)


# ── D4: Engine Adapters ─────────────────────────────────────────

class TestAdapterRegistry:
    def test_has_all(self):
        from inferfabric.engine_adapter import _adapters
        for name in ("vllm", "sglang", "ollama", "comfyui", "tts_server", "asr_server"):
            assert name in _adapters, f"Missing adapter: {name}"

    def test_singleton(self):
        from inferfabric.engine_adapter import get_adapter
        assert get_adapter("vllm") is get_adapter("vllm")

    def test_unknown_raises(self):
        from inferfabric.engine_adapter import get_adapter
        with pytest.raises(KeyError):
            get_adapter("alien")


class TestVLLMAdapter:
    def _a(self):
        from inferfabric.engine_adapter import get_adapter
        return get_adapter("vllm")

    def test_type(self):
        assert self._a().engine_type == "vllm"

    def test_validate_ok(self, vllm_cfg):
        assert self._a().validate_config(vllm_cfg) == []

    def test_validate_missing(self):
        from inferfabric.config import ModelConfig
        m = ModelConfig(name="x", description="x", type="vllm")
        assert any("Missing" in i for i in self._a().validate_config(m))

    def test_validate_bad_mem(self, vllm_cfg):
        vllm_cfg.vllm.gpu_memory_utilization = 1.5
        assert any("gpu_memory_utilization" in i for i in self._a().validate_config(vllm_cfg))

    def test_sleep_no_proc(self, vllm_cfg):
        r = self._a().sleep(vllm_cfg)
        assert r["status"] == "error"

    def test_wake_no_proc(self, vllm_cfg):
        r = self._a().wake(vllm_cfg)
        assert r["status"] == "error"

    def test_get_pid_no_proc(self, vllm_cfg):
        assert self._a().get_pid(vllm_cfg) is None

    def test_sleep_wake_pid_delegates(self, vllm_cfg):
        a = self._a()
        p = MagicMock()
        p.sleep_vllm.return_value = {"status": "ok"}
        p.wake_vllm.return_value = {"status": "ok"}
        p.vllm_pid = 123
        a.set_process_manager(p)
        assert a.sleep(vllm_cfg)["status"] == "ok"
        assert a.wake(vllm_cfg)["status"] == "ok"
        assert a.get_pid(vllm_cfg) == 123

    def test_sleep_wake_called_with_port(self, vllm_cfg):
        a = self._a()
        p = MagicMock()
        a.set_process_manager(p)
        a.sleep(vllm_cfg)
        p.sleep_vllm.assert_called_with(8000)
        a.wake(vllm_cfg)
        p.wake_vllm.assert_called_with(8000)

    def test_ctx_window(self, vllm_cfg):
        assert self._a().get_context_window(vllm_cfg) == 4096

    def test_ctx_window_none(self):
        from inferfabric.config import ModelConfig
        m = ModelConfig(name="x", description="x", type="vllm")
        assert self._a().get_context_window(m) is None

    def test_health_no_cfg(self):
        from inferfabric.config import ModelConfig
        m = ModelConfig(name="x", description="x", type="vllm")
        assert self._a().check_health(m) == "?"

    def test_start_raises(self, vllm_cfg):
        from inferfabric.engine_adapter.vllm import VLLMAdapter
        a = VLLMAdapter()
        with pytest.raises(RuntimeError):
            a.start(vllm_cfg)

    def test_stop_raises(self, vllm_cfg):
        from inferfabric.engine_adapter.vllm import VLLMAdapter
        a = VLLMAdapter()
        with pytest.raises(RuntimeError):
            a.stop(vllm_cfg)

    def test_metrics_flags(self, vllm_cfg):
        assert self._a().get_metrics_flags(vllm_cfg) == []


class TestSGLangAdapter:
    def _a(self):
        from inferfabric.engine_adapter import get_adapter
        return get_adapter("sglang")

    def test_type(self):
        assert self._a().engine_type == "sglang"

    def test_sleep_unsupported(self):
        from inferfabric.config import ModelConfig
        m = ModelConfig(name="x", description="x", type="sglang")
        r = self._a().sleep(m)
        assert r["status"] == "error"

    def test_wake_unsupported(self):
        from inferfabric.config import ModelConfig
        m = ModelConfig(name="x", description="x", type="sglang")
        r = self._a().wake(m)
        assert r["status"] == "error"

    def test_ctx_window_none(self):
        from inferfabric.config import ModelConfig
        m = ModelConfig(name="x", description="x", type="sglang")
        assert self._a().get_context_window(m) is None


class TestAllOtherAdapters:
    @pytest.mark.parametrize("name", ["ollama", "comfyui", "tts_server", "asr_server"])
    def test_type(self, name):
        from inferfabric.engine_adapter import get_adapter
        assert get_adapter(name).engine_type == name

    @pytest.mark.parametrize("name", ["ollama", "comfyui", "tts_server", "asr_server"])
    def test_sleep_unsupported(self, name):
        from inferfabric.config import ModelConfig
        from inferfabric.engine_adapter import get_adapter
        m = ModelConfig(name="x", description="x", type=name)
        r = get_adapter(name).sleep(m)
        assert r["status"] == "error"

    @pytest.mark.parametrize("name", ["ollama", "comfyui", "tts_server", "asr_server"])
    def test_wake_unsupported(self, name):
        from inferfabric.config import ModelConfig
        from inferfabric.engine_adapter import get_adapter
        m = ModelConfig(name="x", description="x", type=name)
        r = get_adapter(name).wake(m)
        assert r["status"] == "error"

    @pytest.mark.parametrize("name", ["ollama", "comfyui", "tts_server", "asr_server"])
    def test_pid_none(self, name):
        from inferfabric.config import ModelConfig
        from inferfabric.engine_adapter import get_adapter
        m = ModelConfig(name="x", description="x", type=name)
        assert get_adapter(name).get_pid(m) is None


# ── D5: Model Lifecycle ─────────────────────────────────────────

class TestModelLifecycle:
    @pytest.fixture
    def ml(self, statedb):
        from inferfabric.model_lifecycle import ModelLifecycle
        from inferfabric.config import ModelConfig, VLLMConfig, SleepModeConfig
        models = {
            "test-m": ModelConfig(
                name="test-m", description="T", type="vllm",
                vllm=VLLMConfig(
                    model_dir="/m", served_name="t", port=8000,
                    conda_env="e", gpu_memory_utilization=0.9,
                    max_model_len=4096,
                    sleep_mode=SleepModeConfig(enabled=True),
                ),
            ),
        }
        with patch("inferfabric.engine_adapter.get_adapter") as mg:
            ma = MagicMock()
            ma.sleep.return_value = {"status": "ok"}
            ma.wake.return_value = {"status": "ok"}
            mg.return_value = ma
            yield ModelLifecycle(
                state=statedb, proc=MagicMock(),
                health=MagicMock(), lock=MagicMock(),
                gpu_state=MagicMock(), models=models,
            )

    def test_sleep_not_running(self, ml):
        r = ml.sleep_model("nonexistent")
        assert r["status"] == "error"

    def test_sleep_success(self, ml):
        ml.state.add_active_service("test-m")
        r = ml.sleep_model("test-m")
        assert r["status"] == "ok"
        assert ml.state.get_sleep_state("test-m") == "l2"

    def test_sleep_already_sleeping(self, ml):
        ml.state.add_active_service("test-m")
        ml.state.set_sleep_state("test-m", 2)
        r = ml.sleep_model("test-m")
        assert r["status"] == "already_sleeping"

    def test_sleep_mutex(self, ml):
        ml.state.add_active_service("test-m")
        ml.state.set_sleep_state("other", 2)
        r = ml.sleep_model("test-m")
        assert r["status"] == "error"

    def test_wake_not_sleeping(self, ml):
        r = ml.wake_model("test-m")
        assert r["status"] == "already_awake"

    def test_wake_success(self, ml):
        ml.state.set_sleep_state("test-m", 2)
        ml.state.gpu_mode = "idle"
        r = ml.wake_model("test-m")
        assert r["status"] == "ok"

    def test_wake_unknown_model(self, ml):
        r = ml.wake_model("ghost")
        assert r["status"] == "error"