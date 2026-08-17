"""Engine adapter registry."""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from inferfabric.engine_adapter.base import EngineAdapter

_adapters: dict[str, type[EngineAdapter]] = {}

def register(name: str, cls: type[EngineAdapter]) -> None:
    _adapters[name] = cls

def get_adapter(engine_type: str) -> EngineAdapter:
    cls = _adapters.get(engine_type)
    if cls is None:
        raise KeyError(f"Unsupported engine: {engine_type!r}. Registered: {list(_adapters)}")
    return cls()

from inferfabric.engine_adapter.sglang import SGLangAdapter
from inferfabric.engine_adapter.vllm import VLLMAdapter
from inferfabric.engine_adapter.ollama_cpp import OllamaCppAdapter
from inferfabric.engine_adapter.comfyui import ComfyUIAdapter
from inferfabric.engine_adapter.tts import TTSAdapter
from inferfabric.engine_adapter.asr import ASRAdapter

__all__ = ["EngineAdapter", "get_adapter", "register",
           "SGLangAdapter", "VLLMAdapter", "OllamaCppAdapter"]
