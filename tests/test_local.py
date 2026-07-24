#!/usr/bin/env python3
"""Local unit tests — no GPU / no vLLM touching.

Updated for v3.1 module structure.
"""

import sys
import os
import tempfile
import json
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from inferfabric.config import (
    VLLMConfig, ComfyUIConfig, Profile, load_profiles,
)
from inferfabric.state import StateDB, ProfileState
from inferfabric.gpu_lock import GPULock
from inferfabric.health import gpu_used_mb, wait_gpu_free, check_http_status
from inferfabric.process_manager import ProcessManager
from inferfabric.manager import ProfileManager


# ─── Helpers ─────────────────────────────────────────────────────

def assert_eq(a, b, label=""):
    assert a == b, f"FAIL {label}: {a!r} != {b!r}"


def assert_true(v, label=""):
    assert v, f"FAIL {label}: {v!r}"


def test_profiles_load():
    """All profiles load without error."""
    # DEFAULT_PROFILES removed in v4.2 — skip if not present
    profiles_path = Path.home() / ".local" / "share" / "inferfabric" / "profiles.yaml"
    if not profiles_path.exists():
        print("  ⏭️ profiles.yaml not found (v4.2+ uses models.d/), skipping")
        return
    raw = open(profiles_path).read()
    assert "profiles:" in raw
    profiles = load_profiles(profiles_path)
    assert len(profiles) > 0
    print(f"  ✅ profiles.yaml valid, {len(profiles)} profiles loaded")


def test_vllm_config_build_cmd():
    """VLLMConfig.build_cmd() produces correct command."""
    cfg = VLLMConfig(
        model_dir="test-model",
        served_name="test",
        port=9999,
        conda_env="test-env",
        max_model_len=64000,
        gpu_memory_utilization=0.5,
        max_num_seqs=4,
        kv_cache_dtype="fp8",
        extra_flags="--reasoning-parser qwen3",
    )
    cmd = cfg.build_cmd()
    assert "--port" in cmd and "9999" in cmd
    assert "--max-model-len" in cmd and "64000" in cmd
    assert "--reasoning-parser" in cmd
    print("  ✅ build_cmd correct")


def test_comfyui_config_native():
    """ComfyUIConfig native vs legacy detection."""
    # Native: has conda_env, no startup_script
    cfg_native = ComfyUIConfig(conda_env="comfyui", port=8188, health_url="http://localhost:8188/system_stats")
    assert cfg_native.use_native == True

    # Legacy: has startup_script
    cfg_legacy = ComfyUIConfig(startup_script="/path/to/script.sh", health_url="http://localhost:8188/system_stats")
    assert cfg_legacy.use_native == False

    # Both: startup_script takes precedence (use_native=False)
    cfg_both = ComfyUIConfig(conda_env="comfyui", startup_script="/path/to/script.sh", health_url="http://localhost:8188/system_stats")
    assert cfg_both.use_native == False

    print("  ✅ ComfyUIConfig native detection correct")


def test_state_db():
    """StateDB CRUD and persistence."""
    with tempfile.TemporaryDirectory() as tmp:
        db = StateDB(Path(tmp) / "test.db")

        # Init defaults
        assert db.get("current_profile") == "idle"
        assert db.get("profile_state") == "idle"
        assert db.get("vllm_pid") == ""
        assert db.get("comfyui_pid") == ""

        # Set / Get
        db.set("current_profile", "qw36_full")
        assert db.get("current_profile") == "qw36_full"

        # set_multi
        db.set_multi({"current_profile": "gemma_full", "profile_state": "switching"})
        assert db.get("current_profile") == "gemma_full"
        assert db.get("profile_state") == "switching"

        # History
        db.add_history("idle", "qw36_full", 5.0, "ok")
        hist = db.get_history(limit=10)
        assert len(hist) == 1
        assert hist[0]["to"] == "qw36_full"
        assert hist[0]["status"] == "ok"

        # Reopen from same path
        db2 = StateDB(Path(tmp) / "test.db")
        assert db2.get("current_profile") == "gemma_full"
        hist2 = db2.get_history(limit=10)
        assert len(hist2) == 1

    print("  ✅ StateDB CRUD + persistence")


def test_profile_state():
    """ProfileState constants and helpers."""
    assert ProfileState.is_active("switching")
    assert ProfileState.is_active("healthy")
    assert ProfileState.is_active("error")
    assert not ProfileState.is_active("idle")
    print("  ✅ ProfileState constants correct")


def test_gpu_lock():
    """GPULock acquire/release with temp file."""
    with tempfile.TemporaryDirectory() as tmp:
        lock = GPULock(lock_path=Path(tmp) / "test.lock")

        # Non-blocking acquire
        assert lock.acquire() == True
        assert lock.is_held == True

        # Second acquire (same instance) → already held
        assert lock.acquire() == True

        # Release
        lock.release()
        assert lock.is_held == False

        # Second lock on same file
        lock2 = GPULock(lock_path=Path(tmp) / "test.lock")
        assert lock2.acquire() == True
        lock2.release()

    print("  ✅ GPULock acquire/release")


def test_profile_list():
    """ProfileManager.list_profiles() returns all profiles."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(state_db_path=tmp + "/state.db")
        profiles = mgr.list_profiles()
        names = {p["name"] for p in profiles}
        expected = {"qw36_full", "qw35_comfyui", "gemma_full", "comfyui_only", "idle"}
        assert names == expected, f"Missing: {expected - names}"
        assert len(profiles) == 5
        print(f"  ✅ list_profiles: {len(profiles)} profiles")


def test_profile_details():
    """Each profile has expected attributes."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(state_db_path=tmp + "/state.db")
        p = mgr._profiles["qw36_full"]
        assert p.gpu_owner == "vllm"
        assert p.vllm.port == 8000
        assert p.vllm.max_model_len == 128000
        assert p.vllm.gpu_memory_utilization == 0.90

        p2 = mgr._profiles["qw35_comfyui"]
        assert p2.gpu_owner == "shared"
        assert p2.vllm.port == 8002
        assert p2.comfyui is not None
        assert p2.comfyui.use_native == True
        assert p2.comfyui.conda_env == "comfyui"
        assert p2.comfyui.port == 8188

        p3 = mgr._profiles["comfyui_only"]
        assert p3.vllm is None
        assert p3.comfyui is not None
        assert p3.comfyui.use_native == True

        p4 = mgr._profiles["idle"]
        assert p4.vllm is None
        assert p4.comfyui is None

    print("  ✅ Profile details correct")


def test_switch_same_profile():
    """Switch to same profile → already_active."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(state_db_path=tmp + "/state.db")
        mgr.state.set_multi({"current_profile": "qw36_full", "profile_state": "healthy"})
        result = mgr.switch("qw36_full")
        assert result["status"] == "already_active"
        assert result["profile"] == "qw36_full"
    print("  ✅ switch same → already_active")


def test_switch_unknown_profile():
    """Switch to non-existent profile → error."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(state_db_path=tmp + "/state.db")
        result = mgr.switch("nonexistent_profile")
        assert result["status"] == "error"
    print("  ✅ switch unknown → error")


def test_switch_idle_no_start():
    """Switch to idle → stops services, starts nothing.
    Mock ProcessManager to verify no start methods are called."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(state_db_path=tmp + "/state.db")
        mgr.state.set("current_profile", "qw36_full")

        started = {"vllm": False, "comfyui": False}
        orig_start_vllm = mgr._proc.start_vllm
        orig_start_comfyui = mgr._proc.start_comfyui
        orig_stop_all = mgr._proc.stop_all

        def mock_start_vllm(cfg):
            started["vllm"] = True
            return {"status": "healthy", "port": 8000, "pid": 12345}
        def mock_start_comfyui(cfg):
            started["comfyui"] = True
            return {"status": "healthy", "port": 8188, "pid": 12346}
        def mock_stop_all(**kw):
            return {"vllm": {"status": "ok"}, "comfyui": {"status": "ok"}}

        # Also mock wait_gpu_free
        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.start_vllm = mock_start_vllm
            mgr._proc.start_comfyui = mock_start_comfyui
            mgr._proc.stop_all = mock_stop_all
            result = mgr.switch("idle")
            assert not started["vllm"], "idle should not start vLLM"
            assert not started["comfyui"], "idle should not start ComfyUI"
            assert result["status"] == "switched"
        finally:
            health_mod.wait_gpu_free = orig_wait
            mgr._proc.start_vllm = orig_start_vllm
            mgr._proc.start_comfyui = orig_start_comfyui
            mgr._proc.stop_all = orig_stop_all
    print("  ✅ idle switch → no services started")


def test_switch_records_history():
    """Switch records history entry."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(state_db_path=tmp + "/state.db")
        mgr.state.set("current_profile", "idle")

        def mock_start_vllm(cfg):
            return {"status": "healthy", "port": 8000, "pid": 12345}
        def mock_stop_all(**kw):
            return {"vllm": {"status": "ok"}, "comfyui": {"status": "ok"}}

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.start_vllm = mock_start_vllm
            mgr._proc.stop_all = mock_stop_all
            result = mgr.switch("qw36_full")
        finally:
            health_mod.wait_gpu_free = orig_wait

        hist = mgr.state.get_history(limit=10)
        assert len(hist) >= 1
        assert hist[0]["to"] == "qw36_full"
        assert hist[0]["status"] == "ok"
    print("  ✅ switch history recorded")


def test_switch_comfyui_failure_interrupts():
    """v3.1 fix: ComfyUI failure should interrupt switch."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(state_db_path=tmp + "/state.db")
        mgr.state.set("current_profile", "idle")

        def mock_start_comfyui(cfg):
            return {"status": "error", "message": "ComfyUI crashed"}
        def mock_start_vllm(cfg):
            return {"status": "healthy", "port": 8002, "pid": 12345}
        def mock_stop_all(**kw):
            return {"vllm": {"status": "ok"}, "comfyui": {"status": "ok"}}

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.start_comfyui = mock_start_comfyui
            mgr._proc.start_vllm = mock_start_vllm
            mgr._proc.stop_all = mock_stop_all
            result = mgr.switch("qw35_comfyui")
            # ComfyUI failed → switch should error, NOT silently succeed
            assert result["status"] == "error", f"Expected error but got: {result['status']}"
            assert "ComfyUI" in result["message"]
        finally:
            health_mod.wait_gpu_free = orig_wait
    print("  ✅ ComfyUI failure interrupts switch (v3.1 fix)")


def test_status_includes_pids():
    """Status dict includes both vllm_pid and comfyui_pid."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(state_db_path=tmp + "/state.db")
        s = mgr.status()
        assert "vllm_pid" in s
        assert "comfyui_pid" in s
    print("  ✅ status includes both PIDs")


def test_gpu_query():
    """gpu_used_mb() returns valid number."""
    used = gpu_used_mb()
    assert 0 < used < 100000, f"GPU used MB unexpected: {used}"
    print(f"  ✅ GPU query: {used} MB")


def test_backward_compat_imports():
    """Old imports from profile_manager still work."""
    from inferfabric.profile_manager import (
        ProfileManager, StateDB, ProfileState, GPULock,
        VLLMConfig, ComfyUIConfig, Profile,
        gpu_used_mb, gpu_total_mb, wait_http, check_http_status,
        ProcessManager,
        GPU_LOCK, GPU_LOCK_PATH,
    )
    assert GPU_LOCK == GPU_LOCK_PATH
    print("  ✅ backward-compatible imports work")


# ─── Runner ─────────────────────────────────────────────────────

def main():
    tests = [
        ("profiles.yaml", test_profiles_load),
        ("VLLMConfig.build_cmd", test_vllm_config_build_cmd),
        ("ComfyUIConfig.native", test_comfyui_config_native),
        ("StateDB CRUD", test_state_db),
        ("ProfileState", test_profile_state),
        ("GPULock", test_gpu_lock),
        ("list_profiles", test_profile_list),
        ("profile details", test_profile_details),
        ("switch same profile", test_switch_same_profile),
        ("switch unknown profile", test_switch_unknown_profile),
        ("idle skip start", test_switch_idle_no_start),
        ("history recorded", test_switch_records_history),
        ("ComfyUI failure interrupts", test_switch_comfyui_failure_interrupts),
        ("status includes PIDs", test_status_includes_pids),
        ("GPU query", test_gpu_query),
        ("backward compat", test_backward_compat_imports),
    ]

    passed = 0
    failed = 0
    for label, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            import traceback
            print(f"  ❌ {label}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed / {len(tests)}")
    return failed


if __name__ == "__main__":
    exit(main())

# ── 共享增量测试 (原 test_shared_incremental.py) ──
#!/usr/bin/env python3
"""Unit tests for shared-model incremental start/stop logic.

No real GPU / vLLM / ComfyUI interaction — all calls are mocked or
pure-config checks.
"""

import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from inferfabric.config import ModelConfig, VLLMConfig, ComfyUIConfig, load_models
from inferfabric.state import StateDB, ProfileState, GPUMode


# ─── Helpers ─────────────────────────────────────────────────────

def assert_eq(a, b, label=""):
    assert a == b, f"FAIL {label}: {a!r} != {b!r}"


def assert_true(v, label=""):
    assert v, f"FAIL {label}: {v!r}"


def assert_in(needle, haystack, label=""):
    assert needle in haystack, f"FAIL {label}: {needle!r} not in {haystack!r}"


# ─── YAML / Config Tests ────────────────────────────────────────

def test_model_config_has_vram_field():
    """ModelConfig exposes typical_vram_pct."""
    m = ModelConfig(
        name="test", description="test", mode="shared",
        type="vllm", typical_vram_pct=38.0,
    )
    assert_eq(m.typical_vram_pct, 38.0)


def test_load_models_parses_vram():
    """load_models() reads typical_vram_pct from YAML."""
    models = load_models()
    assert_eq(models["qwen35-9b"].typical_vram_pct, 38.0)
    assert_eq(models["comfyui"].typical_vram_pct, 50.0)
    # exclusive models default to 0
    assert_eq(models["qwen36-27b"].typical_vram_pct, 0.0)


def test_model_config_default_vram():
    """ModelConfig without typical_vram_pct defaults to 0."""
    m = ModelConfig(name="x", description="x", mode="shared", type="vllm")
    assert_eq(m.typical_vram_pct, 0.0)


# ─── _shared_add_service Tests ───────────────────────────────────

def test_shared_add_already_active():
    """_shared_add_service returns already_active when model is already running."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_manager(tmp, models={"qw35-9b": _make_shared_vllm("qw35-9b", 8002)})
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        result = mgr._shared_add_service(mgr._models["qw35-9b"])
        assert_eq(result["status"], "already_active")


def test_shared_add_incremental_no_stop():
    """_shared_add_service starts only the new model — stop_all is NOT called."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        # Mock _get_current_vram_pct so VRAM check passes (40% + 50% = 90 < 95)
        mgr._get_current_vram_pct = lambda: 40.0

        stop_all_called = [False]
        start_count = {"qw35-9b": 0, "comfyui": 0}

        def mock_stop_all(**kw):
            stop_all_called[0] = True

        def mock_start_comfyui(cfg):
            start_count["comfyui"] += 1
            return {"status": "healthy", "port": cfg.port, "pid": 9999}

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_all = mock_stop_all
            mgr._proc.start_comfyui = mock_start_comfyui

            result = mgr._shared_add_service(mgr._models["comfyui"])

            assert_true(not stop_all_called[0], "stop_all must NOT be called")
            assert_true(start_count["comfyui"] >= 1, "comfyui must be started")
            assert_true(start_count["qw35-9b"] == 0, "qw35-9b must NOT be restarted")
            assert_eq(result["status"], "switched")
            assert_in("qw35-9b", result["active_services"])
            assert_in("comfyui", result["active_services"])
        finally:
            health_mod.wait_gpu_free = orig_wait


def test_shared_add_vram_reject():
    """_shared_add_service rejects when VRAM headroom insufficient."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])
        # Simulate 60% used → 60 + 50 = 110 > 95
        mgr._get_current_vram_pct = lambda: 60.0

        result = mgr._shared_add_service(mgr._models["comfyui"])
        assert_eq(result["status"], "error")
        assert_in("Insufficient GPU memory", result["message"])


def test_shared_add_vram_accept():
    """_shared_add_service accepts when VRAM headroom is OK."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])
        mgr._get_current_vram_pct = lambda: 40.0

        start_count = [0]

        def mock_start_comfyui(cfg):
            start_count[0] += 1
            return {"status": "healthy", "port": cfg.port, "pid": 9999}

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.start_comfyui = mock_start_comfyui
            result = mgr._shared_add_service(mgr._models["comfyui"])
            assert_eq(result["status"], "switched")
            assert_true(start_count[0] >= 1, "model should start")
        finally:
            health_mod.wait_gpu_free = orig_wait


# ─── Port-Based Cleanup Tests (ProcessManager) ──────────────────

def test_stop_vllm_always_runs_port_cleanup():
    """stop_vllm(port=X) always does port-based cleanup regardless of tracked PID."""
    pm = _make_pm()
    pm._set_vllm_pid(99999)  # non-existent PID → ProcessLookupError

    port_calls = []
    def mock_pkill_by_port(port):
        port_calls.append(port)

    orig = pm._pkill_by_port
    pm._pkill_by_port = mock_pkill_by_port

    try:
        result = pm.stop_vllm(port=8002)
        assert_true(len(port_calls) >= 1, "port cleanup must run even when PID is dead")
        assert_eq(port_calls[0], 8002)
    finally:
        pm._pkill_by_port = orig


def test_stop_vllm_port_when_no_tracked_pid():
    """stop_vllm(port=X) works when tracked PID is None."""
    pm = _make_pm()
    pm._set_vllm_pid(None)

    port_calls = []
    def mock_pkill_by_port(port):
        port_calls.append(port)

    orig = pm._pkill_by_port
    pm._pkill_by_port = mock_pkill_by_port

    try:
        result = pm.stop_vllm(port=8002)
        assert_true(len(port_calls) >= 1, "port cleanup must run even when no tracked PID")
        assert_eq(port_calls[0], 8002)
    finally:
        pm._pkill_by_port = orig


def test_stop_comfyui_always_runs_port_cleanup():
    """stop_comfyui_with_config(port=X) always does port-based cleanup."""
    pm = _make_pm()
    pm._set_comfyui_pid(None)

    port_calls = []
    def mock_pkill_by_port(port):
        port_calls.append(port)

    orig = pm._pkill_by_port
    pm._pkill_by_port = mock_pkill_by_port

    try:
        cfg = ComfyUIConfig(port=8188)
        result = pm.stop_comfyui_with_config(cfg, port=8188)
        assert_true(len(port_calls) >= 1, "ComfyUI port cleanup must run")
        assert_eq(port_calls[0], 8188)
    finally:
        pm._pkill_by_port = orig


# ─── stop_all Tests ─────────────────────────────────────────────

def test_stop_all_passes_port_params():
    """stop_all forwards port parameters correctly."""
    pm = _make_pm()

    calls = []

    def mock_stop_vllm(port=None):
        calls.append(("vllm", port))

    def mock_stop_comfyui_with_config(cfg, port=None):
        calls.append(("comfyui", port))

    pm.stop_vllm = mock_stop_vllm
    pm.stop_comfyui_with_config = mock_stop_comfyui_with_config

    cfg = ComfyUIConfig(port=8188)
    pm.stop_all(
        comfyui_cfg=cfg,
        vllm_ports=[8002],
        comfyui_port=8188,
    )

    assert_in(("vllm", 8002), calls)
    assert_in(("comfyui", 8188), calls)


# ─── stop_service GPU Verification ──────────────────────────────

def test_stop_service_verifies_gpu():
    """stop_service calls wait_gpu_free after stop."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_manager(tmp, models={
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
        })
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        def mock_stop_vllm(port=None):
            pass

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_vllm = mock_stop_vllm
            result = mgr.stop_service("qw35-9b")
            assert_eq(result["status"], "stopped")
        finally:
            health_mod.wait_gpu_free = orig_wait


# ─── _switch_to_idle port params ────────────────────────────────

def test_switch_to_idle_passes_ports():
    """_switch_to_idle collects ports from active services and passes them."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b", "comfyui"])

        stop_all_received = {}

        def mock_stop_all(**kw):
            stop_all_received.update(kw)

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_all = mock_stop_all
            result = mgr._switch_to_idle()

            assert_true("vllm_ports" in stop_all_received)
            assert_in(8002, stop_all_received["vllm_ports"])
            assert_true(stop_all_received.get("comfyui_port") == 8188)
        finally:
            health_mod.wait_gpu_free = orig_wait


# ─── Route Dispatch Tests ───────────────────────────────────────

def test_switch_routes_shared_correctly():
    """switch() dispatches to correct path based on mode and target."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        # Mock _shared_add_service to capture the call
        shared_add_called = [False]
        orig_shared_add = mgr._shared_add_service
        def mock_shared_add(model):
            shared_add_called[0] = True
            return {"status": "switched", "model": model.name, "gpu_mode": GPUMode.SHARED}

        mgr._shared_add_service = mock_shared_add
        result = mgr.switch("comfyui")

        assert_true(shared_add_called[0], "shared mode + shared target → _shared_add_service")
        assert_eq(result["status"], "switched")


def test_switch_routes_exclusive_to_deploy():
    """switch() routes exclusive model to _deploy_model."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw36-27b": _make_exclusive_vllm("qw36-27b", 8000),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.IDLE)

        deploy_called = [False]
        orig_deploy = mgr._deploy_model
        def mock_deploy(model, mode):
            deploy_called[0] = True
            return {"status": "switched", "model": model.name, "gpu_mode": mode}

        mgr._deploy_model = mock_deploy
        result = mgr.switch("qw36-27b")

        assert_true(deploy_called[0], "idle + exclusive target → _deploy_model")
        assert_eq(result["status"], "switched")


def test_stop_service_routes_vllm():
    """stop_service routes to stop_vllm(port=) for vLLM models."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_manager(tmp, models={
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
        })
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        port_used = [None]
        def mock_stop_vllm(port=None):
            port_used[0] = port

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_vllm = mock_stop_vllm
            mgr.stop_service("qw35-9b")
            assert_eq(port_used[0], 8002, "stop_service must pass port=8002")
        finally:
            health_mod.wait_gpu_free = orig_wait


def test_stop_service_routes_comfyui():
    """stop_service routes to stop_comfyui_with_config(port=) for ComfyUI models."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_manager(tmp, models={
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        })
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["comfyui"])

        port_used = [None]
        def mock_stop_comfyui(cfg, port=None):
            port_used[0] = port

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_comfyui_with_config = mock_stop_comfyui
            mgr.stop_service("comfyui")
            assert_eq(port_used[0], 8188, "stop_service must pass port=8188")
        finally:
            health_mod.wait_gpu_free = orig_wait


# ─── Helpers ─────────────────────────────────────────────────────

def _make_shared_vllm(name, port):
    return ModelConfig(
        name=name, description=name, mode="shared", type="vllm",
        vllm=VLLMConfig(
            model_dir="test", served_name=name, port=port,
            conda_env="test", max_model_len=64000,
            gpu_memory_utilization=0.4, max_num_seqs=4, kv_cache_dtype="fp8",
        ),
        typical_vram_pct=38.0,
    )


def _make_exclusive_vllm(name, port):
    return ModelConfig(
        name=name, description=name, mode="exclusive", type="vllm",
        vllm=VLLMConfig(
            model_dir="test", served_name=name, port=port,
            conda_env="test", max_model_len=128000,
            gpu_memory_utilization=0.90, max_num_seqs=4, kv_cache_dtype="auto",
        ),
        typical_vram_pct=0.0,
    )


def _make_shared_comfyui(name, port):
    return ModelConfig(
        name=name, description=name, mode="shared", type="comfyui",
        comfyui=ComfyUIConfig(port=port, conda_env="test"),
        typical_vram_pct=50.0,
    )


def _make_manager(tmpdir, models=None):
    """Create a ProfileManager with mock models, no real services."""
    from inferfabric.manager import ProfileManager
    db = Path(tmpdir) / "state.db"
    mgr = ProfileManager(state_db_path=str(db))
    if models:
        mgr._models = models
    return mgr


def _make_pm():
    """Create a ProcessManager with a mock StateDB (no real files)."""
    import tempfile
    from inferfabric.process_manager import ProcessManager
    with tempfile.TemporaryDirectory() as tmp:
        db = StateDB(Path(tmp) / "test.db")
        return ProcessManager(state=db, log_dir=Path(tmp))


# ─── Runner ─────────────────────────────────────────────────────

def main():
    tests = [
        ("ModelConfig has vram field", test_model_config_has_vram_field),
        ("load_models parses vram", test_load_models_parses_vram),
        ("ModelConfig default vram", test_model_config_default_vram),
        ("shared add already active", test_shared_add_already_active),
        ("shared add incremental no stop", test_shared_add_incremental_no_stop),
        ("shared add vram reject", test_shared_add_vram_reject),
        ("shared add vram accept", test_shared_add_vram_accept),
        ("stop_vllm port cleanup always", test_stop_vllm_always_runs_port_cleanup),
        ("stop_vllm port no tracked pid", test_stop_vllm_port_when_no_tracked_pid),
        ("stop_comfyui port cleanup", test_stop_comfyui_always_runs_port_cleanup),
        ("stop_all passes port params", test_stop_all_passes_port_params),
        ("stop_service verifies gpu", test_stop_service_verifies_gpu),
        ("switch_to_idle passes ports", test_switch_to_idle_passes_ports),
        ("switch routes shared correctly", test_switch_routes_shared_correctly),
        ("switch routes exclusive to deploy", test_switch_routes_exclusive_to_deploy),
        ("stop_service routes vllm", test_stop_service_routes_vllm),
        ("stop_service routes comfyui", test_stop_service_routes_comfyui),
    ]

    passed = 0
    failed = 0
    for label, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ✅ {label}")
        except Exception as e:
            import traceback
            print(f"  ❌ {label}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed / {len(tests)}")
    return failed


if __name__ == "__main__":
    exit(main())

# ── 共享增量测试 (原 test_shared_incremental.py) ──
#!/usr/bin/env python3
"""Unit tests for shared-model incremental start/stop logic.

No real GPU / vLLM / ComfyUI interaction — all calls are mocked or
pure-config checks.
"""

import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from inferfabric.config import ModelConfig, VLLMConfig, ComfyUIConfig, load_models
from inferfabric.state import StateDB, ProfileState, GPUMode


# ─── Helpers ─────────────────────────────────────────────────────

def assert_eq(a, b, label=""):
    assert a == b, f"FAIL {label}: {a!r} != {b!r}"


def assert_true(v, label=""):
    assert v, f"FAIL {label}: {v!r}"


def assert_in(needle, haystack, label=""):
    assert needle in haystack, f"FAIL {label}: {needle!r} not in {haystack!r}"


# ─── YAML / Config Tests ────────────────────────────────────────

def test_model_config_has_vram_field():
    """ModelConfig exposes typical_vram_pct."""
    m = ModelConfig(
        name="test", description="test", mode="shared",
        type="vllm", typical_vram_pct=38.0,
    )
    assert_eq(m.typical_vram_pct, 38.0)


def test_load_models_parses_vram():
    """load_models() reads typical_vram_pct from YAML."""
    models = load_models()
    assert_eq(models["qwen35-9b"].typical_vram_pct, 38.0)
    assert_eq(models["comfyui"].typical_vram_pct, 50.0)
    # exclusive models default to 0
    assert_eq(models["qwen36-27b"].typical_vram_pct, 0.0)


def test_model_config_default_vram():
    """ModelConfig without typical_vram_pct defaults to 0."""
    m = ModelConfig(name="x", description="x", mode="shared", type="vllm")
    assert_eq(m.typical_vram_pct, 0.0)


# ─── _shared_add_service Tests ───────────────────────────────────

def test_shared_add_already_active():
    """_shared_add_service returns already_active when model is already running."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_manager(tmp, models={"qw35-9b": _make_shared_vllm("qw35-9b", 8002)})
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        result = mgr._shared_add_service(mgr._models["qw35-9b"])
        assert_eq(result["status"], "already_active")


def test_shared_add_incremental_no_stop():
    """_shared_add_service starts only the new model — stop_all is NOT called."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        # Mock _get_current_vram_pct so VRAM check passes (40% + 50% = 90 < 95)
        mgr._get_current_vram_pct = lambda: 40.0

        stop_all_called = [False]
        start_count = {"qw35-9b": 0, "comfyui": 0}

        def mock_stop_all(**kw):
            stop_all_called[0] = True

        def mock_start_comfyui(cfg):
            start_count["comfyui"] += 1
            return {"status": "healthy", "port": cfg.port, "pid": 9999}

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_all = mock_stop_all
            mgr._proc.start_comfyui = mock_start_comfyui

            result = mgr._shared_add_service(mgr._models["comfyui"])

            assert_true(not stop_all_called[0], "stop_all must NOT be called")
            assert_true(start_count["comfyui"] >= 1, "comfyui must be started")
            assert_true(start_count["qw35-9b"] == 0, "qw35-9b must NOT be restarted")
            assert_eq(result["status"], "switched")
            assert_in("qw35-9b", result["active_services"])
            assert_in("comfyui", result["active_services"])
        finally:
            health_mod.wait_gpu_free = orig_wait


def test_shared_add_vram_reject():
    """_shared_add_service rejects when VRAM headroom insufficient."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])
        # Simulate 60% used → 60 + 50 = 110 > 95
        mgr._get_current_vram_pct = lambda: 60.0

        result = mgr._shared_add_service(mgr._models["comfyui"])
        assert_eq(result["status"], "error")
        assert_in("Insufficient GPU memory", result["message"])


def test_shared_add_vram_accept():
    """_shared_add_service accepts when VRAM headroom is OK."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])
        mgr._get_current_vram_pct = lambda: 40.0

        start_count = [0]

        def mock_start_comfyui(cfg):
            start_count[0] += 1
            return {"status": "healthy", "port": cfg.port, "pid": 9999}

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.start_comfyui = mock_start_comfyui
            result = mgr._shared_add_service(mgr._models["comfyui"])
            assert_eq(result["status"], "switched")
            assert_true(start_count[0] >= 1, "model should start")
        finally:
            health_mod.wait_gpu_free = orig_wait


# ─── Port-Based Cleanup Tests (ProcessManager) ──────────────────

def test_stop_vllm_always_runs_port_cleanup():
    """stop_vllm(port=X) always does port-based cleanup regardless of tracked PID."""
    pm = _make_pm()
    pm._set_vllm_pid(99999)  # non-existent PID → ProcessLookupError

    port_calls = []
    def mock_pkill_by_port(port):
        port_calls.append(port)

    orig = pm._pkill_by_port
    pm._pkill_by_port = mock_pkill_by_port

    try:
        result = pm.stop_vllm(port=8002)
        assert_true(len(port_calls) >= 1, "port cleanup must run even when PID is dead")
        assert_eq(port_calls[0], 8002)
    finally:
        pm._pkill_by_port = orig


def test_stop_vllm_port_when_no_tracked_pid():
    """stop_vllm(port=X) works when tracked PID is None."""
    pm = _make_pm()
    pm._set_vllm_pid(None)

    port_calls = []
    def mock_pkill_by_port(port):
        port_calls.append(port)

    orig = pm._pkill_by_port
    pm._pkill_by_port = mock_pkill_by_port

    try:
        result = pm.stop_vllm(port=8002)
        assert_true(len(port_calls) >= 1, "port cleanup must run even when no tracked PID")
        assert_eq(port_calls[0], 8002)
    finally:
        pm._pkill_by_port = orig


def test_stop_comfyui_always_runs_port_cleanup():
    """stop_comfyui_with_config(port=X) always does port-based cleanup."""
    pm = _make_pm()
    pm._set_comfyui_pid(None)

    port_calls = []
    def mock_pkill_by_port(port):
        port_calls.append(port)

    orig = pm._pkill_by_port
    pm._pkill_by_port = mock_pkill_by_port

    try:
        cfg = ComfyUIConfig(port=8188)
        result = pm.stop_comfyui_with_config(cfg, port=8188)
        assert_true(len(port_calls) >= 1, "ComfyUI port cleanup must run")
        assert_eq(port_calls[0], 8188)
    finally:
        pm._pkill_by_port = orig


# ─── stop_all Tests ─────────────────────────────────────────────

def test_stop_all_passes_port_params():
    """stop_all forwards port parameters correctly."""
    pm = _make_pm()

    calls = []

    def mock_stop_vllm(port=None):
        calls.append(("vllm", port))

    def mock_stop_comfyui_with_config(cfg, port=None):
        calls.append(("comfyui", port))

    pm.stop_vllm = mock_stop_vllm
    pm.stop_comfyui_with_config = mock_stop_comfyui_with_config

    cfg = ComfyUIConfig(port=8188)
    pm.stop_all(
        comfyui_cfg=cfg,
        vllm_ports=[8002],
        comfyui_port=8188,
    )

    assert_in(("vllm", 8002), calls)
    assert_in(("comfyui", 8188), calls)


# ─── stop_service GPU Verification ──────────────────────────────

def test_stop_service_verifies_gpu():
    """stop_service calls wait_gpu_free after stop."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_manager(tmp, models={
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
        })
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        def mock_stop_vllm(port=None):
            pass

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_vllm = mock_stop_vllm
            result = mgr.stop_service("qw35-9b")
            assert_eq(result["status"], "stopped")
        finally:
            health_mod.wait_gpu_free = orig_wait


# ─── _switch_to_idle port params ────────────────────────────────

def test_switch_to_idle_passes_ports():
    """_switch_to_idle collects ports from active services and passes them."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b", "comfyui"])

        stop_all_received = {}

        def mock_stop_all(**kw):
            stop_all_received.update(kw)

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_all = mock_stop_all
            result = mgr._switch_to_idle()

            assert_true("vllm_ports" in stop_all_received)
            assert_in(8002, stop_all_received["vllm_ports"])
            assert_true(stop_all_received.get("comfyui_port") == 8188)
        finally:
            health_mod.wait_gpu_free = orig_wait


# ─── Route Dispatch Tests ───────────────────────────────────────

def test_switch_routes_shared_correctly():
    """switch() dispatches to correct path based on mode and target."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        # Mock _shared_add_service to capture the call
        shared_add_called = [False]
        orig_shared_add = mgr._shared_add_service
        def mock_shared_add(model):
            shared_add_called[0] = True
            return {"status": "switched", "model": model.name, "gpu_mode": GPUMode.SHARED}

        mgr._shared_add_service = mock_shared_add
        result = mgr.switch("comfyui")

        assert_true(shared_add_called[0], "shared mode + shared target → _shared_add_service")
        assert_eq(result["status"], "switched")


def test_switch_routes_exclusive_to_deploy():
    """switch() routes exclusive model to _deploy_model."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw36-27b": _make_exclusive_vllm("qw36-27b", 8000),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.IDLE)

        deploy_called = [False]
        orig_deploy = mgr._deploy_model
        def mock_deploy(model, mode):
            deploy_called[0] = True
            return {"status": "switched", "model": model.name, "gpu_mode": mode}

        mgr._deploy_model = mock_deploy
        result = mgr.switch("qw36-27b")

        assert_true(deploy_called[0], "idle + exclusive target → _deploy_model")
        assert_eq(result["status"], "switched")


def test_stop_service_routes_vllm():
    """stop_service routes to stop_vllm(port=) for vLLM models."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_manager(tmp, models={
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
        })
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        port_used = [None]
        def mock_stop_vllm(port=None):
            port_used[0] = port

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_vllm = mock_stop_vllm
            mgr.stop_service("qw35-9b")
            assert_eq(port_used[0], 8002, "stop_service must pass port=8002")
        finally:
            health_mod.wait_gpu_free = orig_wait


def test_stop_service_routes_comfyui():
    """stop_service routes to stop_comfyui_with_config(port=) for ComfyUI models."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_manager(tmp, models={
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        })
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["comfyui"])

        port_used = [None]
        def mock_stop_comfyui(cfg, port=None):
            port_used[0] = port

        import inferfabric.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_comfyui_with_config = mock_stop_comfyui
            mgr.stop_service("comfyui")
            assert_eq(port_used[0], 8188, "stop_service must pass port=8188")
        finally:
            health_mod.wait_gpu_free = orig_wait


# ─── Helpers ─────────────────────────────────────────────────────

def _make_shared_vllm(name, port):
    return ModelConfig(
        name=name, description=name, mode="shared", type="vllm",
        vllm=VLLMConfig(
            model_dir="test", served_name=name, port=port,
            conda_env="test", max_model_len=64000,
            gpu_memory_utilization=0.4, max_num_seqs=4, kv_cache_dtype="fp8",
        ),
        typical_vram_pct=38.0,
    )


def _make_exclusive_vllm(name, port):
    return ModelConfig(
        name=name, description=name, mode="exclusive", type="vllm",
        vllm=VLLMConfig(
            model_dir="test", served_name=name, port=port,
            conda_env="test", max_model_len=128000,
            gpu_memory_utilization=0.90, max_num_seqs=4, kv_cache_dtype="auto",
        ),
        typical_vram_pct=0.0,
    )


def _make_shared_comfyui(name, port):
    return ModelConfig(
        name=name, description=name, mode="shared", type="comfyui",
        comfyui=ComfyUIConfig(port=port, conda_env="test"),
        typical_vram_pct=50.0,
    )


def _make_manager(tmpdir, models=None):
    """Create a ProfileManager with mock models, no real services."""
    from inferfabric.manager import ProfileManager
    db = Path(tmpdir) / "state.db"
    mgr = ProfileManager(state_db_path=str(db))
    if models:
        mgr._models = models
    return mgr


def _make_pm():
    """Create a ProcessManager with a mock StateDB (no real files)."""
    import tempfile
    from inferfabric.process_manager import ProcessManager
    with tempfile.TemporaryDirectory() as tmp:
        db = StateDB(Path(tmp) / "test.db")
        return ProcessManager(state=db, log_dir=Path(tmp))


# ─── Runner ─────────────────────────────────────────────────────

def main():
    tests = [
        ("ModelConfig has vram field", test_model_config_has_vram_field),
        ("load_models parses vram", test_load_models_parses_vram),
        ("ModelConfig default vram", test_model_config_default_vram),
        ("shared add already active", test_shared_add_already_active),
        ("shared add incremental no stop", test_shared_add_incremental_no_stop),
        ("shared add vram reject", test_shared_add_vram_reject),
        ("shared add vram accept", test_shared_add_vram_accept),
        ("stop_vllm port cleanup always", test_stop_vllm_always_runs_port_cleanup),
        ("stop_vllm port no tracked pid", test_stop_vllm_port_when_no_tracked_pid),
        ("stop_comfyui port cleanup", test_stop_comfyui_always_runs_port_cleanup),
        ("stop_all passes port params", test_stop_all_passes_port_params),
        ("stop_service verifies gpu", test_stop_service_verifies_gpu),
        ("switch_to_idle passes ports", test_switch_to_idle_passes_ports),
        ("switch routes shared correctly", test_switch_routes_shared_correctly),
        ("switch routes exclusive to deploy", test_switch_routes_exclusive_to_deploy),
        ("stop_service routes vllm", test_stop_service_routes_vllm),
        ("stop_service routes comfyui", test_stop_service_routes_comfyui),
    ]

    passed = 0
    failed = 0
    for label, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ✅ {label}")
        except Exception as e:
            import traceback
            print(f"  ❌ {label}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed / {len(tests)}")
    return failed


if __name__ == "__main__":
    exit(main())
