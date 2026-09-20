"""
unit/state/test_model_lifecycle.py — ModelLifecycle 单元测试

测试对象:
  - ModelLifecycle._start_model (dispatch)
  - ModelLifecycle.sleep_model (验证 + 状态转移)
  - ModelLifecycle.wake_model (验证 + 状态转移)

覆盖范围:
  - 未知 model type → error
  - sleep: 未运行 → error, 非 vLLM → error, 已睡觉的已存在 → error
  - wake: 未睡觉 → already_awake, GPU 模式冲突 → error
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
from inferfabric.model_lifecycle import ModelLifecycle
from inferfabric.state import GPUMode, ServiceState


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _make_lifecycle(active_services=None):
    """Create a ModelLifecycle with fully mocked dependencies."""
    state = MagicMock()
    state.get_active_services.return_value = active_services or ["test-vllm"]
    state.get_sleep_state.return_value = None
    state.get_all_sleep_states.return_value = {}
    state.gpu_mode = GPUMode.IDLE

    proc = MagicMock()
    health = MagicMock()
    lock = MagicMock()
    gpu_state = MagicMock()
    gpu_state._get_current_vram_pct.return_value = 0

    # Mock model config
    model = MagicMock()
    model.name = "test-vllm"
    model.type = "vllm"
    model.is_vllm = True
    model.is_exclusive = True
    model.is_gpu_none = False
    model.is_sglang = False
    model.is_comfyui = False
    model.is_ninfer = False
    model.is_ollama_cpp = False
    model.is_tts_server = False
    model.is_asr_server = False
    model.gpu_role = "exclusive"
    model.peak_vram_mb = 0
    model.typical_vram_pct = 0
    model.vllm.port = 8100
    model.vllm.sleep_mode.enabled = True

    models = {"test-vllm": model}

    return ModelLifecycle(state, proc, health, lock, gpu_state, models)


def _make_shared_lifecycle():
    """Lifecycle with a shared vLLM model."""
    state = MagicMock()
    state.get_active_services.return_value = ["test-shared"]
    state.get_sleep_state.return_value = None
    state.get_all_sleep_states.return_value = {}
    state.gpu_mode = GPUMode.SHARED

    proc = MagicMock()
    health = MagicMock()
    lock = MagicMock()
    gpu_state = MagicMock()
    gpu_state._get_current_vram_pct.return_value = 0

    model = MagicMock()
    model.name = "test-shared"
    model.type = "vllm"
    model.is_vllm = True
    model.is_exclusive = False
    model.is_gpu_none = False
    model.is_sglang = False
    model.is_comfyui = False
    model.is_ninfer = False
    model.is_ollama_cpp = False
    model.is_tts_server = False
    model.is_asr_server = False
    model.gpu_role = "shared"
    model.peak_vram_mb = 0
    model.typical_vram_pct = 0
    model.vllm.port = 8101
    model.vllm.sleep_mode.enabled = True

    models = {"test-shared": model}

    return ModelLifecycle(state, proc, health, lock, gpu_state, models)


def _make_lifecycle_with_active_exclusive(name):
    """Lifecycle whose only active service is an exclusive GPU-bound model."""
    state = MagicMock()
    state.get_active_services.return_value = [name]
    state.get_sleep_state.return_value = None
    state.get_all_sleep_states.return_value = {}
    state.gpu_mode = GPUMode.EXCLUSIVE

    proc = MagicMock()
    health = MagicMock()
    lock = MagicMock()
    gpu_state = MagicMock()
    gpu_state._get_current_vram_pct.return_value = 0

    model = MagicMock()
    model.name = name
    model.type = "vllm"
    model.is_vllm = True
    model.is_exclusive = True
    model.is_gpu_none = False
    model.is_sglang = False
    model.is_comfyui = False
    model.is_ninfer = False
    model.is_ollama_cpp = False
    model.is_tts_server = False
    model.is_asr_server = False
    model.needs_gpu = True
    model.gpu_role = "exclusive"
    model.peak_vram_mb = 0
    model.typical_vram_pct = 0
    model.vllm.port = 8100

    models = {name: model}

    return ModelLifecycle(state, proc, health, lock, gpu_state, models)


def _make_lifecycle_with_active_shared(name):
    """Lifecycle whose only active service is a shared GPU-bound model."""
    state = MagicMock()
    state.get_active_services.return_value = [name]
    state.get_sleep_state.return_value = None
    state.get_all_sleep_states.return_value = {}
    state.gpu_mode = GPUMode.SHARED

    proc = MagicMock()
    health = MagicMock()
    lock = MagicMock()
    gpu_state = MagicMock()
    gpu_state._get_current_vram_pct.return_value = 0

    model = MagicMock()
    model.name = name
    model.type = "vllm"
    model.is_vllm = True
    model.is_exclusive = False
    model.is_gpu_none = False
    model.is_sglang = False
    model.is_comfyui = False
    model.is_ninfer = False
    model.is_ollama_cpp = False
    model.is_tts_server = False
    model.is_asr_server = False
    model.needs_gpu = False
    model.gpu_role = "shared"
    model.peak_vram_mb = 0
    model.typical_vram_pct = 0
    model.vllm.port = 8101

    models = {name: model}

    return ModelLifecycle(state, proc, health, lock, gpu_state, models)


# ═══════════════════════════════════════════════════════════════
# 1. _start_model dispatch
# ═══════════════════════════════════════════════════════════════


def test_start_model_unknown_type():
    """未知 model type 返回 error。"""
    mlc = _make_lifecycle()
    model = MagicMock()
    model.type = "unknown-engine"
    model.name = "unknown"

    result = mlc._start_model(model)
    assert result["status"] == "error"
    assert "Unknown model type" in result["message"]


@patch("inferfabric.engine_adapter.get_adapter")
def test_start_model_dispatches_to_adapter(mock_get_adapter):
    """已知 type 调用对应 adapter.start。"""
    mlc = _make_lifecycle()
    mock_adapter = MagicMock()
    mock_adapter.start.return_value = {"status": "healthy"}
    mock_get_adapter.return_value = mock_adapter

    model = mlc._models["test-vllm"]
    result = mlc._start_model(model)

    mock_get_adapter.assert_called_once_with("vllm")
    mock_adapter.set_process_manager.assert_called_once()
    mock_adapter.start.assert_called_once_with(model)
    assert result["status"] == "healthy"


@patch("inferfabric.engine_adapter.get_adapter")
def test_start_model_propagates_adapter_error(mock_get_adapter):
    """adapter.start 错误透传。"""
    mlc = _make_lifecycle()
    mock_adapter = MagicMock()
    mock_adapter.start.return_value = {"status": "timeout", "message": "GPU OOM"}
    mock_get_adapter.return_value = mock_adapter

    model = mlc._models["test-vllm"]
    result = mlc._start_model(model)
    assert result["status"] == "timeout"
    assert result["message"] == "GPU OOM"


# ═══════════════════════════════════════════════════════════════
# 2. Sleep State Machine
# ═══════════════════════════════════════════════════════════════


def test_sleep_model_not_running():
    """未运行的 model 不能睡觉。"""
    mlc = _make_lifecycle(active_services=["other"])

    result = mlc.sleep_model("test-vllm")
    assert result["status"] == "error"
    assert "not running" in result["message"]


def test_sleep_model_not_vllm():
    """非 vLLM model 不能睡觉。"""
    mlc = _make_lifecycle()
    model = mlc._models["test-vllm"]
    model.is_vllm = False
    model.type = "comfyui"

    result = mlc.sleep_model("test-vllm")
    assert result["status"] == "error"
    assert "not a vLLM model" in result["message"]


def test_sleep_model_sleep_disabled():
    """sleep_mode 未启用时不能睡觉。"""
    mlc = _make_lifecycle()
    model = mlc._models["test-vllm"]
    model.vllm.sleep_mode.enabled = False

    result = mlc.sleep_model("test-vllm")
    assert result["status"] == "error"
    assert "not enabled" in result["message"]


def test_sleep_model_already_sleeping():
    """已睡觉的 model 不能再睡觉。"""
    mlc = _make_lifecycle()
    mlc.state.get_sleep_state.return_value = 2  # L2 sleep

    result = mlc.sleep_model("test-vllm")
    assert result["status"] == "already_sleeping"


def test_sleep_model_mutex_blocks_two_sleeps():
    """已有一个在睡觉时，另一个不能睡觉。"""
    mlc = _make_lifecycle()
    mlc.state.get_all_sleep_states.return_value = {"other-model": 2}

    result = mlc.sleep_model("test-vllm")
    assert result["status"] == "error"
    assert "already sleeping" in result["message"]


def test_sleep_unknown_model():
    """未知 model sleep 返回 error。"""
    mlc = _make_lifecycle(active_services=["unknown-model"])

    result = mlc.sleep_model("unknown-model")
    assert result["status"] == "error"
    assert "Unknown model" in result["message"]


@patch("inferfabric.engine_adapter.get_adapter")
def test_sleep_exclusive_sets_gpu_idle(mock_get_adapter):
    """独占 model 睡觉 → GPU 变 idle。"""
    mlc = _make_lifecycle()
    mock_adapter = MagicMock()
    mock_adapter.sleep.return_value = {"status": "ok"}
    mock_get_adapter.return_value = mock_adapter

    result = mlc.sleep_model("test-vllm")

    assert result["status"] == "ok"
    mlc.state.set_sleep_state.assert_called_with("test-vllm", 2)
    mlc.state.set_multi.assert_called_once()
    multi_args = mlc.state.set_multi.call_args[0][0]
    assert multi_args["gpu_mode"] == GPUMode.IDLE
    assert multi_args["profile_state"] == ServiceState.IDLE


@patch("inferfabric.engine_adapter.get_adapter")
def test_sleep_failure_clears_sleep_state(mock_get_adapter):
    """adapter.sleep 失败时清除 sleep state。"""
    mlc = _make_lifecycle()
    mock_adapter = MagicMock()
    mock_adapter.sleep.return_value = {"status": "error", "message": "GPU not ready"}
    mock_get_adapter.return_value = mock_adapter

    result = mlc.sleep_model("test-vllm")

    assert result["status"] == "error"
    mlc.state.set_sleep_state.assert_any_call("test-vllm", None)


# ═══════════════════════════════════════════════════════════════
# 3. Wake State Machine
# ═══════════════════════════════════════════════════════════════


def test_wake_model_not_sleeping():
    """未睡觉的 model wake → already_awake。"""
    mlc = _make_lifecycle()
    mlc.state.get_sleep_state.return_value = None

    result = mlc.wake_model("test-vllm")
    assert result["status"] == "already_awake"


def test_wake_model_not_vllm():
    """非 vLLM model wake → error。"""
    mlc = _make_lifecycle()
    model = mlc._models["test-vllm"]
    model.is_vllm = False

    result = mlc.wake_model("test-vllm")
    assert result["status"] == "error"
    assert "not a vLLM model" in result["message"]


def test_wake_unknown_model():
    """未知 model wake → error。"""
    mlc = _make_lifecycle()

    result = mlc.wake_model("nonexistent")
    assert result["status"] == "error"
    assert "Unknown model" in result["message"]


def test_wake_exclusive_requires_idle_gpu():
    """独占 model 唤醒时需要 GPU idle（EXCLUSIVE → EXCLUSIVE 被 validate_transition 拒绝）。"""
    mlc = _make_lifecycle()
    mlc.state.get_sleep_state.return_value = 2
    mlc.state.gpu_mode = GPUMode.EXCLUSIVE  # GPU occupied by another model

    result = mlc.wake_model("test-vllm")
    # validate_transition(EXCLUSIVE → EXCLUSIVE) should fail
    assert result["status"] == "error"


@patch("inferfabric.engine_adapter.get_adapter")
def test_wake_shared_from_shared_gpu(mock_get_adapter):
    """共享 model 从 shared GPU 唤醒：pre-flight checks 通过，调用 adapter.wake。"""
    mlc = _make_shared_lifecycle()
    mlc.state.get_sleep_state.return_value = 2
    mlc.state.gpu_mode = GPUMode.SHARED
    # Pre-load the lock so wake flows through without blocking
    mlc._lock.acquire.return_value = True

    mock_adapter = MagicMock()
    mock_adapter.wake.return_value = {"status": "ok"}
    mock_get_adapter.return_value = mock_adapter

    # For shared models, wake_model calls _shared_add_service after wake,
    # which tries to start the model. The return value depends on that flow.
    # The key assertion: validate_transition(SHARED → SHARED) passed.
    result = mlc.wake_model("test-shared")
    # validate_transition passed (no "cannot transition" error)
    assert result["status"] != "error"
    # adapter.wake was called
    mock_adapter.wake.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# 4. stop_service exclusive → _switch_to_idle delegation (Task 0.3)
# ═══════════════════════════════════════════════════════════════


def test_stop_service_exclusive_delegates_to_switch_idle(monkeypatch):
    """B 方案：exclusive 模型 stop_service 应转走 _switch_to_idle，返回 status=stopped。"""
    from inferfabric.model_lifecycle import ModelLifecycle
    from inferfabric.state import GPUMode
    lc = _make_lifecycle_with_active_exclusive("Qwen38-27B-TXT")  # 既有 helper 或新建
    called = {}
    def fake_switch_to_idle():
        called["switch_to_idle"] = True
        return {"status": "switched", "model": "idle", "stopped": ["Qwen38-27B-TXT"], "elapsed_sec": 1.2}
    monkeypatch.setattr(lc, "_switch_to_idle", fake_switch_to_idle)

    result = lc.stop_service("Qwen38-27B-TXT")
    assert called.get("switch_to_idle") is True, "exclusive stop 应转走 _switch_to_idle"
    assert result["status"] == "stopped", "返回值归一为 stopped（CLI/handler 依赖）"
    assert result.get("gpu_mode") == "idle"
    assert "Qwen38-27B-TXT" in result.get("stopped", [])


def test_stop_service_shared_still_calls_stop_model_process(monkeypatch):
    """B 方案不破坏 shared：shared 模型仍走 _stop_model_process，不转 idle。"""
    lc = _make_lifecycle_with_active_shared("ovis-ocr2")
    called = {"switch_to_idle": False, "stop_model_process": False}
    monkeypatch.setattr(lc, "_switch_to_idle", lambda: called.__setitem__("switch_to_idle", True))
    monkeypatch.setattr(lc, "_stop_model_process", lambda m, n: called.__setitem__("stop_model_process", True))
    lc.stop_service("ovis-ocr2")
    assert called["stop_model_process"] and not called["switch_to_idle"]


# ═══════════════════════════════════════════════════════════════
# 5. _stop_all_active unified stop helper (Task 2.4)
# ═══════════════════════════════════════════════════════════════


def _make_lifecycle_mixed():
    """Lifecycle with 2 GPU-bound services + 1 gpu_role=none service.

    - "qwen2" (vllm, shared): needs_gpu=True
    - "nomic-embed" (vllm, shared): needs_gpu=True
    - "bge-m3" (ollama_cpp): needs_gpu=False, is_gpu_none=True
    """
    state = MagicMock()
    state.get_active_services.return_value = ["qwen2", "nomic-embed", "bge-m3"]
    state.gpu_mode = GPUMode.SHARED

    def _mk(name, mtype, needs_gpu, gpu_none):
        m = MagicMock()
        m.name = name
        m.type = mtype
        m.needs_gpu = needs_gpu
        m.is_gpu_none = gpu_none
        m.gpu_role = "none" if gpu_none else "shared"
        return m

    models = {
        "qwen2": _mk("qwen2", "vllm", needs_gpu=True, gpu_none=False),
        "nomic-embed": _mk("nomic-embed", "vllm", needs_gpu=True, gpu_none=False),
        "bge-m3": _mk("bge-m3", "ollama_cpp", needs_gpu=False, gpu_none=True),
    }

    return ModelLifecycle(state, MagicMock(), MagicMock(), MagicMock(), MagicMock(), models)


class TestStopAllActive:
    """Task 2.4: _stop_all_active — 统一「停所有 active 服务」入口（经 _stop_model_process）。"""

    def test_stops_gpu_bound_services_by_default(self, monkeypatch):
        """默认（include_none=False）只停 needs_gpu 服务。"""
        lc = _make_lifecycle_mixed()
        stopped = []
        monkeypatch.setattr(lc, "_stop_model_process", lambda m, n: stopped.append(n))

        lc._stop_all_active()

        assert sorted(stopped) == ["nomic-embed", "qwen2"], \
            f"默认应只停 GPU-bound 服务，got {stopped}"

    def test_include_none_true_stops_none_services(self, monkeypatch):
        """include_none=True 也停 gpu_role=none 服务（force_reset 用）。"""
        lc = _make_lifecycle_mixed()
        stopped = []
        monkeypatch.setattr(lc, "_stop_model_process", lambda m, n: stopped.append(n))

        lc._stop_all_active(include_none=True)

        assert sorted(stopped) == ["bge-m3", "nomic-embed", "qwen2"], \
            f"include_none=True 应停全部 active 服务，got {stopped}"

    def test_explicit_services_list_overrides_state(self, monkeypatch):
        """显式 services 列表优先（不读 state 的 active_services）。"""
        lc = _make_lifecycle_mixed()
        stopped = []
        monkeypatch.setattr(lc, "_stop_model_process", lambda m, n: stopped.append(n))

        lc._stop_all_active(services=["bge-m3"], include_none=True)

        assert stopped == ["bge-m3"], \
            f"应只停显式列表中的服务（state 里还有 qwen2/nomic-embed 不应被停），got {stopped}"

    def test_unknown_service_defensively_skipped(self, monkeypatch):
        """active_services 里不在 _models 的服务被防御性跳过。"""
        lc = _make_lifecycle_mixed()
        stopped = []
        monkeypatch.setattr(lc, "_stop_model_process", lambda m, n: stopped.append(n))

        lc._stop_all_active(services=["ghost-service"], include_none=True)

        assert stopped == [], f"未知服务应被跳过，got {stopped}"