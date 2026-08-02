"""tests/test_gap_phase1_process_kill.py — D-2: process termination precision"""

import os
import signal
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from inferfabric.process_manager import ProcessManager
from inferfabric.state import StateDB


@pytest.fixture
def state_db():
    with tempfile.TemporaryDirectory() as d:
        db = StateDB(Path(d) / "state.db")
        yield db


@pytest.fixture
def pm(state_db, tmp_path):
    return ProcessManager(state=state_db, log_dir=tmp_path)


class TestPkillVllmFallback:
    """Verify _pkill_vllm_fallback does NOT use global pkill -f patterns."""

    @patch("inferfabric.process_manager.subprocess.run")
    @patch("inferfabric.process_manager.os.kill")
    def test_fallback_uses_pid_files_not_pkill_f_global(self, mock_kill, mock_run, pm):
        """_pkill_vllm_fallback should use PID files, never pkill -f for global patterns."""
        # No PID files exist by default
        result = pm._pkill_vllm_fallback()

        assert result["status"] == "ok"

        # Collect all pkill calls
        pkill_calls = [
            c for c in mock_run.call_args_list
            if isinstance(c[0][0], list) and c[0][0][0] == "pkill"
        ]

        # Verify no global pkill -9 -f patterns are used
        for c in pkill_calls:
            args = c[0][0]
            # args is like ["pkill", "-f", "pattern"] — check the pattern
            for arg in args:
                if isinstance(arg, str):
                    assert "vllm serve" not in arg.lower(), \
                        f"Should not use pkill -f 'vllm serve': {args}"
                    assert "VLLM::EngineCore" not in arg, \
                        f"Should not use pkill -f 'VLLM::EngineCore': {args}"

    def test_pid_file_kill_path(self, pm, tmp_path):
        """When PID files exist, fallback should kill those PIDs."""
        pid_file = tmp_path / "vllm_test_env.pid"
        pid_file.write_text("424242")

        with patch("inferfabric.process_manager.os.kill") as mock_kill, \
             patch("inferfabric.process_manager.subprocess.run") as mock_run, \
             patch.object(pm, "_wait_gpu_idle", return_value={"status": "ok"}):

            result = pm._pkill_vllm_fallback()

            assert result["status"] == "ok"
            # Should have sent SIGTERM and SIGKILL to pid 424242
            mock_kill.assert_any_call(424242, signal.SIGTERM)
            mock_kill.assert_any_call(424242, signal.SIGKILL)

    @patch("inferfabric.process_manager.subprocess.run")
    def test_fallback_uses_fuser_not_pkill_f(self, mock_run, pm, tmp_path):
        """When no PID files exist, fallback should use fuser on ports, not pkill -f."""
        # Create a PID file with a dead PID so we can test the fuser fallback
        # Actually, let's just test that fuser gets called via _pkill_by_port
        with patch.object(pm, "_pkill_by_port") as mock_port_kill, \
             patch.object(pm, "_wait_gpu_idle", return_value={"status": "ok"}):

            result = pm._pkill_vllm_fallback()
            assert result["status"] == "ok"

            # _pkill_by_port should be called for each fallback port
            assert mock_port_kill.call_count >= 3, \
                f"Expected at least 3 port-based kills, got {mock_port_kill.call_count}"

    def test_no_paramiko_killall(self, pm):
        """Verify the fallback does not import or use paramiko/remote kill."""
        import inspect
        source = inspect.getsource(pm._pkill_vllm_fallback)
        assert "pkill -9 -f" not in source, \
            "Source should not contain 'pkill -9 -f'"
        assert "paramiko" not in source, \
            "Source should not contain 'paramiko'"


class TestPkillByPortFuser:
    """Verify _pkill_by_port uses fuser, not pkill."""

    def test_fuser_called(self, pm):
        """_pkill_by_port should call fuser on the specific port."""
        with patch("inferfabric.process_manager.subprocess.run") as mock_run:
            pm._pkill_by_port(8000)
            fuser_calls = [
                c for c in mock_run.call_args_list
                if isinstance(c[0][0], list) and c[0][0][0] == "fuser"
            ]
            assert len(fuser_calls) > 0, "_pkill_by_port should call fuser"
