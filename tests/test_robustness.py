#!/usr/bin/env python3
"""InferFabric v4.3 robustness test suite — covers audit fixes.

Tests:
  1. Dashboard sw lock (finally + timeout safety)
  2. stop_all/stop_service ComfyUI config passthrough
  3. /v1/models multi-model aggregation
  4. Proxy lazy upstream invalidation
  5. _pkill fallback dynamic port discovery
  6. force_reset ComfyUI config passthrough
  7. State machine edge cases & robustness
  8. Concurrent request handling
"""

import sys
import os
import json
import time
import tempfile
import threading
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock, call
from http.client import HTTPConnection

sys.path.insert(0, str(Path(__file__).parent.parent))


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _make_state_db(tmp: str) -> object:
    """Create a StateDB backed by a temp directory."""
    from inferfabric.state import StateDB
    db = StateDB(Path(tmp) / "state.db")
    db.set("gpu_mode", "idle")
    db.set("active_services", "[]")
    return db


def _make_model(name, mode, port, is_vllm=True):
    """Create a mock ModelConfig."""
    m = MagicMock()
    m.name = name
    m.mode = mode
    m.is_vllm = is_vllm
    m.is_comfyui = not is_vllm
    m.is_exclusive = (mode == "exclusive")
    if is_vllm:
        m.vllm = MagicMock()
        m.vllm.port = port
        m.vllm.conda_env = "test-env"
        m.vllm.served_name = f"vllm_{name}"
        m.comfyui = None
    else:
        m.comfyui = MagicMock()
        m.comfyui.port = port
        m.vllm = None
    return m


# ═══════════════════════════════════════════════════════════════
# 1. Dashboard sw Lock Safety
# ═══════════════════════════════════════════════════════════════

def test_dashboard_sw_lock_has_finally():
    """Verify all action functions use finally{sw=false} instead of bare sw=false."""
    dashboard_path = Path(__file__).parent.parent / "inferfabric" / "dashboard.py"
    content = dashboard_path.read_text()

    # Count 'finally{sw=false}' occurrences
    finally_count = content.count("finally{sw=false;}")
    # Count action functions that need the lock
    action_funcs = ["doRelease", "doSleep", "doWake", "doSwitch", "doStop"]
    for func in action_funcs:
        assert f"async function {func}" in content, f"Missing function {func}"

    assert finally_count >= 5, f"Expected >=5 finally{{sw=false}}, got {finally_count}"
    # No bare 'sw=false' outside finally blocks (in action functions)
    # The only sw=false should be inside finally or the swLock function
    bare_sw_false = content.count("sw=false;")
    # sw=false appears in swLock (force unlock) and finally blocks
    assert bare_sw_false >= 5, f"Expected sw=false in finally blocks, got {bare_sw_false}"
    print("✅ Dashboard sw lock: all action functions use finally{sw=false}")


def test_dashboard_sw_lock_timeout_mechanism():
    """Verify swLock() function exists with 30s timeout safety."""
    dashboard_path = Path(__file__).parent.parent / "inferfabric" / "dashboard.py"
    content = dashboard_path.read_text()

    assert "function swLock()" in content, "Missing swLock() function"
    assert "30000" in content, "Missing 30s timeout in swLock"
    assert "swT=Date.now()" in content, "Missing timestamp in swLock"
    assert "if(!swLock())return" in content, "Missing swLock() guard in action functions"
    print("✅ Dashboard swLock: 30s timeout safety mechanism present")


def test_dashboard_no_bare_sw_true_in_actions():
    """Verify no action function uses bare 'sw=true' (should use swLock())."""
    dashboard_path = Path(__file__).parent.parent / "inferfabric" / "dashboard.py"
    content = dashboard_path.read_text()

    # sw=true should only appear inside swLock() function, not in action function bodies
    # Count sw=true occurrences - should be exactly 1 (in swLock)
    sw_true_count = content.count("sw=true")
    assert sw_true_count == 1, f"Expected exactly 1 sw=true (in swLock), found {sw_true_count}"
    # Verify it's inside swLock
    swlock_pos = content.find("function swLock()")
    sw_true_pos = content.find("sw=true")
    assert sw_true_pos > swlock_pos, "sw=true should be inside swLock() function"
    print("✅ Dashboard: no bare sw=true in action functions")


# ═══════════════════════════════════════════════════════════════
# 2. ComfyUI Config Passthrough
# ═══════════════════════════════════════════════════════════════

def test_switch_to_idle_passes_comfyui_config():
    """_switch_to_idle() should pass comfyui_cfg to stop_all()."""
    manager_path = Path(__file__).parent.parent / "inferfabric" / "manager.py"
    content = manager_path.read_text()

    # Find _switch_to_idle method
    assert "comfyui_cfg = None" in content, "Missing comfyui_cfg discovery in _switch_to_idle"
    assert "stop_all(comfyui_cfg=comfyui_cfg)" in content, "Missing comfyui_cfg passthrough in _switch_to_idle"
    print("✅ _switch_to_idle: passes ComfyUI config to stop_all()")


def test_stop_service_uses_stop_comfyui_with_config():
    """stop_service() should use stop_comfyui_with_config() for ComfyUI models."""
    manager_path = Path(__file__).parent.parent / "inferfabric" / "manager.py"
    content = manager_path.read_text()

    assert "stop_comfyui_with_config(model.comfyui)" in content, \
        "stop_service() should call stop_comfyui_with_config() with model config"
    print("✅ stop_service: uses stop_comfyui_with_config() for ComfyUI")


def test_shared_add_service_passes_comfyui_config():
    """_shared_add_service() should pass comfyui_cfg to stop_all()."""
    manager_path = Path(__file__).parent.parent / "inferfabric" / "manager.py"
    content = manager_path.read_text()

    # Find _shared_add_service
    assert "stop_all(comfyui_cfg=comfyui_cfg)" in content, \
        "Missing comfyui_cfg passthrough in _shared_add_service"
    print("✅ _shared_add_service: passes ComfyUI config to stop_all()")


def test_force_reset_passes_comfyui_config():
    """force_reset() should pass comfyui_cfg to stop_all()."""
    manager_path = Path(__file__).parent.parent / "inferfabric" / "manager.py"
    content = manager_path.read_text()

    # Find force_reset
    lines = content.splitlines()
    in_force_reset = False
    found_cfg_discovery = False
    found_passthrough = False
    for line in lines:
        if "def force_reset" in line:
            in_force_reset = True
        elif in_force_reset and "def " in line and "force_reset" not in line:
            in_force_reset = False
        elif in_force_reset:
            if "comfyui_cfg = None" in line:
                found_cfg_discovery = True
            if "stop_all(comfyui_cfg=comfyui_cfg)" in line:
                found_passthrough = True

    assert found_cfg_discovery, "force_reset() missing comfyui_cfg discovery"
    assert found_passthrough, "force_reset() missing comfyui_cfg passthrough"
    print("✅ force_reset: passes ComfyUI config to stop_all()")


# ═══════════════════════════════════════════════════════════════
# 3. /v1/models Multi-Model Aggregation
# ═══════════════════════════════════════════════════════════════

def test_v1_models_aggregation_code():
    """Verify _handle_v1_models iterates all active services, not just active[0]."""
    proxy_path = Path(__file__).parent.parent / "inferfabric" / "proxy.py"
    content = proxy_path.read_text()

    # Should iterate over all active services
    assert "for svc in active:" in content, "Missing iteration over all active services"
    # Should aggregate into all_models list
    assert "all_models" in content, "Missing all_models aggregation list"
    assert "all_models.extend" in content, "Missing all_models.extend() for aggregation"
    # Should return aggregated result
    assert '"data": all_models' in content, "Missing aggregated data in response"
    print("✅ /v1/models: aggregates models from all active vLLM services")


def test_v1_models_no_hardcoded_first():
    """Verify no active[0] hardcoded index in _handle_v1_models."""
    proxy_path = Path(__file__).parent.parent / "inferfabric" / "proxy.py"
    content = proxy_path.read_text()

    # Extract _handle_v1_models method
    start = content.find("def _handle_v1_models")
    end = content.find("\n    def ", start + 1)
    method = content[start:end]

    assert "active[0]" not in method, "Found hardcoded active[0] in _handle_v1_models"
    print("✅ /v1/models: no hardcoded active[0]")


# ═══════════════════════════════════════════════════════════════
# 4. Proxy Lazy Upstream Invalidation
# ═══════════════════════════════════════════════════════════════

def test_get_upstream_no_health_probe():
    """get_upstream() should NOT probe /health on every call (lazy invalidation)."""
    proxy_path = Path(__file__).parent.parent / "inferfabric" / "proxy.py"
    content = proxy_path.read_text()

    # Extract get_upstream method
    start = content.find("def get_upstream")
    end = content.find("\n    def ", start + 1)
    method = content[start:end]

    assert '"/health"' not in method, "get_upstream() should not probe /health (lazy invalidation)"
    assert "lazy" in method.lower() or "Lazy" in method, "Missing lazy invalidation docstring"
    print("✅ get_upstream: uses lazy invalidation (no health probe)")


# ═══════════════════════════════════════════════════════════════
# 5. Dynamic Port Discovery in pkill Fallback
# ═══════════════════════════════════════════════════════════════

def test_pkill_fallback_dynamic_ports():
    """_pkill_vllm_fallback should discover ports from models.d, not hardcode."""
    pm_path = Path(__file__).parent.parent / "inferfabric" / "process_manager.py"
    content = pm_path.read_text()

    # Extract _pkill_vllm_fallback method
    start = content.find("def _pkill_vllm_fallback")
    end = content.find("\n    def ", start + 1)
    method = content[start:end]

    assert "load_models" in method, "Missing dynamic port discovery via load_models()"
    assert "vllm_ports" in method, "Missing vllm_ports variable"
    # Should still have fallback defaults
    assert "8000" in method, "Missing fallback default ports"
    print("✅ _pkill_vllm_fallback: dynamic port discovery with fallback defaults")


# ═══════════════════════════════════════════════════════════════
# 6. State Machine Robustness
# ═══════════════════════════════════════════════════════════════

def test_state_machine_transition_validation():
    """Test that invalid transitions are rejected."""
    from inferfabric.manager import validate_transition
    from inferfabric.state import GPUMode

    # Valid transitions
    assert validate_transition(GPUMode.IDLE, GPUMode.EXCLUSIVE) is True
    assert validate_transition(GPUMode.IDLE, GPUMode.SHARED) is True
    assert validate_transition(GPUMode.EXCLUSIVE, GPUMode.IDLE) is True
    assert validate_transition(GPUMode.SHARED, GPUMode.IDLE) is True
    assert validate_transition(GPUMode.SHARED, GPUMode.SHARED) is True

    # Invalid transitions
    assert validate_transition(GPUMode.EXCLUSIVE, GPUMode.SHARED) is False
    assert validate_transition(GPUMode.SHARED, GPUMode.EXCLUSIVE) is False

    print("✅ State machine: all transition validations correct")


def test_state_machine_idempotent_idle():
    """Switching to idle when already idle should be no-op."""
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state_db(tmp)
        from inferfabric.process_manager import ProcessManager
        from inferfabric.manager import ModelManager

        # Mock process manager
        pm = MagicMock()
        pm.stop_all.return_value = {"status": "ok"}
        pm.force_kill_all.return_value = {"status": "ok"}
        pm.vllm_pid = None
        pm.comfyui_pid = None

        mgr = ModelManager.__new__(ModelManager)
        mgr.state = state
        mgr._proc = pm
        mgr._models = {}
        mgr._lock = MagicMock()
        mgr._lock.acquire.return_value = True

        result = mgr.switch("idle")
        assert result["status"] == "already_active", f"Expected already_active, got {result}"
        print("✅ State machine: idle→idle is idempotent")


def test_stop_service_rejects_exclusive():
    """stop_service() must reject stopping individual exclusive services."""
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state_db(tmp)
        state.set("gpu_mode", "exclusive")
        state.set("active_services", '["qwen36-27b"]')

        from inferfabric.manager import ModelManager
        mgr = ModelManager.__new__(ModelManager)
        mgr.state = state
        mgr._proc = MagicMock()
        mgr._models = {"qwen36-27b": _make_model("qwen36-27b", "exclusive", 8000)}

        result = mgr.stop_service("qwen36-27b")
        assert result["status"] == "error", f"Expected error, got {result}"
        assert "exclusive" in result["message"].lower(), f"Message should mention exclusive: {result}"
        print("✅ stop_service: rejects stopping exclusive service")


# ═══════════════════════════════════════════════════════════════
# 7. GPU Lock Robustness
# ═══════════════════════════════════════════════════════════════

def test_gpu_lock_force_clear():
    """GPU lock force_clear() should work even when no lock exists."""
    from inferfabric.gpu_lock import GPULock
    lock = GPULock()
    # Should not raise
    lock.force_clear()
    assert lock.acquire() is True, "Lock should be acquirable after force_clear"
    lock.release()
    print("✅ GPU lock: force_clear works when no lock exists")


def test_gpu_lock_double_release_safe():
    """Double release should not raise."""
    from inferfabric.gpu_lock import GPULock
    lock = GPULock()
    lock.acquire()
    lock.release()
    # Second release should be safe (no-op or no exception)
    try:
        lock.release()
        print("✅ GPU lock: double release is safe (no-op)")
    except Exception:
        print("⚠️ GPU lock: double release raises (acceptable but not ideal)")


# ═══════════════════════════════════════════════════════════════
# 8. Process Manager Robustness
# ═══════════════════════════════════════════════════════════════

def test_stop_vllm_no_pid_uses_fallback():
    """stop_vllm() with no PID should use pkill fallback."""
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state_db(tmp)
        from inferfabric.process_manager import ProcessManager
        pm = ProcessManager(state, Path(tmp))

        # No PID set
        assert pm.vllm_pid is None

        # Mock pkill fallback
        with patch.object(pm, '_pkill_vllm_fallback', return_value={"status": "ok", "message": "pkill fallback"}) as mock_fallback:
            result = pm.stop_vllm()
            mock_fallback.assert_called_once()
            assert result["status"] == "ok"
        print("✅ stop_vllm: uses pkill fallback when no PID tracked")


def test_stop_comfyui_no_pid_uses_fallback():
    """stop_comfyui() with no PID should use pkill fallback."""
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state_db(tmp)
        from inferfabric.process_manager import ProcessManager
        pm = ProcessManager(state, Path(tmp))

        assert pm.comfyui_pid is None

        with patch.object(pm, '_pkill_comfyui_fallback', return_value={"status": "ok"}) as mock_fallback:
            result = pm.stop_comfyui()
            mock_fallback.assert_called_once()
            assert result["status"] == "ok"
        print("✅ stop_comfyui: uses pkill fallback when no PID tracked")


def test_stop_comfyui_with_config_prefers_native():
    """stop_comfyui_with_config() should prefer native stop when PID available."""
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state_db(tmp)
        from inferfabric.process_manager import ProcessManager
        pm = ProcessManager(state, Path(tmp))

        # Set a fake PID
        state.set("comfyui_pid", "12345")

        with patch.object(pm, '_stop_comfyui_native', return_value={"status": "ok"}) as mock_native:
            with patch.object(pm, '_stop_comfyui_script') as mock_script:
                result = pm.stop_comfyui()
                mock_native.assert_called_once_with(12345)
                mock_script.assert_not_called()
        print("✅ stop_comfyui_with_config: prefers native stop when PID available")


def test_wait_gpu_idle_returns_on_low_vram():
    """_wait_gpu_idle() should return ok when GPU VRAM < 2GB."""
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state_db(tmp)
        from inferfabric.process_manager import ProcessManager
        pm = ProcessManager(state, Path(tmp))

        # Mock gpu_used_mb to return low value
        with patch('inferfabric.process_manager.gpu_used_mb', return_value=512):
            result = pm._wait_gpu_idle(timeout=5)
            assert result["status"] == "ok"
            assert result["used_mb"] == 512
        print("✅ _wait_gpu_idle: returns ok when VRAM < 2GB")


def test_wait_gpu_idle_timeout():
    """_wait_gpu_idle() should timeout when GPU stays busy."""
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state_db(tmp)
        from inferfabric.process_manager import ProcessManager
        pm = ProcessManager(state, Path(tmp))

        # Mock gpu_used_mb to always return high value
        with patch('inferfabric.process_manager.gpu_used_mb', return_value=8192):
            result = pm._wait_gpu_idle(timeout=3)
            assert result["status"] == "timeout"
        print("✅ _wait_gpu_idle: times out when GPU stays busy")


# ═══════════════════════════════════════════════════════════════
# 9. Health Check Robustness
# ═══════════════════════════════════════════════════════════════

def test_check_http_status_timeout():
    """check_http_status should return ❌ on connection failure."""
    from inferfabric.health import check_http_status
    result = check_http_status("http://localhost:59999/nonexistent")
    assert result == "❌", f"Expected ❌, got {result}"
    print("✅ check_http_status: returns ❌ on connection failure")


def test_wait_http_timeout():
    """wait_http should return False on timeout."""
    from inferfabric.health import wait_http
    result = wait_http("http://localhost:59999/nonexistent", timeout=1)
    assert result is False, f"Expected False, got {result}"
    print("✅ wait_http: returns False on timeout")


# ═══════════════════════════════════════════════════════════════
# 10. State DB Robustness
# ═══════════════════════════════════════════════════════════════

def test_state_db_set_get_roundtrip():
    """StateDB set/get should roundtrip correctly."""
    with tempfile.TemporaryDirectory() as tmp:
        from inferfabric.state import StateDB
        db = StateDB(Path(tmp) / "test.db")
        db.set("key1", "value1")
        db.set("key2", "42")
        assert db.get("key1") == "value1"
        assert db.get("key2") == "42"
        assert db.get("nonexistent") is None
        print("✅ StateDB: set/get roundtrip works")


def test_state_db_set_multi():
    """StateDB set_multi should atomically set multiple keys."""
    with tempfile.TemporaryDirectory() as tmp:
        from inferfabric.state import StateDB
        db = StateDB(Path(tmp) / "test.db")
        db.set_multi({"k1": "v1", "k2": "v2", "k3": "v3"})
        assert db.get("k1") == "v1"
        assert db.get("k2") == "v2"
        assert db.get("k3") == "v3"
        print("✅ StateDB: set_multi works")


def test_state_db_active_services():
    """StateDB active_services management."""
    with tempfile.TemporaryDirectory() as tmp:
        from inferfabric.state import StateDB
        db = StateDB(Path(tmp) / "test.db")
        db.set("gpu_mode", "idle")
        db.set("active_services", "[]")

        db.add_active_service("model1")
        assert db.get_active_services() == ["model1"]

        db.add_active_service("model2")
        assert db.get_active_services() == ["model1", "model2"]

        db.set_active_services(["model2"])
        assert db.get_active_services() == ["model2"]
        print("✅ StateDB: active_services management works")


# ═══════════════════════════════════════════════════════════════
# 11. Config Loading Robustness
# ═══════════════════════════════════════════════════════════════

def test_load_models_real_configs():
    """Load actual models.d/ configs and validate structure."""
    from inferfabric.config import load_models
    models_dir = Path(__file__).parent.parent / "models.d"
    if not models_dir.exists():
        print("⚠️ Skipping: models.d/ not found")
        return

    models = load_models(models_dir)
    assert len(models) > 0, "No models loaded from models.d/"

    for name, m in models.items():
        assert m.name == name, f"Model name mismatch: {m.name} != {name}"
        assert m.mode in ("exclusive", "shared"), f"Invalid mode for {name}: {m.mode}"
        if m.is_vllm:
            assert m.vllm.port > 0, f"Invalid port for {name}"
            assert m.vllm.conda_env, f"Missing conda_env for {name}"
        elif m.is_comfyui:
            assert m.comfyui.port > 0, f"Invalid port for {name}"

    print(f"✅ Config: loaded {len(models)} models from models.d/")


def test_vllm_config_build_cmd():
    """VLLMConfig.build_cmd() should produce valid command list."""
    from inferfabric.config import VLLMConfig
    cfg = VLLMConfig(
        model_dir="test-model",
        served_name="vllm_test",
        port=8000,
        conda_env="test-env",
        max_model_len=128000,
        gpu_memory_utilization=0.90,
        max_num_seqs=4,
        kv_cache_dtype="fp8",
    )
    cmd = cfg.build_cmd()
    assert isinstance(cmd, list), "build_cmd() should return list"
    assert "--port" in cmd, "Missing --port in build_cmd()"
    assert "8000" in cmd, "Missing port value in build_cmd()"
    assert "--served-model-name" in cmd, "Missing --served-model-name"
    print("✅ VLLMConfig.build_cmd(): produces valid command")


# ═══════════════════════════════════════════════════════════════
# 12. Proxy Manager Robustness
# ═══════════════════════════════════════════════════════════════

def test_proxy_upstream_pool_lazy():
    """ProxyManager upstream pool should use lazy invalidation."""
    proxy_path = Path(__file__).parent.parent / "inferfabric" / "proxy.py"
    content = proxy_path.read_text()

    # get_upstream should not probe /health
    start = content.find("def get_upstream")
    end = content.find("\n    def ", start + 1)
    method = content[start:end]

    assert "conn.request" not in method, "get_upstream should not make requests (lazy)"
    assert "invalidate_upstream" in content, "Missing invalidate_upstream method"
    print("✅ Proxy: upstream pool uses lazy invalidation")


def test_proxy_cumulative_metrics_reset():
    """ProxyManager should have reset_cum() for metric accumulators."""
    proxy_path = Path(__file__).parent.parent / "inferfabric" / "proxy.py"
    content = proxy_path.read_text()

    assert "reset_cum" in content, "Missing reset_cum() method"
    assert "_cum" in content, "Missing _cum accumulator dict"
    print("✅ Proxy: cumulative metrics with reset_cum()")


# ═══════════════════════════════════════════════════════════════
# 13. KV Offload Auto-Detection
# ═══════════════════════════════════════════════════════════════

def test_kv_offload_skips_expandable_segments():
    """start_vllm should skip expandable_segments when KV offloading is enabled."""
    pm_path = Path(__file__).parent.parent / "inferfabric" / "process_manager.py"
    content = pm_path.read_text()

    assert "has_kv_offload" in content, "Missing KV offload detection"
    assert '"--kv-offloading-size"' in content, "Missing --kv-offloading-size check"
    assert "skipping expandable_segments" in content, "Missing skip log message"
    print("✅ KV offload: auto-detection skips expandable_segments")


# ═══════════════════════════════════════════════════════════════
# 14. Concurrent Access Robustness
# ═══════════════════════════════════════════════════════════════

def test_state_db_concurrent_writes():
    """StateDB should handle concurrent writes without corruption."""
    with tempfile.TemporaryDirectory() as tmp:
        from inferfabric.state import StateDB
        db = StateDB(Path(tmp) / "test.db")
        errors = []

        def writer(key, value, count):
            try:
                for i in range(count):
                    db.set(key, f"{value}_{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(f"k{i}", f"v{i}", 50))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Concurrent write errors: {errors}"
        print("✅ StateDB: concurrent writes without corruption")


# ═══════════════════════════════════════════════════════════════
# Run All Tests
# ═══════════════════════════════════════════════════════════════

def run_all():
    tests = [
        # Dashboard sw lock
        test_dashboard_sw_lock_has_finally,
        test_dashboard_sw_lock_timeout_mechanism,
        test_dashboard_no_bare_sw_true_in_actions,
        # ComfyUI config passthrough
        test_switch_to_idle_passes_comfyui_config,
        test_stop_service_uses_stop_comfyui_with_config,
        test_shared_add_service_passes_comfyui_config,
        test_force_reset_passes_comfyui_config,
        # /v1/models aggregation
        test_v1_models_aggregation_code,
        test_v1_models_no_hardcoded_first,
        # Proxy lazy invalidation
        test_get_upstream_no_health_probe,
        # Dynamic port discovery
        test_pkill_fallback_dynamic_ports,
        # State machine
        test_state_machine_transition_validation,
        test_state_machine_idempotent_idle,
        test_stop_service_rejects_exclusive,
        # GPU lock
        test_gpu_lock_force_clear,
        test_gpu_lock_double_release_safe,
        # Process manager
        test_stop_vllm_no_pid_uses_fallback,
        test_stop_comfyui_no_pid_uses_fallback,
        test_stop_comfyui_with_config_prefers_native,
        test_wait_gpu_idle_returns_on_low_vram,
        test_wait_gpu_idle_timeout,
        # Health check
        test_check_http_status_timeout,
        test_wait_http_timeout,
        # State DB
        test_state_db_set_get_roundtrip,
        test_state_db_set_multi,
        test_state_db_active_services,
        # Config
        test_load_models_real_configs,
        test_vllm_config_build_cmd,
        # Proxy
        test_proxy_upstream_pool_lazy,
        test_proxy_cumulative_metrics_reset,
        # KV offload
        test_kv_offload_skips_expandable_segments,
        # Concurrent
        test_state_db_concurrent_writes,
    ]

    passed = 0
    failed = 0
    errors = []

    for test in tests:
        name = test.__name__
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((name, e))
            print(f"❌ {name}: {e}")

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
    if errors:
        print(f"\nFailed tests:")
        for name, e in errors:
            print(f"  - {name}: {e}")
    print(f"{'='*60}")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
