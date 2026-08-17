"""Engine adapter registry."""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from inferfabric.engine_adapter.base import EngineAdapter

_adapters: dict[str, type[EngineAdapter]] = {}
_instances: dict[str, EngineAdapter] = {}

def register(name: str, cls: type[EngineAdapter]) -> None:
    _adapters[name] = cls
    _instances.pop(name, None)  # invalidate cache on re-register

def get_adapter(engine_type: str) -> EngineAdapter:
    inst = _instances.get(engine_type)
    if inst is not None:
        return inst
    cls = _adapters.get(engine_type)
    if cls is None:
        raise KeyError(f"Unsupported engine: {engine_type!r}. Registered: {list(_adapters)}")
    inst = cls()
    _instances[engine_type] = inst
    return inst

from inferfabric.engine_adapter.sglang import SGLangAdapter
from inferfabric.engine_adapter.vllm import VLLMAdapter
from inferfabric.engine_adapter.ollama import OllamaAdapter, OllamaDaemonAdapter
from inferfabric.engine_adapter.ollama_cpp import OllamaCppAdapter
from inferfabric.engine_adapter.comfyui import ComfyUIAdapter
from inferfabric.engine_adapter.tts import TTSAdapter
from inferfabric.engine_adapter.asr import ASRAdapter

__all__ = ["EngineAdapter", "get_adapter", "register",
           "SGLangAdapter", "VLLMAdapter", "OllamaCppAdapter",
           "OllamaAdapter", "OllamaDaemonAdapter"]