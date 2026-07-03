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
    VLLMConfig, ComfyUIConfig, Profile, load_profiles, DEFAULT_PROFILES,
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
    raw = open(DEFAULT_PROFILES).read()
    assert "profiles:" in raw
    profiles = load_profiles(DEFAULT_PROFILES)
    assert len(profiles) == 5
    print("  ✅ profiles.yaml valid, 5 profiles loaded")


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
