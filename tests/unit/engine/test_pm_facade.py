"""ProcessManager facade — ninfer 注册 + 透传 + 状态层（Task 3）。"""
from unittest.mock import MagicMock


def _make_facade(tmp_path):
    from inferfabric.process_manager.facade import ProcessManager
    from inferfabric.state import StateDB
    state = StateDB(tmp_path / "s.db")
    return ProcessManager(state, tmp_path)


class TestFacadeNInferDelegation:
    def test_start_ninfer_threads_container_name(self, tmp_path):
        """facade.start_ninfer(cfg, container_name) 透传给 NInferProcessManager。"""
        pm = _make_facade(tmp_path)
        pm._ninfer = MagicMock()
        pm._ninfer.start_ninfer.return_value = {"status": "healthy"}
        cfg = MagicMock()
        result = pm.start_ninfer(cfg, container_name="iff-ninfer-qwen38")
        pm._ninfer.start_ninfer.assert_called_once_with(cfg, "iff-ninfer-qwen38")

    def test_stop_ninfer_threads_container_name(self, tmp_path):
        """facade.stop_ninfer(container_name) 透传。"""
        pm = _make_facade(tmp_path)
        pm._ninfer = MagicMock()
        pm._ninfer.stop_ninfer.return_value = {"status": "ok"}
        result = pm.stop_ninfer("ninfer-8007")
        pm._ninfer.stop_ninfer.assert_called_once_with("ninfer-8007")

    def test_start_sglang_threads_container_name(self, tmp_path):
        """facade.start_sglang(cfg, container_name) 透传（Task 2 签名）。"""
        pm = _make_facade(tmp_path)
        pm._sglang = MagicMock()
        pm._sglang.start_sglang.return_value = {"status": "healthy"}
        cfg = MagicMock()
        pm.start_sglang(cfg, container_name="sglang-foo")
        pm._sglang.start_sglang.assert_called_once_with(cfg, "sglang-foo")

    def test_stop_all_clears_ninfer_state(self, tmp_path):
        """stop_all(active_services=[]) 必须清 ninfer PID + container（与 sglang 对称）。"""
        pm = _make_facade(tmp_path)
        pm._set_ninfer_pid(123)
        pm._set_ninfer_container("ninfer-8007")
        pm.stop_all(active_services=[])
        assert pm.ninfer_pid is None
        assert pm.ninfer_container is None
