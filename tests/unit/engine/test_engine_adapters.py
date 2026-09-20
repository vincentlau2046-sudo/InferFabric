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


class TestVLLMAdapterStopDispatch:
    """VLLMAdapter.stop 按 resolved_deployment 分派 docker/conda（Task 3.1）。"""

    def _make_model(self, deployment="", **vllm_overrides):
        from inferfabric.config import ModelConfig, VLLMConfig
        defaults = dict(model_dir="/m", served_name="t", conda_env="vllm",
                        port=8000, max_model_len=4096, gpu_memory_utilization=0.9)
        defaults.update(vllm_overrides)
        return ModelConfig(
            name="t", description="d", type="vllm", deployment=deployment,
            gpu_role="exclusive", vllm=VLLMConfig(**defaults),
        )

    def test_vllm_adapter_stop_conda_calls_stop_vllm(self):
        """conda 部署的 vllm：stop 调 ProcessManager.stop_vllm（进程组 kill）。"""
        from inferfabric.engine_adapter.vllm import VLLMAdapter
        adapter = VLLMAdapter()
        pm = MagicMock()
        adapter.set_process_manager(pm)
        model = self._make_model(deployment="")  # 未声明 → 推导 conda
        assert model.resolved_deployment == "conda"
        adapter.stop(model)
        pm.stop_vllm.assert_called_once()
        pm.stop_sglang.assert_not_called()

    def test_vllm_adapter_stop_docker_calls_docker_stop(self, monkeypatch):
        """docker 部署的 vllm：stop 调 docker stop <container>，不走 stop_vllm。"""
        import inferfabric.engine_adapter.vllm as vmod
        from inferfabric.config import ModelConfig
        adapter = vmod.VLLMAdapter()
        pm = MagicMock()
        adapter.set_process_manager(pm)
        # R10: docker 部署不要求 conda_env
        model = self._make_model(deployment="docker", conda_env="")
        assert model.resolved_deployment == "docker"
        # vllm 当前无嵌套 docker config → container_name 为 None；
        # 模拟未来显式 container_name 声明（Task 1.2 扩展点），patch 类属性
        monkeypatch.setattr(ModelConfig, "container_name", property(lambda self: "vllm-foo"))
        docker_calls = []
        def fake_run(*args, **kwargs):
            docker_calls.append(args)
            return MagicMock(returncode=0, stderr=b"")
        monkeypatch.setattr(vmod.subprocess, "run", fake_run)
        result = adapter.stop(model)
        assert result["status"] == "ok", f"expected ok, got {result}"
        assert any("docker" in str(c) and "stop" in str(c) for c in docker_calls), "应调 docker stop"
        pm.stop_vllm.assert_not_called()

    def test_validate_conda_requires_conda_env(self):
        """R10：conda 部署（含默认推导）仍要求 conda_env 非空。"""
        from inferfabric.engine_adapter.vllm import VLLMAdapter
        adapter = VLLMAdapter()
        model = self._make_model(deployment="", conda_env="")
        issues = adapter.validate_config(model)
        assert any("conda_env" in i for i in issues), f"conda 部署应要求 conda_env: {issues}"

    def test_validate_docker_skips_conda_env(self):
        """R10：docker 部署不要求 conda_env。"""
        from inferfabric.engine_adapter.vllm import VLLMAdapter
        adapter = VLLMAdapter()
        model = self._make_model(deployment="docker", conda_env="")
        issues = adapter.validate_config(model)
        assert not any("conda_env" in i for i in issues), f"docker 部署应跳过 conda_env: {issues}"


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

    def test_stop_threads_unified_container_name(self):
        """stop 必须显式传 model.container_name 给 stop_sglang（不依赖底层扫描）。"""
        from inferfabric.config import ModelConfig, SGLangConfig
        from inferfabric.engine_adapter.sglang import SGLangAdapter

        served_name = "qwen3-test"
        model = ModelConfig(
            name="qwen3", description="test", type="sglang",
            sglang=SGLangConfig(model_dir="/models/qwen3", served_name=served_name, port=8100),
        )
        pm = MagicMock()
        adapter = SGLangAdapter(pm)
        adapter.stop(model)
        # container_name 必须等于统一属性值 f"sglang-{served_name}"
        # （与 start_sglang / build_docker_cmd --name 逐字节一致）
        pm.stop_sglang.assert_called_once_with(
            port=8100, container_name=f"sglang-{served_name}"
        )


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
