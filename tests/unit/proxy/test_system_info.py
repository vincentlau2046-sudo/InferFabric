"""
unit/proxy/test_system_info.py — _system_info GPU 温度采集测试

根因：dashboard "温度 — °C" 始终显示 —。前端链路已通（trTemp 占位符 →
overview.js 消费 util.temp → store.js 映射 sys.gpu_temp_c），但后端
_system_info 的 nvidia-smi query 没查 temperature.gpu → snapshot 无 gpu_temp_c。

本测试断言 _system_info 产出 gpu_temp_c（从 nvidia-smi 第 4 字段解析），
且字段缺失/非数字时不抛、不设键（graceful 降级 → 前端显示 —）。
"""

import sys
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_deps = Path(__file__).parent.parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import pytest


def _make_handler():
    """ProxyHandler with minimal mocked state."""
    import inferfabric.proxy.handler as handler_module
    ProxyHandler = handler_module.ProxyHandler
    h = ProxyHandler.__new__(ProxyHandler)
    return h, handler_module


class _FakeCompleted:
    """模拟 subprocess.run 返回值。"""
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


class TestSystemInfoGpuTemp:
    def test_gpu_temp_c_parsed_from_nvidia_smi(self, monkeypatch):
        """nvidia-smi 4 字段 stdout → gpu_temp_c 正确解析（round 1 位）。"""
        h, _ = _make_handler()
        # nvidia-smi 实测格式：util, clock, power, temp（逗号后可能带空格）
        monkeypatch.setattr(subprocess, "run", lambda *a, **k:
            _FakeCompleted(stdout="100, 2857, 496.36, 55"))

        info = h._system_info()

        assert "gpu_temp_c" in info, f"缺 gpu_temp_c: {info}"
        assert info["gpu_temp_c"] == 55.0, f"温度解析错误: {info['gpu_temp_c']}"
        # 既有字段不受影响
        assert info["gpu_util_pct"] == 100.0
        assert info["gpu_clock_mhz"] == 2857
        assert info["gpu_power_w"] == 496.4, f"power round 到 1 位: {info['gpu_power_w']}"

    def test_gpu_temp_c_rounds_decimal(self, monkeypatch):
        """温度带小数 → round 到 1 位。"""
        h, _ = _make_handler()
        monkeypatch.setattr(subprocess, "run", lambda *a, **k:
            _FakeCompleted(stdout="50, 2100, 200.5, 67.45"))

        info = h._system_info()
        assert info["gpu_temp_c"] == 67.5, f"应 round 到 1 位: {info['gpu_temp_c']}"

    def test_gpu_temp_c_absent_when_field_missing(self, monkeypatch):
        """旧版 nvidia-smi 无 temperature.gpu 字段（只 3 值）→ 不抛、不设键。"""
        h, _ = _make_handler()
        monkeypatch.setattr(subprocess, "run", lambda *a, **k:
            _FakeCompleted(stdout="100, 2857, 496.36"))

        # 不应抛 IndexError
        info = h._system_info()
        assert "gpu_temp_c" not in info, \
            f"字段缺失时不应设 gpu_temp_c: {info}"
        # 既有字段仍正常解析
        assert info["gpu_util_pct"] == 100.0

    def test_gpu_temp_c_absent_when_nvidia_smi_fails(self, monkeypatch):
        """nvidia-smi 失败（非零 rc / 无 GPU）→ 不抛、不设键（graceful 降级）。"""
        h, _ = _make_handler()
        monkeypatch.setattr(subprocess, "run", lambda *a, **k:
            _FakeCompleted(stdout="", returncode=1))

        info = h._system_info()
        assert "gpu_temp_c" not in info, \
            f"nvidia-smi 失败时不应设 gpu_temp_c: {info}"

    def test_gpu_temp_c_absent_when_subprocess_raises(self, monkeypatch):
        """subprocess.run 抛异常（如 nvidia-smi 不在 PATH）→ 不抛、不设键。"""
        h, _ = _make_handler()

        def raise_fnf(*a, **k):
            raise FileNotFoundError("nvidia-smi not found")

        monkeypatch.setattr(subprocess, "run", raise_fnf)
        info = h._system_info()
        assert "gpu_temp_c" not in info
