"""
unit/engine/test_validate_pid.py — _validate_pid PID 复用保护测试（D1 修复）

根因：BaseProcessManager._validate_pid 解码 /proc/<pid>/cmdline 后函数体
结束，缺最终 return → 成功路径隐式返回 None（falsy）。P1-2 PID 复用保护
在全部调用方失效：
- vllm stop_vllm 恒走 "stale PID" 分支 → 优雅 killpg 成死代码，
  VLLM::EngineCore 子进程可孤儿化继续占 VRAM；
- tts stop_tts_server(port=None) 恒清 PID 后返回 "not running"，不杀进程；
- comfyui 裸 stop 恒走 pkill 兜底，native 优雅路径不可达。

本测试锁定 _validate_pid 的完整语义：cmdline 匹配（大小写不敏感）→ True；
PID 已复用为无关进程 / 不存在 / 空 cmdline（内核线程）→ False；
PermissionError → 保守假设仍有效（True）。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))


def _make_pm():
    from inferfabric.process_manager.base import BaseProcessManager
    return BaseProcessManager.__new__(BaseProcessManager)


def _patch_proc_cmdline(monkeypatch, pid: int, cmdline: bytes | None):
    """让 /proc/<pid>/cmdline 的 read_bytes 返回指定内容（None = 不存在）。"""
    def fake_read_bytes(self):
        target = f"/proc/{pid}/cmdline"
        if str(self) != target:
            raise FileNotFoundError(target)
        if cmdline is None:
            raise FileNotFoundError(target)
        return cmdline

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)


class TestValidatePid:
    def test_true_when_cmdline_matches_substring(self, monkeypatch):
        """cmdline 含期望子串（大小写不敏感）→ True。修复前隐式 None。"""
        pm = _make_pm()
        _patch_proc_cmdline(monkeypatch, 4242,
                            b"python\x00-main.py\x00\x00--port\x008001\x00VLLM\x00")
        assert pm._validate_pid(4242, "vllm") is True

    def test_false_when_pid_recycled_unrelated(self, monkeypatch):
        """PID 被内核复用到无关进程（cmdline 不含子串）→ False。修复前隐式 None。"""
        pm = _make_pm()
        _patch_proc_cmdline(monkeypatch, 4242,
                            b"nginx\x00:\x00worker\x00process\x00")
        assert pm._validate_pid(4242, "vllm") is False

    def test_false_when_cmdline_empty_kernel_thread(self, monkeypatch):
        """空 cmdline（内核线程）→ False。"""
        pm = _make_pm()
        _patch_proc_cmdline(monkeypatch, 7, b"")
        assert pm._validate_pid(7, "vllm") is False

    def test_false_when_pid_missing(self, monkeypatch):
        """PID 不存在（FileNotFoundError）→ False。"""
        pm = _make_pm()
        _patch_proc_cmdline(monkeypatch, 999999, None)
        assert pm._validate_pid(999999, "vllm") is False

    def test_true_on_permission_error_conservative(self, monkeypatch):
        """无法读 cmdline（权限）→ 保守假设仍有效（避免误杀无关进程走 fallback）。"""
        from inferfabric.process_manager.base import BaseProcessManager
        pm = BaseProcessManager.__new__(BaseProcessManager)

        def raise_perm(self):
            raise PermissionError("cannot read cmdline")

        monkeypatch.setattr(Path, "read_bytes", raise_perm)
        assert pm._validate_pid(1234, "comfyui") is True
