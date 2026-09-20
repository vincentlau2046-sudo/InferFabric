# tests/unit/engine/test_pm_sglang.py
"""SGLangProcessManager — stop 改优雅 docker stop（D3），start 加 container_name 参。"""
from unittest.mock import MagicMock
from pathlib import Path


def _make_pm(tmp_path):
    from inferfabric.process_manager.sglang import SGLangProcessManager
    from inferfabric.state import StateDB
    state = StateDB(tmp_path / "s.db")
    return SGLangProcessManager(state, tmp_path)


class TestStopSGLangGraceful:
    def test_stop_uses_docker_stop_not_kill(self, monkeypatch, tmp_path):
        """stop_sglang 改优雅：调 docker stop，不调 docker kill，不调 docker rm（D3）。"""
        import subprocess
        pm = _make_pm(tmp_path)
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return MagicMock(returncode=0, stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = pm.stop_sglang(container_name="sglang-foo")
        assert result["status"] == "ok"
        # 只应有一次 docker stop（无 kill、无 rm）
        stop_calls = [c for c in calls if "stop" in c]
        kill_calls = [c for c in calls if "kill" in c]
        rm_calls = [c for c in calls if "rm" in c]
        assert len(stop_calls) == 1, f"应只一次 docker stop: {calls}"
        assert len(kill_calls) == 0, f"不应 docker kill: {calls}"
        assert len(rm_calls) == 0, f"不应 docker rm（--rm 自动移除）: {calls}"
        assert "sglang-foo" in stop_calls[0]


class TestStartSGLangContainerName:
    def test_start_accepts_container_name_param(self, monkeypatch, tmp_path):
        """start_sglang(cfg, container_name) 接受参数，用于状态跟踪（不重推 L49）。"""
        import subprocess
        pm = _make_pm(tmp_path)
        from inferfabric.config import SGLangConfig
        cfg = SGLangConfig(model_dir="/m", served_name="foo", port=8100,
                          mem_fraction=0.9, context_length=4096)

        def fake_popen(cmd, **kw):
            return MagicMock(pid=555, poll=lambda: None, returncode=None)

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        # sglang.py:63 调 os.getpgid(proc.pid) — fake pid 555 不存在会抛
        # ProcessLookupError，必须 patch
        monkeypatch.setattr("inferfabric.process_manager.sglang.os.getpgid", lambda pid: 555)
        monkeypatch.setattr("inferfabric.process_manager.sglang.os.environ", {"PATH": "/x"})
        monkeypatch.setattr("inferfabric.process_manager.sglang.time.sleep", lambda *a: None)
        monkeypatch.setattr("inferfabric.process_manager.sglang.check_http_status", lambda *a, **k: "✅")

        pm.start_sglang(cfg, container_name="sglang-foo")
        # 容器名记录到 state（来自参数，非重推）
        assert pm.sglang_container == "sglang-foo"
        assert pm.sglang_pid == 555
