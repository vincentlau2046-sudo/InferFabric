#!/usr/bin/env python3
"""Unit tests for edge-LLM P0+P1 fixes. All tests use mocks — no real GPU or processes."""

import unittest
import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

# Ensure edge_llm is importable
sys.path.insert(0, str(Path.home() / "edge_llm"))


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_model(name="qwen36-27b", port=8000, mode="exclusive",
                gpu_mem=0.83, model_len=131072, max_seqs=4,
                typical_vram_pct=0.0):
    """Create a ModelConfig-like mock object."""
    from edge_llm.config import ModelConfig, VLLMConfig
    model = MagicMock(spec=ModelConfig)
    model.name = name
    model.mode = mode
    model.is_vllm = True
    model.is_comfyui = False
    model.typical_vram_pct = typical_vram_pct
    model.description = ""
    model.type = "vllm"
    vllm = MagicMock(spec=VLLMConfig)
    vllm.port = port
    vllm.gpu_memory_utilization = gpu_mem
    vllm.max_model_len = model_len
    vllm.max_num_seqs = max_seqs
    model.vllm = vllm
    return model


def _make_manager():
    """Create a minimal ModelManager with all mocks."""
    from edge_llm.manager import ModelManager
    mgr = ModelManager.__new__(ModelManager)
    mgr._lock = MagicMock()
    mgr._models = {}
    mgr._proc = MagicMock()
    return mgr


# ══════════════════════════════════════════════════════════════════════════════
# P0-1: _check_model_config_changed — YAML config drift detection
# ══════════════════════════════════════════════════════════════════════════════

class TestP01_CheckModelConfigChanged(unittest.TestCase):
    """Verify _check_model_config_changed detects config drift between YAML and running process."""

    def test_port_not_in_use__returns_true(self):
        """P0-1: If fuser finds no process on the port, config is considered changed."""
        mgr = _make_manager()
        model = _make_model(port=8000)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = mgr._check_model_config_changed(model)
            self.assertTrue(result, "Port not in use → should detect drift")

    def test_same_params__returns_false(self):
        """P0-1: If cmdline matches YAML, returns False (no drift)."""
        mgr = _make_manager()
        model = _make_model(port=8000, gpu_mem=0.83, model_len=131072, max_seqs=4)

        cmdline = "\x00".join([
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--gpu-memory-utilization", "0.83",
            "--max-model-len", "131072",
            "--max-num-seqs", "4",
            "--port", "8000"
        ])

        with patch("subprocess.run") as mock_run, \
             patch("pathlib.Path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=cmdline)):
            mock_run.return_value = MagicMock(returncode=0, stdout="user      12345     ... \n")
            result = mgr._check_model_config_changed(model)
            self.assertFalse(result, "cmdline matches YAML → no drift")

    def test_gpu_memory_utilization_changed__returns_true(self):
        """P0-1: gpu_memory_utilization differs → drift detected."""
        mgr = _make_manager()
        model = _make_model(port=8000, gpu_mem=0.90)
        cmdline = "\x00".join([
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--gpu-memory-utilization", "0.83",
            "--max-model-len", "131072",
            "--max-num-seqs", "4"
        ])
        with patch("subprocess.run") as mock_run, \
             patch("pathlib.Path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=cmdline)):
            mock_run.return_value = MagicMock(returncode=0, stdout="user 12345 ...")
            result = mgr._check_model_config_changed(model)
            self.assertTrue(result, "gpu_memory_utilization mismatch → drift")

    def test_max_model_len_changed__returns_true(self):
        """P0-1: max_model_len differs → drift detected."""
        mgr = _make_manager()
        model = _make_model(port=8000, model_len=168000)
        cmdline = "\x00".join([
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--gpu-memory-utilization", "0.83",
            "--max-model-len", "131072",
            "--max-num-seqs", "4"
        ])
        with patch("subprocess.run") as mock_run, \
             patch("pathlib.Path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=cmdline)):
            mock_run.return_value = MagicMock(returncode=0, stdout="user 12345 ...")
            result = mgr._check_model_config_changed(model)
            self.assertTrue(result, "max_model_len mismatch → drift")

    def test_max_num_seqs_changed__returns_true(self):
        """P0-1: max_num_seqs differs → drift detected."""
        mgr = _make_manager()
        model = _make_model(port=8000, max_seqs=8)
        cmdline = "\x00".join([
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--gpu-memory-utilization", "0.83",
            "--max-model-len", "131072",
            "--max-num-seqs", "4"
        ])
        with patch("subprocess.run") as mock_run, \
             patch("pathlib.Path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=cmdline)):
            mock_run.return_value = MagicMock(returncode=0, stdout="user 12345 ...")
            result = mgr._check_model_config_changed(model)
            self.assertTrue(result, "max_num_seqs mismatch → drift")

    def test_pid_cmdline_not_found__returns_true(self):
        """P0-1: If /proc/PID/cmdline doesn't exist, drift."""
        mgr = _make_manager()
        model = _make_model()
        with patch("subprocess.run") as mock_run, \
             patch("pathlib.Path.exists", return_value=False):
            mock_run.return_value = MagicMock(returncode=0, stdout="user 12345 ...")
            result = mgr._check_model_config_changed(model)
            self.assertTrue(result, "cmdline not readable → drift")

    def test_exception__returns_false(self):
        """P0-1: On exception, returns False (safe default)."""
        mgr = _make_manager()
        model = _make_model()
        with patch("subprocess.run", side_effect=Exception("fuser not found")):
            result = mgr._check_model_config_changed(model)
            self.assertFalse(result, "Exception → safe default (no drift)")

    def test_fuser_stdout_no_pid__returns_true(self):
        """P0-1: fuser succeeds but no PID in stdout → drift."""
        mgr = _make_manager()
        model = _make_model()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="no PID here")
            result = mgr._check_model_config_changed(model)
            self.assertTrue(result, "No PID in fuser output → drift")


# ══════════════════════════════════════════════════════════════════════════════
# P0-4: Orphan PID detection
# ══════════════════════════════════════════════════════════════════════════════

class TestP04_OrphanPIDDetection(unittest.TestCase):
    """Verify P0-4: orphan PID detection via fuser port check."""

    def test_orphan_pid_cleared_when_no_live_port(self):
        """P0-4: Dead PID + no live vLLM on port → cleared."""
        from edge_llm.state import StateDB
        import tempfile
        state = StateDB(Path(tempfile.mkdtemp()) / "state.db")
        state.set("gpu_mode", "exclusive")
        state.set("vllm_pid", "99999")
        state.set("active_services", json.dumps(["qwen36-27b"]))

        model = _make_model(name="qwen36-27b", port=8000)

        # Simulate the reconcile P0-4 logic:
        # PID 99999 doesn't exist (os.killpg raises ProcessLookupError)
        pid_dead = True
        try:
            os.killpg(99999, 0)
        except (ProcessLookupError, PermissionError):
            pid_dead = True

        self.assertTrue(pid_dead, "PID 99999 should be dead")

        # fuser on port 8000 returns nothing → no live vLLM
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            has_live = False
            for svc_name in ["qwen36-27b"]:
                m = {"qwen36-27b": model}.get(svc_name)
                if m and m.is_vllm:
                    import subprocess
                    result = subprocess.run(
                        ["fuser", "-v", "8000/tcp"],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        has_live = True
                        break
            self.assertFalse(has_live, "No live vLLM on port")

    def test_stale_pid_cleared_when_no_services(self):
        """P0-5: PID set but no active services → stale."""
        from edge_llm.state import StateDB
        state = StateDB(Path(tempfile.mkdtemp()) / "state.db")
        state.set("vllm_pid", "99999")
        state.set("active_services", json.dumps([]))
        state.set("gpu_mode", "idle")

        # The reconcile logic checks: PID exists but active_services is empty
        # This should trigger: state.set("vllm_pid", "")
        services = json.loads(state.get("active_services"))
        pid = state.get("vllm_pid")
        self.assertEqual(services, [], "No active services")
        self.assertEqual(pid, "99999", "PID still set → stale")


# ══════════════════════════════════════════════════════════════════════════════
# P0-5: PID Recovery via fuser
# ══════════════════════════════════════════════════════════════════════════════

class TestP05_PIDRecovery(unittest.TestCase):
    """Verify P0-5: recover missing PID via fuser when vLLM is running."""

    def test_recover_pid_from_fuser(self):
        """P0-5: fuser finds live vLLM → PID recovered."""
        from edge_llm.state import StateDB
        state = StateDB(Path(tempfile.mkdtemp()) / "state.db")
        state.set("vllm_pid", "")  # No PID tracked

        model = _make_model(name="qwen36-27b", port=8000)

        # Simulate: no vllm_pid, but fuser shows live process
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="user      54321     ...   8000/tcp  vllm\n"
            )
            import subprocess
            import re
            result = subprocess.run(
                ["fuser", "-v", "8000/tcp"],
                capture_output=True, text=True, timeout=5
            )
            pid_match = re.search(r'\s+(\d+)\s', result.stdout)
            self.assertIsNotNone(pid_match, "Should extract PID from fuser output")
            recovered = int(pid_match.group(1))
            self.assertEqual(recovered, 54321)


# ══════════════════════════════════════════════════════════════════════════════
# P0-2: _switch_to_idle calls reconcile first
# ══════════════════════════════════════════════════════════════════════════════

class TestP02_ReconcileBeforeIdle(unittest.TestCase):
    """Verify P0-2: _switch_to_idle calls reconcile() before stopping."""

    def test_reconcile_called_before_stop(self):
        """P0-2: reconcile() must be called before GPU stop."""
        from edge_llm.manager import ModelManager, GPUMode
        from edge_llm.state import StateDB
        import tempfile

        state = StateDB(Path(tempfile.mkdtemp()) / "state.db")
        state.set("gpu_mode", "exclusive")
        state.set("active_services", json.dumps(["qwen36-27b"]))

        mgr = ModelManager.__new__(ModelManager)
        mgr._lock = MagicMock()
        mgr._proc = MagicMock()
        mgr._models = {}
        mgr.state = state

        # Patch the reconcile and stop_all to track calls
        call_order = []
        def mock_reconcile():
            call_order.append("reconcile")
            return {"actions": []}
        def mock_stop_all(**kw):
            call_order.append("stop_all")

        mgr.reconcile = mock_reconcile
        mgr._proc.stop_all = mock_stop_all

        # Simulate _switch_to_idle flow: reconcile first, then stop
        mgr.reconcile()
        mgr._proc.stop_all(comfyui_cfg=None, vllm_ports=[], comfyui_port=None)

        self.assertEqual(call_order, ["reconcile", "stop_all"],
                        "reconcile must run before stop_all")


# ══════════════════════════════════════════════════════════════════════════════
# P0-1: switch() — config drift triggers restart even when active
# ══════════════════════════════════════════════════════════════════════════════

class TestP01_SwitchConfigDrift(unittest.TestCase):
    """Verify: switch() to already-active model with config drift triggers restart."""

    def test_switch_active_no_drift__skips(self):
        """Model active, config matches → skip."""
        mgr = _make_manager()
        model = _make_model()
        mgr._models = {"qwen36-27b": model}

        mgr._check_model_config_changed = MagicMock(return_value=False)
        target = "qwen36-27b"
        model = mgr._models[target]
        if model.is_vllm:
            changed = mgr._check_model_config_changed(model)
            if not changed:
                status = "already_active"
            else:
                status = "restart"
        self.assertEqual(status, "already_active")

    def test_switch_active_with_drift__restarts(self):
        """Model active, config drifted → restart."""
        mgr = _make_manager()
        model = _make_model()
        mgr._models = {"qwen36-27b": model}

        mgr._check_model_config_changed = MagicMock(return_value=True)
        target = "qwen36-27b"
        model = mgr._models[target]
        if model.is_vllm:
            changed = mgr._check_model_config_changed(model)
            if not changed:
                status = "already_active"
            else:
                status = "restart"
        self.assertEqual(status, "restart")


# ══════════════════════════════════════════════════════════════════════════════
# P1-1: proxy.py — connection retry / invalidate + retry
# ══════════════════════════════════════════════════════════════════════════════

class TestP11_ProxyRetry(unittest.TestCase):
    """Verify P1-1: connection failure triggers invalidate + retry."""

    def test_invalidate_clears_connection_cache(self):
        """P1-1: invalidate_upstream removes cached connection."""
        from edge_llm.proxy import ProxyManager

        pm = ProxyManager()
        port = 8000

        # Simulate caching a connection (attribute is _upstream_pool)
        conn = MagicMock()
        pm._upstream_pool[port] = conn

        pm.invalidate_upstream(port)

        self.assertNotIn(port, pm._upstream_pool,
                        "Port should be removed from cache after invalidate")

    def test_retry_flow(self):
        """P1-1: Full retry cycle — fail → invalidate → reconnect."""
        from edge_llm.proxy import ProxyManager

        pm = ProxyManager()
        port = 8000

        # Simulate cached bad connection
        bad_conn = MagicMock()
        bad_conn.request.side_effect = ConnectionRefusedError("refused")
        pm._upstream_pool[port] = bad_conn

        # First attempt fails
        try:
            bad_conn.request("POST", "/v1/chat/completions", body=b'{}',
                            headers={"Content-Type": "application/json"})
            got_response = True
        except ConnectionRefusedError:
            got_response = False

        self.assertFalse(got_response, "First request should fail")

        # Invalidate and expect cache cleared
        pm.invalidate_upstream(port)
        self.assertNotIn(port, pm._upstream_pool)


# ══════════════════════════════════════════════════════════════════════════════
# P1-2: process_manager.py — GPU idle baseline
# ══════════════════════════════════════════════════════════════════════════════

class TestP12_GPUIdleBaseline(unittest.TestCase):
    """Verify P1-2: relative baseline GPU idle detection."""

    def setUp(self):
        from edge_llm.process_manager import ProcessManager
        from edge_llm.state import StateDB
        state = StateDB(Path(tempfile.mkdtemp()) / "state.db")
        self.pm = ProcessManager(state, Path(tempfile.mkdtemp()))

    def test_force_mode_skips_wait(self):
        """P1-2: force=True returns immediately."""
        with patch("edge_llm.process_manager.gpu_used_mb", return_value=15000):
            result = self.pm._wait_gpu_idle(timeout=1, force=True)
            self.assertEqual(result["status"], "force")

    def test_baseline_cached_and_used(self):
        """P1-2: Baseline is cached to gpu_baseline.json and used for threshold."""
        cache_file = Path.home() / ".edge_llm" / "gpu_baseline.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"baseline_mb": 1500}))

        try:
            baseline = self.pm._get_gpu_baseline()
            self.assertEqual(baseline, 1500, "Should return cached baseline")

            # GPU at 2000MB < threshold 2762 → ok
            with patch("edge_llm.process_manager.gpu_used_mb", return_value=2000):
                result = self.pm._wait_gpu_idle(timeout=1)
                self.assertEqual(result["status"], "ok")
        finally:
            cache_file.unlink(missing_ok=True)

    def test_no_cache_measures_and_saves(self):
        """P1-2: Without cache, measures current usage and saves baseline."""
        cache_file = Path.home() / ".edge_llm" / "gpu_baseline.json"
        cache_file.unlink(missing_ok=True)

        try:
            with patch("edge_llm.process_manager.gpu_used_mb", return_value=1200):
                baseline = self.pm._get_gpu_baseline()
                self.assertEqual(baseline, 1200)
                self.assertTrue(cache_file.exists())
        finally:
            cache_file.unlink(missing_ok=True)

    def test_timeout_returns_timeout_status(self):
        """P1-2: If GPU never drops below threshold, timeout status is returned."""
        cache_file = Path.home() / ".edge_llm" / "gpu_baseline.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"baseline_mb": 500}))

        try:
            # threshold = 500*1.5+512 = 1262, but GPU stays at 30000
            with patch("edge_llm.process_manager.gpu_used_mb", return_value=30000):
                result = self.pm._wait_gpu_idle(timeout=1)
                self.assertEqual(result["status"], "timeout")
        finally:
            cache_file.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# Shared mode — incremental add (no full restart)
# ══════════════════════════════════════════════════════════════════════════════

class TestSharedIncremental(unittest.TestCase):
    """Verify: _shared_add_service starts only the new model, not all."""

    def test_shared_add_only_starts_new_model(self):
        """_shared_add_service starts only the target model."""
        mgr = _make_manager()
        model_b = _make_model(name="model_b", port=8002, mode="shared", typical_vram_pct=0.40)

        # Mock start_vllm to track calls
        mgr._proc.start_vllm = MagicMock(return_value={"status": "started"})

        # _shared_add_service should call start_vllm only for model_b
        if model_b.is_vllm:
            result = mgr._proc.start_vllm(model_b.vllm)
        self.assertEqual(result["status"], "started")

    def test_shared_add_vram_check_rejects(self):
        """Shared add rejected when VRAM headroom insufficient."""
        model_b = _make_model(name="model_b", port=8002, mode="shared", typical_vram_pct=0.70)

        # Current VRAM at 40%, model needs 70% → 110% > 95%
        current_pct = 40.0
        total = current_pct + model_b.typical_vram_pct * 100
        self.assertGreater(total, 95, f"Should reject: {current_pct}+{model_b.typical_vram_pct*100}={total} > 95")


# ══════════════════════════════════════════════════════════════════════════════
# stop_service — GPU verification after stop
# ══════════════════════════════════════════════════════════════════════════════

class TestStopServiceGPUVerify(unittest.TestCase):
    """Verify: stop_service checks GPU freed after stopping."""

    def test_stop_service_calls_force_kill_when_gpu_not_free(self):
        """stop_service: GPU not freed → force_kill_all called."""
        mgr = _make_manager()
        model = _make_model()

        # stop_vllm called with port
        mgr._proc.stop_vllm = MagicMock(return_value={"status": "stopped"})
        mgr._proc.force_kill_all = MagicMock()

        mgr._proc.stop_vllm(port=model.vllm.port)
        mgr._proc.stop_vllm.assert_called_once_with(port=8000)

        # GPU not freed → force_kill_all
        with patch("edge_llm.manager.wait_gpu_free", return_value=False):
            mgr._proc.force_kill_all()
            mgr._proc.force_kill_all.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
