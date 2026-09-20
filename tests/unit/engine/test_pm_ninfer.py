# tests/unit/engine/test_pm_ninfer.py
"""NInferProcessManager — start/stop 生命周期（Task 1，D1）。

镜像 vllm/sglang PM 的 mock-subprocess 测试模式。start_ninfer 的
container_name 必须来自参数（不重推 cfg.container_name or f"ninfer-{port}"）。
"""
from unittest.mock import MagicMock


def _make_pm(tmp_path):
    from inferfabric.process_manager.ninfer import NInferProcessManager
    from inferfabric.state import StateDB
    state = StateDB(tmp_path / "s.db")
    return NInferProcessManager(state, tmp_path)


def _make_cfg():
    from inferfabric.config import NInferConfig
    return NInferConfig(
        port=8007, container_name="iff-ninfer-qwen38",
        weight_path="/w/model.bin", docker_image="ninfer:latest",
        model_id="m1", max_concurrency=8, max_context=615000,
        kv_capacity=0, default_max_tokens=8192, pending_timeout_ms=300000,
        kv_dtype="nvfp4", prefill_chunk=8192,
    )


class TestStartNInferContainerName:
    def test_start_uses_param_container_name_not_rederive(self, monkeypatch, tmp_path):
        """start_ninfer(cfg, container_name) 必须用参数名，不重推 cfg.container_name。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        # 参数给一个与 cfg.container_name 不同的名，验证用的是参数
        passed_names = []

        def fake_popen(cmd, **kw):
            # 捕获 --name 后的值
            if "--name" in cmd:
                idx = cmd.index("--name")
                passed_names.append(cmd[idx + 1])
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None  # 不立即退出
            return proc

        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.Popen", fake_popen)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run", lambda *a, **k: MagicMock(returncode=0))
        monkeypatch.setattr("inferfabric.process_manager.ninfer.wait_http", lambda url, timeout=120: True)

        pm.start_ninfer(cfg, container_name="custom-name-from-param")
        assert passed_names == ["custom-name-from-param"], \
            f"应用参数名，实际 docker run --name: {passed_names}"

    def test_start_tracks_pid_and_container(self, monkeypatch, tmp_path):
        """start_ninfer 成功后记录 PID + container 到 state。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.Popen",
                            lambda cmd, **kw: MagicMock(pid=999, poll=lambda: None))
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run",
                            lambda *a, **k: MagicMock(returncode=0))
        monkeypatch.setattr("inferfabric.process_manager.ninfer.wait_http", lambda url, timeout=120: True)
        pm.start_ninfer(cfg, container_name="ninfer-8007")
        assert pm.ninfer_pid == 999
        assert pm.ninfer_container == "ninfer-8007"


class TestStopNInferGraceful:
    def test_stop_uses_docker_stop_not_kill(self, monkeypatch, tmp_path):
        """stop_ninfer 必须用 docker stop（优雅），不调 docker kill（D3）。"""
        pm = _make_pm(tmp_path)
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return MagicMock(returncode=0, stderr=b"")

        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run", fake_run)
        result = pm.stop_ninfer("ninfer-8007")
        assert result["status"] == "ok"
        assert len(calls) == 1
        cmd = calls[0]
        assert "docker" in cmd and "stop" in cmd, f"应调 docker stop: {cmd}"
        assert "kill" not in cmd, f"不应调 docker kill: {cmd}"
        assert "ninfer-8007" in cmd

    def test_stop_none_container_name_returns_warning(self, tmp_path):
        """None container_name：不触子进程，直接 warning（守卫）。"""
        pm = _make_pm(tmp_path)
        result = pm.stop_ninfer("")
        assert result["status"] == "warning"
        assert "container_name" in result["message"]

    def test_stop_nonzero_rc_returns_warning(self, monkeypatch, tmp_path):
        """非零 rc：warning + stderr 片段。"""
        pm = _make_pm(tmp_path)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run",
                            lambda *a, **k: MagicMock(returncode=1, stderr=b"container not found"))
        result = pm.stop_ninfer("ninfer-8007")
        assert result["status"] == "warning"
        assert "exit 1" in result["message"]

    def test_stop_clears_pid_and_container_state(self, monkeypatch, tmp_path):
        """stop 成功后清 PID + container state。"""
        pm = _make_pm(tmp_path)
        pm._set_ninfer_pid(123)
        pm._set_ninfer_container("ninfer-8007")
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run",
                            lambda *a, **k: MagicMock(returncode=0, stderr=b""))
        pm.stop_ninfer("ninfer-8007")
        assert pm.ninfer_pid is None
        assert pm.ninfer_container is None
