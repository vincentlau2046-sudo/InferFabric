"""
unit/engine/test_engine_adapters.py — 引擎适配器注册与路由测试

测试对象: inferfabric.engine_adapter._adapters, get_adapter, EngineAdapter 子类
覆盖范围:
  - 注册表完整性（7 种引擎类型）
  - get_adapter 路由、单例、未知类型异常
  - 各适配器 engine_type 属性
  - VLLMAdapter: validate_config、sleep 无 proc、get_port、get_pid_state_key
  - SGLang/Ollama: sleep 默认不支持
"""

import pytest
from unittest.mock import MagicMock


class TestAdapterRegistry:
    """适配器注册表"""

    def test_all_adapters_registered(self):
        """所有内置适配器已注册。"""
        from inferfabric.engine_adapter import _adapters

        expected = {"vllm", "ollama", "sglang", "comfyui", "tts_server",
                    "asr_server", "ollama_cpp"}
        for name in expected:
            assert name in _adapters, f"Missing adapter: {name}"

    def test_get_adapter_known_types(self):
        """get_adapter 对已知类型返回适配器实例。"""
        from inferfabric.engine_adapter import get_adapter

        for engine_type in ["vllm", "ollama", "sglang", "comfyui",
                            "tts_server", "asr_server", "ollama_cpp"]:
            adapter = get_adapter(engine_type)
            assert adapter is not None, f"get_adapter('{engine_type}') returned None"

    def test_get_adapter_unknown_raises(self):
        """get_adapter 对未知类型抛 KeyError/ValueError。"""
        from inferfabric.engine_adapter import get_adapter

        with pytest.raises((KeyError, ValueError)):
            get_adapter("nonexistent_engine")

    def test_get_adapter_singleton(self):
        """多次 get_adapter 返回同一实例。"""
        from inferfabric.engine_adapter import get_adapter

        a1 = get_adapter("vllm")
        a2 = get_adapter("vllm")
        assert a1 is a2


class TestVLLMAdapter:
    """VLLM 适配器"""

    def test_engine_type(self):
        from inferfabric.engine_adapter import get_adapter
        adapter = get_adapter("vllm")
        assert adapter.engine_type == "vllm"

    def test_validate_valid_config(self):
        """有效配置通过验证（返回空 issues 列表）。"""
        from inferfabric.engine_adapter import get_adapter
        from inferfabric.config import ModelConfig, VLLMConfig

        adapter = get_adapter("vllm")
        cfg = ModelConfig(
            name="test",
            description="test model",
            type="vllm",
            gpu_role="exclusive",
            vllm=VLLMConfig(
                model_dir="/models/test",
                served_name="test",
                conda_env="vllm",
                port=8000,
                gpu_memory_utilization=0.9,
                max_model_len=4096,
            ),
        )
        issues = adapter.validate_config(cfg)
        assert issues == [], f"Unexpected issues: {issues}"

    def test_validate_missing_vllm_block(self):
        """缺少 vllm 配置块时返回 issue。"""
        from inferfabric.engine_adapter import get_adapter
        from inferfabric.config import ModelConfig

        adapter = get_adapter("vllm")
        cfg = ModelConfig(name="test", description="test", type="vllm", gpu_role="exclusive")
        issues = adapter.validate_config(cfg)
        assert len(issues) > 0

    def test_validate_bad_gpu_mem(self):
        """gpu_memory_utilization 超出范围时返回 issue。"""
        from inferfabric.engine_adapter import get_adapter
        from inferfabric.config import ModelConfig, VLLMConfig

        adapter = get_adapter("vllm")
        cfg = ModelConfig(
            name="test", description="test", type="vllm", gpu_role="exclusive",
            vllm=VLLMConfig(
                model_dir="/models/test", served_name="test",
                conda_env="vllm", port=8000, max_model_len=4096,
                gpu_memory_utilization=1.5,  # 超出范围
            ),
        )
        issues = adapter.validate_config(cfg)
        assert len(issues) > 0

    def test_sleep_without_process_manager(self):
        """无 process_manager 时 sleep 返回 error。"""
        from inferfabric.engine_adapter import get_adapter
        from inferfabric.config import ModelConfig, VLLMConfig

        adapter = get_adapter("vllm")
        cfg = ModelConfig(
            name="test", description="test", type="vllm", gpu_role="exclusive",
            vllm=VLLMConfig(model_dir="/m", served_name="t", conda_env="v",
                            port=8000, max_model_len=4096,
                            gpu_memory_utilization=0.9),
        )
        # 临时清除 proc
        orig_proc = adapter._proc
        adapter._proc = None
        try:
            result = adapter.sleep(cfg)
            assert result["status"] == "error"
        finally:
            adapter._proc = orig_proc

    def test_get_port_returns_vllm_port(self):
        """get_port 返回 vllm 配置中的端口号。"""
        from inferfabric.engine_adapter import get_adapter
        from inferfabric.config import ModelConfig, VLLMConfig

        adapter = get_adapter("vllm")
        cfg = ModelConfig(
            name="test", description="test", type="vllm", gpu_role="exclusive",
            vllm=VLLMConfig(model_dir="/m", served_name="t", conda_env="v",
                            port=8000, max_model_len=4096,
                            gpu_memory_utilization=0.9),
        )
        assert adapter.get_port(cfg) == 8000

    def test_get_pid_state_key(self):
        """get_pid_state_key 返回 'vllm_pid'。"""
        from inferfabric.engine_adapter import get_adapter
        adapter = get_adapter("vllm")
        assert adapter.get_pid_state_key() == "vllm_pid"


class TestOllamaCppAdapter:
    """Ollama-CPP 适配器"""

    def test_engine_type(self):
        from inferfabric.engine_adapter import get_adapter
        adapter = get_adapter("ollama_cpp")
        assert adapter.engine_type == "ollama_cpp"

    def test_get_pid_state_key(self):
        from inferfabric.engine_adapter import get_adapter
        adapter = get_adapter("ollama_cpp")
        # ollama_cpp 有自己的 PID state key 或 None
        key = adapter.get_pid_state_key()
        assert key is None or isinstance(key, str)


class TestTTSAdapter:
    """TTS 适配器"""

    def test_engine_type(self):
        from inferfabric.engine_adapter import get_adapter
        adapter = get_adapter("tts_server")
        assert adapter.engine_type == "tts_server"


class TestASRAdapter:
    """ASR 适配器"""

    def test_engine_type(self):
        from inferfabric.engine_adapter import get_adapter
        adapter = get_adapter("asr_server")
        assert adapter.engine_type == "asr_server"


class TestComfyUIAdapter:
    """ComfyUI 适配器"""

    def test_engine_type(self):
        from inferfabric.engine_adapter import get_adapter
        adapter = get_adapter("comfyui")
        assert adapter.engine_type == "comfyui"


class TestSGLangAdapter:
    """SGLang 适配器"""

    def test_engine_type(self):
        from inferfabric.engine_adapter import get_adapter
        adapter = get_adapter("sglang")
        assert adapter.engine_type == "sglang"

    def test_sleep_returns_error_by_default(self):
        """SGLang sleep 默认返回 error（不支持）。"""
        from inferfabric.engine_adapter import get_adapter
        from inferfabric.config import ModelConfig

        adapter = get_adapter("sglang")
        cfg = ModelConfig(name="test", description="test", type="sglang", gpu_role="exclusive")
        result = adapter.sleep(cfg)
        assert "error" in result.get("status", "") or "not supported" in result.get("message", "").lower()


class TestOllamaAdapter:
    """Ollama 适配器"""

    def test_engine_type(self):
        from inferfabric.engine_adapter import get_adapter
        adapter = get_adapter("ollama")
        assert adapter.engine_type == "ollama"

    def test_sleep_returns_error_by_default(self):
        """Ollama sleep 默认返回 error（不支持）。"""
        from inferfabric.engine_adapter import get_adapter
        from inferfabric.config import ModelConfig

        adapter = get_adapter("ollama")
        cfg = ModelConfig(name="test", description="test", type="ollama", gpu_role="exclusive")
        result = adapter.sleep(cfg)
        assert "error" in result.get("status", "") or "not supported" in result.get("message", "").lower()
