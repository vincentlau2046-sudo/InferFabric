"""
unit/engine/test_stop_all.py — ProcessManager.stop_all 回归测试

Task 2.3: stop_all 在 active_services 给定时不再重复调 stop_<engine>
（服务已由调用方经 engine adapter 停止），仅清理 tracked PIDs。
legacy active_services=None 路径（bare stop everything）保持不变。
"""

import tempfile
from pathlib import Path

import pytest

from inferfabric.process_manager import ProcessManager
from inferfabric.state import StateDB


def _make_pm_with_tracked_pids(
    vllm_pid: int | None = None,
    sglang_pid: int | None = None,
    comfyui_pid: int | None = None,
    tts_pid: int | None = None,
    asr_pid: int | None = None,
):
    """Build a real ProcessManager backed by a temp StateDB with tracked PIDs set."""
    tmpdir = tempfile.mkdtemp()
    state = StateDB(Path(tmpdir) / "state.db")
    state.set("vllm_pid", str(vllm_pid) if vllm_pid else "")
    state.set("sglang_pid", str(sglang_pid) if sglang_pid else "")
    state.set("comfyui_pid", str(comfyui_pid) if comfyui_pid else "")
    state.set("tts_pid", str(tts_pid) if tts_pid else "")
    state.set("asr_pid", str(asr_pid) if asr_pid else "")
    return ProcessManager(state, log_dir=Path(tmpdir))


# ═══════════════════════════════════════════════════════════════════
# 1. active_services given — no restop, only PID cleanup (Task 2.3)
# ═══════════════════════════════════════════════════════════════════

class TestStopAllWithActiveServices:
    """Task 2.3: stop_all(active_services=...) must NOT re-stop engines."""

    def test_stop_all_with_active_services_does_not_restop(self, monkeypatch):
        """stop_all 在 active_services 给定时仅清 PID，不重复调 stop_vllm 等。"""
        pm = _make_pm_with_tracked_pids(
            vllm_pid=999, sglang_pid=998, comfyui_pid=1000,
            tts_pid=1001, asr_pid=1002,
        )
        calls = []
        monkeypatch.setattr(pm, "stop_vllm", lambda **k: calls.append("vllm"))
        monkeypatch.setattr(pm, "stop_sglang", lambda **k: calls.append("sglang"))
        monkeypatch.setattr(pm, "stop_comfyui", lambda **k: calls.append("comfyui"))
        monkeypatch.setattr(
            pm, "stop_comfyui_with_config",
            lambda cfg, port=None: calls.append("comfyui_with_config"),
        )
        monkeypatch.setattr(pm, "stop_tts_server", lambda **k: calls.append("tts"))
        monkeypatch.setattr(pm, "stop_asr_server", lambda **k: calls.append("asr"))

        pm.stop_all(active_services=["ovis-ocr2"])

        assert calls == [], "active_services 给定时 stop_all 不应重复停止（已由 adapter 停）"
        assert pm.vllm_pid is None, "应清理 tracked PID"
        assert pm.sglang_pid is None
        assert pm.comfyui_pid is None
        assert pm.tts_pid is None
        assert pm.asr_pid is None

    def test_stop_all_active_services_explicit_ports_still_stop(self, monkeypatch):
        """active_services 给定时，显式 port 参数路径仍生效（调用方显式指定端口）。"""
        pm = _make_pm_with_tracked_pids(vllm_pid=999)
        calls = []
        monkeypatch.setattr(pm, "stop_vllm", lambda port=None: calls.append(port))

        pm.stop_all(vllm_ports=[8000, 8001], active_services=["ovis-ocr2"])

        assert 8000 in calls and 8001 in calls, "显式 vllm_ports 路径应保留"
        assert pm.vllm_pid is None, "显式停止后仍应清理 tracked PID"

    def test_stop_all_active_services_comfyui_cfg_still_stops(self, monkeypatch):
        """active_services 给定时，显式 comfyui_cfg 仍触发优雅 ComfyUI 停止
        （force_reset 路径，R6 accepted window）。"""
        from inferfabric.config import ComfyUIConfig

        pm = _make_pm_with_tracked_pids(comfyui_pid=1000)
        calls = []
        monkeypatch.setattr(
            pm, "stop_comfyui_with_config",
            lambda cfg, port=None: calls.append(("comfyui", port)),
        )
        monkeypatch.setattr(pm, "stop_vllm", lambda **k: calls.append("vllm"))

        pm.stop_all(comfyui_cfg=ComfyUIConfig(), active_services=["comfyui"])

        assert calls == [("comfyui", 8188)], "显式 comfyui_cfg 路径应保留"
        assert pm.comfyui_pid is None, "应清理 tracked PID"


# ═══════════════════════════════════════════════════════════════════
# 2. Legacy path (active_services=None) — unchanged
# ═══════════════════════════════════════════════════════════════════

class TestStopAllLegacy:
    """Legacy bare stop_all() still stops everything via bare stop_<engine>."""

    def test_stop_all_legacy_stops_everything(self, monkeypatch):
        """active_services=None（默认）：bare stop_vllm/stop_comfyui/stop_tts/stop_asr 全触发。"""
        pm = _make_pm_with_tracked_pids(
            vllm_pid=999, comfyui_pid=1000, tts_pid=1001, asr_pid=1002,
        )
        calls = []
        monkeypatch.setattr(pm, "stop_vllm", lambda **k: calls.append("vllm"))
        monkeypatch.setattr(pm, "stop_sglang", lambda **k: calls.append("sglang"))
        monkeypatch.setattr(pm, "stop_comfyui", lambda **k: calls.append("comfyui"))
        monkeypatch.setattr(pm, "stop_tts_server", lambda **k: calls.append("tts"))
        monkeypatch.setattr(pm, "stop_asr_server", lambda **k: calls.append("asr"))

        pm.stop_all()

        assert "vllm" in calls, "legacy 路径应 bare stop vLLM"
        assert "comfyui" in calls, "legacy 路径应 bare stop ComfyUI"
        assert "tts" in calls, "legacy 路径应 bare stop TTS"
        assert "asr" in calls, "legacy 路径应 bare stop ASR"
        assert "sglang" not in calls, "SGLang 仅经显式 ports 停止（无 bare stop）"
