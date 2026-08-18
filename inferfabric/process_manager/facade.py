"""
inferfabric/process_manager/facade.py — ProcessManager facade.

Delegates per-engine lifecycle methods to sub-managers.
PID properties read directly from StateDB (shared with sub-managers).
stop_all / force_kill_all orchestrate across all engines.
"""

import os
import re as _re
import time
import signal
import logging
import subprocess
from pathlib import Path
from typing import Optional

from inferfabric.config import DEFAULT_LOG_DIR, COMFYUI_DIR
from inferfabric.process_manager.base import BaseProcessManager
from inferfabric.process_manager.vllm import VLLMProcessManager
from inferfabric.process_manager.sglang import SGLangProcessManager
from inferfabric.process_manager.comfyui import ComfyUIProcessManager
from inferfabric.process_manager.tts import TTSProcessManager
from inferfabric.process_manager.asr import ASRProcessManager
from inferfabric.process_manager.ollama_cpp import OllamaCppProcessManager

log = logging.getLogger("inferfabric")


class ProcessManager(BaseProcessManager):
    """Unified process lifecycle facade for all engine types.

    API-compatible with the original monolithic ProcessManager.
    Delegates per-engine operations to dedicated sub-managers.
    """

    def __init__(self, state, log_dir: Path = DEFAULT_LOG_DIR):
        super().__init__(state, log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._vllm = VLLMProcessManager(state, log_dir)
        self._sglang = SGLangProcessManager(state, log_dir)
        self._comfyui = ComfyUIProcessManager(state, log_dir)
        self._tts = TTSProcessManager(state, log_dir)
        self._asr = ASRProcessManager(state, log_dir)
        self._ollama = OllamaCppProcessManager(state, log_dir)

    # ─── PID Tracking ────────────────────────────────────────────

    @property
    def vllm_pid(self) -> Optional[int]:
        pid_str = self._state.get("vllm_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    @property
    def comfyui_pid(self) -> Optional[int]:
        pid_str = self._state.get("comfyui_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    @property
    def sglang_pid(self) -> Optional[int]:
        pid_str = self._state.get("sglang_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    @property
    def sglang_container(self) -> Optional[str]:
        c = self._state.get("sglang_container")
        return c or None

    @property
    def tts_pid(self) -> Optional[int]:
        pid_str = self._state.get("tts_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    @property
    def asr_pid(self) -> Optional[int]:
        pid_str = self._state.get("asr_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    # ─── PID Setters (test compat, delegate to sub-managers via state) ──

    def _set_vllm_pid(self, pid: Optional[int]):
        self._state.set("vllm_pid", str(pid) if pid else "")

    def _set_comfyui_pid(self, pid: Optional[int]):
        self._state.set("comfyui_pid", str(pid) if pid else "")

    def _set_sglang_pid(self, pid: Optional[int]):
        self._state.set("sglang_pid", str(pid) if pid else "")

    def _set_sglang_container(self, name: Optional[str]):
        self._state.set("sglang_container", name or "")

    def _set_tts_pid(self, pid: Optional[int]):
        self._state.set("tts_pid", str(pid) if pid else "")

    def _set_asr_pid(self, pid: Optional[int]):
        self._state.set("asr_pid", str(pid) if pid else "")

    # ─── vLLM ────────────────────────────────────────────────────

    def start_vllm(self, cfg) -> dict:
        return self._vllm.start_vllm(cfg)

    def stop_vllm(self, port: Optional[int] = None) -> dict:
        return self._vllm.stop_vllm(port)

    def sleep_vllm(self, port: int) -> dict:
        return self._vllm.sleep_vllm(port)

    def wake_vllm(self, port: int) -> dict:
        return self._vllm.wake_vllm(port)

    def is_sleeping(self, port: int) -> bool:
        return self._vllm.is_sleeping(port)

    def is_vllm_alive(self, port: int) -> bool:
        return self._vllm.is_vllm_alive(port)

    # ─── SGLang ─────────────────────────────────────────────────

    def start_sglang(self, cfg) -> dict:
        return self._sglang.start_sglang(cfg)

    def stop_sglang(self, port: Optional[int] = None, container_name: Optional[str] = None) -> dict:
        return self._sglang.stop_sglang(port, container_name)

    def is_sglang_alive(self, port: int) -> bool:
        return self._sglang.is_sglang_alive(port)

    # ─── ComfyUI ─────────────────────────────────────────────────

    def start_comfyui(self, cfg) -> dict:
        return self._comfyui.start_comfyui(cfg)

    def stop_comfyui(self, port: Optional[int] = None) -> dict:
        return self._comfyui.stop_comfyui(port)

    def stop_comfyui_with_config(self, cfg, port: Optional[int] = None) -> dict:
        return self._comfyui.stop_comfyui_with_config(cfg, port)

    def is_comfyui_alive(self, port: int = 8188) -> bool:
        return self._comfyui.is_comfyui_alive(port)

    # ─── Ollama.cpp ──────────────────────────────────────────────

    def start_ollama_cpp(self, cfg, model_type: str = "") -> dict:
        return self._ollama.start_ollama_cpp(cfg, model_type)

    def stop_ollama_cpp(self, port: Optional[int] = None):
        self._ollama.stop_ollama_cpp(port)

    def run_ollama(self, model_ref: str, keep_alive: str = "5m", num_gpu: int = -1) -> dict:
        return self._ollama.run_ollama(model_ref, keep_alive, num_gpu)

    # ─── TTS Server ─────────────────────────────────────────────

    def start_tts_server(self, cfg) -> dict:
        return self._tts.start_tts_server(cfg)

    def stop_tts_server(self, port: Optional[int] = None) -> dict:
        return self._tts.stop_tts_server(port)

    # ─── ASR Server ─────────────────────────────────────────────

    def start_asr_server(self, cfg) -> dict:
        return self._asr.start_asr_server(cfg)

    def stop_asr_server(self, port: Optional[int] = None) -> dict:
        return self._asr.stop_asr_server(port)

    # ─── Combined Operations ─────────────────────────────────────

    def stop_all(
        self,
        comfyui_cfg=None,
        vllm_ports: Optional[list[int]] = None,
        comfyui_port: Optional[int] = None,
        tts_port: Optional[int] = None,
        asr_port: Optional[int] = None,
        sglang_ports: Optional[list[int]] = None,
    ) -> dict:
        """Stop all services: ComfyUI first, then vLLM, then TTS, then ASR,
        then SGLang."""
        results = {}
        if comfyui_cfg:
            port = comfyui_port or comfyui_cfg.port
            results["comfyui"] = self.stop_comfyui_with_config(comfyui_cfg, port=port)
        else:
            results["comfyui"] = self.stop_comfyui()
        if vllm_ports:
            for p in vllm_ports:
                self.stop_vllm(port=p)
            results["vllm"] = {"status": "ok", "ports": vllm_ports}
        else:
            results["vllm"] = self.stop_vllm()
        if tts_port:
            results["tts"] = self.stop_tts_server(port=tts_port)
        else:
            results["tts"] = self.stop_tts_server()
        if asr_port:
            results["asr"] = self.stop_asr_server(port=asr_port)
        else:
            results["asr"] = self.stop_asr_server()
        if sglang_ports:
            for p in sglang_ports:
                self.stop_sglang(port=p)
            results["sglang"] = {"status": "ok", "ports": sglang_ports}
        return results

    def force_kill_all(self) -> dict:
        """Nuclear option: SIGKILL everything related to vLLM + ComfyUI + TTS + ASR + SGLang."""
        # vLLM
        pgid = self.vllm_pid
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        subprocess.run(["pkill", "-9", "-f", "vllm serve"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "VLLM::EngineCore"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "sglang serve"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "sglang.launch_server"], timeout=5, check=False)
        for port in [8000, 8001, 8002]:
            subprocess.run(["pkill", "-9", "-f", f"vllm.*{port}"], timeout=5, check=False)

        # ComfyUI
        cpgid = self.comfyui_pid
        if cpgid:
            try:
                os.killpg(cpgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        subprocess.run(["pkill", "-9", "-f", f"python.*{_re.escape(str(COMFYUI_DIR))}/main.py"], timeout=5, check=False)
        comfyui_dir = _re.escape(str(COMFYUI_DIR))
        subprocess.run(["pkill", "-9", "-f", f"python.*{comfyui_dir}"], timeout=5, check=False)

        # TTS
        tpgid = self.tts_pid
        if tpgid:
            try:
                os.killpg(tpgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        subprocess.run(["pkill", "-9", "-f", "Qwen3-TTS-Openai-Fastapi/api"], timeout=5, check=False)
        subprocess.run(["fuser", "-k", "8880/tcp"], timeout=5, check=False)

        # ASR
        apgid = self.asr_pid
        if apgid:
            try:
                os.killpg(apgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        subprocess.run(["pkill", "-9", "-f", "funasr-server"], timeout=5, check=False)
        subprocess.run(["fuser", "-k", "8881/tcp"], timeout=5, check=False)

        time.sleep(2)
        self._state.set("vllm_pid", "")
        self._state.set("comfyui_pid", "")
        self._state.set("tts_pid", "")
        self._state.set("asr_pid", "")
        self._cleanup_pid_files("vllm")
        self._cleanup_pid_files("comfyui")
        self._cleanup_pid_files("tts")
        self._cleanup_pid_files("asr")
        self._reap_zombies()
        self._wait_gpu_idle()
        return {"status": "ok"}