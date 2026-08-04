"""
inferfabric/process_manager.py — Unified process lifecycle for vLLM + ComfyUI.

Extracted from profile_manager.py (v3.0 → v3.1 refactoring).

Key improvements over v3.0:
  - ComfyUI now uses native Python process management (no bash script dependency)
  - Process group tracking for both vLLM and ComfyUI
  - ComfyUI PID tracked in state.db
  - Unified stop pattern: SIGTERM → wait → SIGKILL process group
"""

import os
import re as _re
import time
import signal
import shlex
import json
import logging
import urllib.request
import urllib.error
import subprocess
from pathlib import Path
from typing import Optional

from .config import (
    CONDA_ENVS,
    DEFAULT_LOG_DIR,
    COMFYUI_DIR,
    VLLM_STARTUP_CHECK_INTERVAL,
    VLLM_STARTUP_CHECK_ROUNDS,
    HEALTH_CHECK_TIMEOUT,
    STOP_SIGTERM_TIMEOUT,
    VLLMConfig,
    ComfyUIConfig,
    TTSConfig,
    ConfigError,
    SleepModeConfig,
)
from .state import StateDB
from .health import wait_http, check_http_status, wait_gpu_free, gpu_used_mb

log = logging.getLogger("inferfabric")


class ProcessManager:
    """Manages vLLM and ComfyUI processes using process groups (not pkill)."""

    def __init__(self, state: StateDB, log_dir: Path = DEFAULT_LOG_DIR):
        self._state = state
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)

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

    def _set_vllm_pid(self, pid: Optional[int]):
        self._state.set("vllm_pid", str(pid) if pid else "")

    def _set_comfyui_pid(self, pid: Optional[int]):
        self._state.set("comfyui_pid", str(pid) if pid else "")

    @property
    def tts_pid(self) -> Optional[int]:
        pid_str = self._state.get("tts_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    def _set_tts_pid(self, pid: Optional[int]):
        self._state.set("tts_pid", str(pid) if pid else "")

    # ─── vLLM ────────────────────────────────────────────────────

    def start_vllm(self, cfg: VLLMConfig) -> dict:
        """Start vLLM via conda env's vllm binary. Uses start_new_session for process group isolation.

        Model-specific environment variables are injected from cfg.extra_env (YAML-defined).
        """
        log_file = self._log_dir / f"vllm_{cfg.conda_env}.log"
        pid_file = self._log_dir / f"vllm_{cfg.conda_env}.pid"

        vllm_bin = CONDA_ENVS / cfg.conda_env / "bin" / "vllm"
        if not vllm_bin.exists():
            log.error("vllm binary not found: %s", vllm_bin)
            return {"status": "error", "message": f"vllm not found in conda env {cfg.conda_env}"}

        cmd = cfg.build_cmd()
        cmd[0] = str(vllm_bin)

        log.info("Starting vLLM cmd: %s", " ".join(cmd[:8]) + "...")
        env = dict(os.environ)
        # KV offloading conflicts with expandable_segments (NIXL/Mooncake IB memory)
        has_kv_offload = "--kv-offloading-size" in " ".join(cmd)
        if not has_kv_offload:
            env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
        else:
            log.info("KV offloading detected — skipping expandable_segments for %s", cfg.served_name)

        # Enable sleep mode if configured
        if cfg.sleep_mode and cfg.sleep_mode.enabled:
            env["VLLM_SERVER_DEV_MODE"] = "1"
            cmd.append("--enable-sleep-mode")
            log.info("Sleep mode enabled (L2) for %s", cfg.served_name)

        # Inject extra_env from model YAML config (highest priority)
        if cfg.extra_env:
            if cfg.sleep_mode and cfg.sleep_mode.enabled and "VLLM_SERVER_DEV_MODE" in cfg.extra_env:
                raise ConfigError(
                    f"extra_env overrides VLLM_SERVER_DEV_MODE for {cfg.served_name} — "
                    f"sleep mode will not work. Remove VLLM_SERVER_DEV_MODE from extra_env or disable sleep_mode."
                )
            for k, v in cfg.extra_env.items():
                env[k] = v
                log.debug("extra_env: %s=%s", k, v)

        conda_bin = str(CONDA_ENVS / cfg.conda_env / "bin")
        env["PATH"] = conda_bin + ":" + env.get("PATH", "")

        log_file.write_text("")

        log_fh = open(str(log_file), "a")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except Exception as e:
            log.error("Failed to start vLLM: %s", e)
            return {"status": "error", "message": f"Popen failed: {e}"}
        finally:
            log_fh.close()

        pgid = proc.pid  # With start_new_session, PID == PGID
        self._set_vllm_pid(pgid)
        pid_file.write_text(str(pgid))
        log.info("vLLM started: PID=%d (PGID=%d)", proc.pid, pgid)

        # Check if process died immediately
        for _ in range(VLLM_STARTUP_CHECK_ROUNDS):
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = "read log failed"
                log.error("vLLM exited immediately (ret=%d): %s", ret, err[-500:])
                self._set_vllm_pid(None)
                pid_file.unlink(missing_ok=True)
                return {"status": "error", "message": f"vLLM exited with code {ret}", "log": str(log_file)}

            time.sleep(VLLM_STARTUP_CHECK_INTERVAL)

        # Wait for vLLM to become healthy (use model-specific timeout if configured)
        health_timeout = cfg.startup_timeout if cfg.startup_timeout > 0 else HEALTH_CHECK_TIMEOUT
        healthy = wait_http(f"http://localhost:{cfg.port}/health", timeout=health_timeout)
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        else:
            if proc.poll() is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = ""
                return {"status": "error", "message": "vLLM crashed during loading", "log": str(log_file)}
            else:
                self.stop_vllm()
                return {"status": "timeout", "message": f"vLLM didn't become healthy within {health_timeout}s"}

    def stop_vllm(self, port: Optional[int] = None) -> dict:
        """Stop vLLM using process group kill. SIGTERM → wait → SIGKILL entire group.

        When ``port`` is supplied, also does port-based cleanup after the tracked
        PID path completes (or immediately if the tracked PID is dead).  This
        catches orphaned processes that were not spawned by iff.
        """
        pgid = self.vllm_pid
        if pgid is None and port is None:
            log.warning("No vLLM PID tracked and no port given — falling back to pkill")
            return self._pkill_vllm_fallback()

        # P1-2: 校验 PID 是否已被操作系统复用到无关进程
        if pgid is not None and not self._validate_pid(pgid, "vllm"):
            log.warning(
                "Tracked vLLM PID %d does not appear to be a vLLM process "
                "(cmdline mismatch) — clearing stale PID and using fallback",
                pgid,
            )
            self._set_vllm_pid(None)
            if port:
                self._pkill_by_port(port)
                self._cleanup_pid_files("vllm")
                self._wait_gpu_idle()
                return {"status": "ok", "message": "port-based cleanup after stale PID"}
            return self._pkill_vllm_fallback()

        if pgid is None:
            # Tracked PID gone but port given — skip PG path, go straight to port cleanup
            log.info("Tracked PID gone but port=%d given — port-based cleanup only", port)
            self._pkill_by_port(port)
            self._set_vllm_pid(None)
            self._cleanup_pid_files("vllm")
            self._wait_gpu_idle()
            return {"status": "ok", "message": "port-based cleanup"}

        log.info("Stopping vLLM PGID=%d", pgid)

        # SIGTERM the process group
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            log.info("Process group %d already dead", pgid)
            self._set_vllm_pid(None)
            self._cleanup_pid_files("vllm")
            if port:
                self._pkill_by_port(port)
            return {"status": "ok", "message": "already dead"}

        # Wait for graceful shutdown
        for i in range(STOP_SIGTERM_TIMEOUT):
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                log.info("vLLM process group %d terminated gracefully in %ds", pgid, i + 1)
                self._set_vllm_pid(None)
                self._cleanup_pid_files("vllm")
                self._reap_zombies()
                self._wait_gpu_idle()
                if port:
                    self._pkill_by_port(port)
                return {"status": "ok", "message": f"terminated in {i + 1}s"}
            time.sleep(1)

        # SIGKILL
        log.warning("SIGTERM timeout for PGID %d, sending SIGKILL to group", pgid)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

        time.sleep(2)
        self._set_vllm_pid(None)
        self._cleanup_pid_files("vllm")
        self._reap_zombies()
        if port:
            self._pkill_by_port(port)
        self._wait_gpu_idle()
        return {"status": "ok", "message": "killed (SIGKILL)"}

    def _pkill_by_port(self, port: int) -> None:
        """Kill any remaining process listening on a specific port.

        Safety net for orphaned processes not tracked in state.db.
        """
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                result = subprocess.run(
                    ["fuser", "-k", "-" + str(sig), str(port) + "/tcp"],
                    timeout=5, check=False, capture_output=True
                )
                if result.returncode == 0:
                    log.info("fuser killed processes on port %d (sig=%d)", port, sig)
                    time.sleep(1)
                    break
            except FileNotFoundError:
                # fuser not available — fall back to pkill
                subprocess.run(
                    ["pkill", "-" + str(sig), "-f", f"vllm.*:{port}"],
                    timeout=5, check=False, capture_output=True
                )
                subprocess.run(
                    ["pkill", "-" + str(sig), "-f", f"VLLM::EngineCore.*--port {port}"],
                    timeout=5, check=False, capture_output=True
                )
                time.sleep(1)
                break
            except subprocess.TimeoutExpired:
                log.warning("fuser on port %d timed out, skipping", port)
                break
        time.sleep(1)

    def _pkill_vllm_fallback(self) -> dict:
        """Fallback: stop vLLM when no PID is tracked.

        Strategy (safe, no global pkill -f):
          1. Scan PID files in log_dir → kill -9 <pid> → wait.
          2. For each known vLLM port: fuser <port>/tcp (only kills on that port).
        """
        # Step 1: Kill by PID files
        for pf in sorted(self._log_dir.glob("vllm_*.pid")):
            try:
                pid = int(pf.read_text().strip())
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.kill(pid, sig)
                        log.info("%s vLLM PID %d via PID file %s",
                                 "SIGTERM" if sig == signal.SIGTERM else "SIGKILL",
                                 pid, pf.name)
                    except (ProcessLookupError, PermissionError):
                        pass
                time.sleep(1)
            except (ValueError, OSError) as e:
                log.debug("Failed to read PID from %s: %s", pf, e)
            pf.unlink(missing_ok=True)

        # Step 2: Port-based cleanup via fuser (last resort)
        vllm_ports = []
        try:
            from .config import load_models
            for m in load_models().values():
                if m.vllm:
                    vllm_ports.append(m.vllm.port)
        except Exception:
            pass
        if not vllm_ports:
            vllm_ports = [8000, 8001, 8002]

        for port in vllm_ports:
            self._pkill_by_port(port)

        time.sleep(2)
        self._cleanup_pid_files("vllm")
        self._reap_zombies()
        self._wait_gpu_idle()
        return {"status": "ok", "message": "pkill fallback"}

    # ─── ComfyUI ─────────────────────────────────────────────────

    def start_comfyui(self, cfg: ComfyUIConfig) -> dict:
        """Start ComfyUI. Uses native Python process management when config supports it,
        falls back to bash script for legacy configs."""
        if cfg.use_native:
            return self._start_comfyui_native(cfg)
        elif cfg.startup_script:
            return self._start_comfyui_script(cfg)
        else:
            return {"status": "error", "message": "ComfyUI config has neither conda_env nor startup_script"}

    def _start_comfyui_native(self, cfg: ComfyUIConfig) -> dict:
        """Start ComfyUI natively via conda env's Python with process group isolation."""
        python_bin = CONDA_ENVS / cfg.conda_env / "bin" / "python"
        if not python_bin.exists():
            log.error("Python binary not found: %s", python_bin)
            return {"status": "error", "message": f"python not found in conda env {cfg.conda_env}"}

        main_py = cfg.resolved_working_dir / "main.py"
        if not main_py.exists():
            log.error("ComfyUI main.py not found: %s", main_py)
            return {"status": "error", "message": f"main.py not found at {main_py}"}

        cmd = [str(python_bin), str(main_py), "--listen", "0.0.0.0",
               "--port", str(cfg.port)]
        if cfg.extra_flags:
            cmd.extend(shlex.split(cfg.extra_flags))

        log.info("Starting ComfyUI cmd: %s", " ".join(cmd))
        env = dict(os.environ)
        env["HF_ENDPOINT"] = "https://hf-mirror.com"
        # Add CUDA runtime to LD_LIBRARY_PATH
        cuda_rt = str(CONDA_ENVS / cfg.conda_env / "lib" / "python3.12" / "site-packages" / "nvidia" / "cuda_runtime" / "lib")
        env["LD_LIBRARY_PATH"] = cuda_rt + (":" + env.get("LD_LIBRARY_PATH", "") if env.get("LD_LIBRARY_PATH") else "")
        # Add conda env's bin/ to PATH
        conda_bin = str(CONDA_ENVS / cfg.conda_env / "bin")
        env["PATH"] = conda_bin + ":" + env.get("PATH", "")

        log_file = self._log_dir / "comfyui.log"
        log_file.write_text("")

        log_fh = open(str(log_file), "a")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
                cwd=str(cfg.resolved_working_dir),
            )
        except Exception as e:
            log.error("Failed to start ComfyUI: %s", e)
            return {"status": "error", "message": f"Popen failed: {e}"}
        finally:
            log_fh.close()

        pgid = proc.pid  # start_new_session → PID == PGID
        self._set_comfyui_pid(pgid)
        pid_file = self._log_dir / "comfyui.pid"
        pid_file.write_text(str(pgid))
        log.info("ComfyUI started: PID=%d (PGID=%d)", proc.pid, pgid)

        # Quick check for immediate failure
        for _ in range(6):  # 3 seconds
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = "read log failed"
                log.error("ComfyUI exited immediately (ret=%d): %s", ret, err[-500:])
                self._set_comfyui_pid(None)
                pid_file.unlink(missing_ok=True)
                return {"status": "error", "message": f"ComfyUI exited with code {ret}", "log": str(log_file)}
            time.sleep(0.5)

        # Wait for health check
        health_url = cfg.health_url or f"http://localhost:{cfg.port}/system_stats"
        healthy = wait_http(health_url, timeout=120)
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        else:
            if proc.poll() is not None:
                return {"status": "error", "message": "ComfyUI crashed during loading"}
            else:
                self.stop_comfyui()
                return {"status": "timeout", "message": "ComfyUI didn't become healthy within 2 minutes"}

    def _start_comfyui_script(self, cfg: ComfyUIConfig) -> dict:
        """Legacy: start ComfyUI via bash startup script."""
        script = Path(cfg.startup_script).expanduser().resolve()
        home = Path.home().resolve()
        if not (script.is_absolute() and (str(script).startswith(str(home)) or str(script).startswith("/home"))):
            log.error("Unsafe ComfyUI script path: %s", script)
            return {"status": "error", "message": "Script path must be absolute under home"}
        try:
            result = subprocess.run([str(script), "start"], timeout=120, check=False)
            return {"status": "started" if result.returncode == 0 else "error"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def stop_comfyui(self, port: Optional[int] = None) -> dict:
        """Stop ComfyUI using process group kill (native) or stop script (legacy).

        When ``port`` is supplied, also does port-based cleanup as a safety net.
        """
        pgid = self.comfyui_pid
        if pgid is not None:
            # P1-2: 校验 PID 是否已被操作系统复用到无关进程
            if self._validate_pid(pgid, "comfyui"):
                result = self._stop_comfyui_native(pgid)
            else:
                log.warning(
                    "Tracked ComfyUI PID %d does not appear to be a ComfyUI process "
                    "(cmdline mismatch) — clearing stale PID and falling back",
                    pgid,
                )
                self._set_comfyui_pid(None)
                result = self._pkill_comfyui_fallback()
        elif self._comfyui_process_exists():
            log.warning("No ComfyUI PID tracked, falling back to pkill")
            result = self._pkill_comfyui_fallback()
        else:
            log.info("No ComfyUI process running — skip stop")
            result = {"status": "ok", "message": "not running"}

        # Port-based safety net
        if port:
            log.info("Port-based cleanup for ComfyUI on port %d", port)
            self._pkill_by_port(port)
        return result

    def stop_comfyui_with_config(self, cfg: ComfyUIConfig, port: Optional[int] = None) -> dict:
        """Stop ComfyUI with config knowledge for legacy script fallback.

        When ``port`` is supplied, also does port-based cleanup as a safety net.
        """
        port = port or cfg.port
        pgid = self.comfyui_pid
        if pgid is not None:
            result = self._stop_comfyui_native(pgid)
        elif cfg.stop_script:
            result = self._stop_comfyui_script(cfg)
        else:
            result = self._pkill_comfyui_fallback()

        # Port-based safety net
        log.info("Port-based cleanup for ComfyUI on port %d", port)
        self._pkill_by_port(port)
        return result

    def _stop_comfyui_native(self, pgid: int) -> dict:
        """Stop ComfyUI by process group. SIGTERM → wait → SIGKILL."""
        log.info("Stopping ComfyUI PGID=%d", pgid)

        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            log.info("ComfyUI process group %d already dead", pgid)
            self._set_comfyui_pid(None)
            self._cleanup_pid_files("comfyui")
            return {"status": "ok", "message": "already dead"}

        for i in range(STOP_SIGTERM_TIMEOUT):
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                log.info("ComfyUI process group %d terminated gracefully in %ds", pgid, i + 1)
                self._set_comfyui_pid(None)
                self._cleanup_pid_files("comfyui")
                self._wait_gpu_idle()
                return {"status": "ok", "message": f"terminated in {i + 1}s"}
            time.sleep(1)

        log.warning("SIGTERM timeout for ComfyUI PGID %d, sending SIGKILL", pgid)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

        time.sleep(2)
        self._set_comfyui_pid(None)
        self._cleanup_pid_files("comfyui")
        self._wait_gpu_idle()
        return {"status": "ok", "message": "killed (SIGKILL)"}

    def _stop_comfyui_script(self, cfg: ComfyUIConfig) -> dict:
        """Legacy: stop ComfyUI via bash stop script."""
        script = Path(cfg.stop_script).expanduser().resolve()
        try:
            result = subprocess.run(
                ["bash", "-c", f"{script} stop"],
                timeout=15, check=False, capture_output=True
            )
            self._set_comfyui_pid(None)
            return {"status": "ok", "returncode": result.returncode}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _comfyui_process_exists(self) -> bool:
        """Check if any ComfyUI process is actually running."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"python.*{_re.escape(str(COMFYUI_DIR))}"],
                timeout=3, capture_output=True,
            )
            return result.returncode == 0
        except Exception:
            return True  # assume exists on error (safe default)

    def _pkill_comfyui_fallback(self) -> dict:
        """Fallback: stop ComfyUI via pkill."""
        subprocess.run(["pkill", "-f", f"python.*{_re.escape(str(COMFYUI_DIR))}/main.py"], timeout=5, check=False, capture_output=True)
        time.sleep(2)
        # SIGKILL remaining
        subprocess.run(["pkill", "-9", "-f", f"python.*{_re.escape(str(COMFYUI_DIR))}/main.py"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", f"python.*{_re.escape(str(COMFYUI_DIR))}"], timeout=5, check=False)
        time.sleep(1)
        self._set_comfyui_pid(None)
        self._cleanup_pid_files("comfyui")
        self._wait_gpu_idle()
        return {"status": "ok", "message": "pkill fallback"}

    # ─── Ollama.cpp ───────────────────────────────────────────

    def start_ollama_cpp(self, cfg: "OllamaCppConfig") -> dict:
        """Start Ollama.cpp / llama.cpp server for a specific model.

        Each model gets its own process with process group isolation.
        Uses llama-server binary (OpenAI-compatible API).
        """
        from .config import OllamaCppConfig
        model_path = Path(cfg.model_path).expanduser().resolve()
        if not model_path.exists():
            return {"status": "error", "message": f"GGUF model not found: {model_path}"}

        # ── Select llama-server binary by GPU offload mode ──
        # gpu_layers != 0 → CUDA build (GPU offload)
        # gpu_layers == 0 → CPU-only build (no CUDA runtime overhead)
        llama_cpp_base = Path.home() / "llama-cpp"
        if cfg.gpu_layers != 0:
            llama_server = llama_cpp_base / "build-cuda" / "bin" / "llama-server"
            if not llama_server.exists():
                return {"status": "error", "message": f"CUDA llama-server not found at {llama_server}"}
        else:
            llama_server = llama_cpp_base / "build" / "bin" / "llama-server"
            if not llama_server.exists():
                # Fallback to conda/PATH for backward compat
                conda_base = Path.home() / "miniconda3"
                conda_bin = conda_base / "bin"
                llama_server = conda_bin / "llama-server"
                if not llama_server.exists():
                    import shutil
                    llama_server_path = shutil.which("llama-server")
                    if llama_server_path:
                        llama_server = Path(llama_server_path)
                    else:
                        return {"status": "error", "message": f"llama-server not found"}

        cmd = [
            str(llama_server),
            "-m", str(model_path),
            "--host", "0.0.0.0",
            "--port", str(cfg.port),
            "-c", str(cfg.context_size),
            "-t", str(cfg.threads),
        ]
        # Enable embeddings endpoint for embedding models
        cmd.append("--embeddings")
        if cfg.gpu_layers != 0:
            cmd.extend(["-ngl", str(cfg.gpu_layers)])

        log.info("Starting ollama.cpp: %s", " ".join(cmd[:6]) + "...")
        log_file = self._log_dir / f"ollama_cpp_{cfg.port}.log"
        log_file.write_text("")

        env = dict(os.environ)
        # Ensure llama-server can find its shared libs
        llama_server_dir = str(llama_server.parent)
        env["PATH"] = llama_server_dir + ":" + env.get("PATH", "")

        log_fh = open(str(log_file), "a")
        try:
            proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
        except Exception as e:
            log_fh.close()
            return {"status": "error", "message": f"ollama.cpp Popen failed: {e}"}
        finally:
            log_fh.close()

        pgid = proc.pid
        pid_file = self._log_dir / f"ollama_cpp_{cfg.port}.pid"
        pid_file.write_text(str(pgid))
        log.info("ollama.cpp started: PID=%d, port=%d", pgid, cfg.port)

        # Quick failure detection
        for _ in range(6):
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = "read log failed"
                log.error("ollama.cpp exited immediately (ret=%d): %s", ret, err[-500:])
                pid_file.unlink(missing_ok=True)
                return {"status": "error", "message": f"ollama.cpp exited with code {ret}", "log": str(log_file)}
            time.sleep(0.5)

        healthy = wait_http(f"http://localhost:{cfg.port}/health", timeout=120)
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        else:
            self.stop_ollama_cpp(cfg.port)
            return {"status": "timeout", "message": "ollama.cpp didn't become healthy within 2 minutes"}

    def stop_ollama_cpp(self, port: Optional[int] = None):
        """Stop ollama.cpp via port-based cleanup."""
        if port:
            self._pkill_by_port(port)
        # Clean up PID file
        for pf in self._log_dir.glob("ollama_cpp_*.pid"):
            pf.unlink(missing_ok=True)

    def run_ollama(self, model_ref: str, keep_alive: str = "5m", num_gpu: int = -1) -> dict:
        """Trigger `ollama run` to load/pull a model into the Ollama daemon.

        Pipes empty stdin so the CLI loads the model then exits without
        blocking. `num_gpu` is passed via `OLLAMA_NUM_GPU` env var to control
        GPU offloading layers (e.g., 0 for CPU-only, -1 for auto).
        """
        env = os.environ.copy()
        if num_gpu >= 0:
            env["OLLAMA_NUM_GPU"] = str(num_gpu)

        cmd = ["ollama", "run", model_ref, "--keepalive", keep_alive]
        try:
            result = subprocess.run(
                cmd, input="\n", capture_output=True, text=True, timeout=60, env=env
            )
        except FileNotFoundError:
            return {"status": "error", "message": "ollama CLI not found in PATH"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": f"ollama run timed out after 60s for {model_ref}"}
        if result.returncode == 0:
            return {"status": "ok", "message": f"Loaded {model_ref} into Ollama daemon"}
        return {
            "status": "error",
            "message": f"ollama run failed: {result.stderr.strip() or result.stdout.strip()}",
        }

    # ─── TTS Server ──────────────────────────────────────────────

    def start_tts_server(self, cfg: TTSConfig) -> dict:
        """Start TTS server via conda env with process group isolation.

        Reuses the same Popen + start_new_session pattern as ComfyUI,
        with extra_env injection from model YAML config.
        """
        python_bin = CONDA_ENVS / cfg.conda_env / "bin" / "python"
        if not python_bin.exists():
            log.error("Python binary not found: %s", python_bin)
            return {"status": "error", "message": f"python not found in conda env {cfg.conda_env}"}

        working_dir = cfg.resolved_working_dir
        if not working_dir.exists():
            log.error("TTS working_dir not found: %s", working_dir)
            return {"status": "error", "message": f"working_dir not found: {working_dir}"}

        # Parse start_cmd (may contain quoted arguments)
        cmd = shlex.split(cfg.start_cmd)
        cmd[0] = str(python_bin)  # replace 'python' with conda absolute path

        log.info("Starting TTS server cmd: %s", " ".join(cmd))
        env = dict(os.environ)

        # Inject extra_env from model YAML (highest priority)
        if cfg.extra_env:
            for k, v in cfg.extra_env.items():
                env[k] = v
                log.debug("TTS extra_env: %s=%s", k, v)

        # Add conda env's bin/ to PATH
        conda_bin = str(CONDA_ENVS / cfg.conda_env / "bin")
        env["PATH"] = conda_bin + ":" + env.get("PATH", "")

        log_file = self._log_dir / "tts_server.log"
        log_file.write_text("")

        log_fh = open(str(log_file), "a")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
                cwd=str(working_dir),
            )
        except Exception as e:
            log.error("Failed to start TTS server: %s", e)
            return {"status": "error", "message": f"Popen failed: {e}"}
        finally:
            log_fh.close()

        pgid = proc.pid  # start_new_session → PID == PGID
        self._set_tts_pid(pgid)
        pid_file = self._log_dir / "tts_server.pid"
        pid_file.write_text(str(pgid))
        log.info("TTS server started: PID=%d (PGID=%d)", proc.pid, pgid)

        # Quick check for immediate failure
        for _ in range(6):  # 3 seconds
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = "read log failed"
                log.error("TTS server exited immediately (ret=%d): %s", ret, err[-500:])
                self._set_tts_pid(None)
                pid_file.unlink(missing_ok=True)
                return {"status": "error", "message": f"TTS server exited with code {ret}", "log": str(log_file)}
            time.sleep(0.5)

        # Wait for health check
        health_url = cfg.health_url or f"http://localhost:{cfg.port}/health"
        timeout = cfg.health_check_timeout or 180
        healthy = wait_http(health_url, timeout=timeout)
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        else:
            if proc.poll() is not None:
                return {"status": "error", "message": "TTS server crashed during startup"}
            else:
                self.stop_tts_server(port=cfg.port)
                return {"status": "timeout", "message": f"TTS server didn't become healthy within {timeout}s"}

    def stop_tts_server(self, port: Optional[int] = None) -> dict:
        """Stop TTS server using process group kill. SIGTERM → wait → SIGKILL."""
        pgid = self.tts_pid

        if pgid is not None and not self._validate_pid(pgid, "tts"):
            log.warning("Tracked TTS PID %d is stale, clearing", pgid)
            self._set_tts_pid(None)
            pgid = None

        if pgid is None and port is None:
            log.info("No TTS process running — skip stop")
            return {"status": "ok", "message": "not running"}

        if pgid is None:
            # No tracked PID but port given — port-based cleanup
            log.info("No TTS PID tracked, port=%d — port-based cleanup", port)
            self._pkill_by_port(port)
            self._set_tts_pid(None)
            self._cleanup_pid_files("tts")
            return {"status": "ok", "message": "port-based cleanup"}

        log.info("Stopping TTS server PGID=%d", pgid)

        # SIGTERM the process group
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            log.info("TTS process group %d already dead", pgid)
            self._set_tts_pid(None)
            self._cleanup_pid_files("tts")
            if port:
                self._pkill_by_port(port)
            return {"status": "ok", "message": "already dead"}

        # Wait for graceful shutdown
        for i in range(STOP_SIGTERM_TIMEOUT):
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                log.info("TTS process group %d terminated gracefully in %ds", pgid, i + 1)
                self._set_tts_pid(None)
                self._cleanup_pid_files("tts")
                self._reap_zombies()
                if port:
                    self._pkill_by_port(port)
                return {"status": "ok", "message": f"terminated in {i + 1}s"}
            time.sleep(1)

        # SIGKILL
        log.warning("SIGTERM timeout for TTS PGID %d, sending SIGKILL", pgid)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

        time.sleep(2)
        self._set_tts_pid(None)
        self._cleanup_pid_files("tts")
        self._reap_zombies()
        if port:
            self._pkill_by_port(port)
        return {"status": "ok", "message": "killed (SIGKILL)"}

    # ─── Combined Operations ─────────────────────────────────────

    def stop_all(
        self,
        comfyui_cfg: Optional[ComfyUIConfig] = None,
        vllm_ports: Optional[list[int]] = None,
        comfyui_port: Optional[int] = None,
        tts_port: Optional[int] = None,
    ) -> dict:
        """Stop all services: ComfyUI first, then vLLM, then TTS.

        Port parameters are used for port-based safety-net cleanup.
        """
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
        return results

    def force_kill_all(self) -> dict:
        """Nuclear option: SIGKILL everything related to vLLM + ComfyUI + TTS."""
        # vLLM
        pgid = self.vllm_pid
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        subprocess.run(["pkill", "-9", "-f", "vllm serve"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "VLLM::EngineCore"], timeout=5, check=False)
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
        # Try to kill ComfyUI specifically by working dir
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
        # Port-based cleanup for TTS
        subprocess.run(["fuser", "-k", "8880/tcp"], timeout=5, check=False)

        time.sleep(2)
        self._set_vllm_pid(None)
        self._set_comfyui_pid(None)
        self._set_tts_pid(None)
        self._cleanup_pid_files("vllm")
        self._cleanup_pid_files("comfyui")
        self._cleanup_pid_files("tts")
        self._reap_zombies()
        self._wait_gpu_idle()
        return {"status": "ok"}

    # ─── Health Checks/Sleep ─────────────────────────────────────

    # ─── Sleep/Wake ────────────────────────────────────────────

    def sleep_vllm(self, port: int) -> dict:
        """Put vLLM server to L2 sleep (discard weights, free VRAM)."""
        url = f"http://localhost:{port}/sleep?level=2"
        log.info("Sleeping vLLM at port %d (L2)", port)
        t0 = time.time()
        try:
            req = urllib.request.Request(url, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                elapsed = round(time.time() - t0, 1)
                log.info("vLLM sleep OK (port=%d, %.1fs)", port, elapsed)
                return {"status": "ok", "port": port, "elapsed_sec": elapsed}
        except urllib.error.HTTPError as e:
            elapsed = round(time.time() - t0, 1)
            log.error("vLLM sleep HTTP %d (port=%d): %s", e.code, port, e.reason)
            return {"status": "error", "message": f"HTTP {e.code}: {e.reason}", "elapsed_sec": elapsed}
        except Exception as e:
            elapsed = round(time.time() - t0, 1)
            log.error("vLLM sleep failed (port=%d): %s", port, e)
            return {"status": "error", "message": str(e), "elapsed_sec": elapsed}

    def wake_vllm(self, port: int) -> dict:
        """L2 wake: kill sleeping process, then cold restart via switch.

        vLLM 0.23.0 L2 sleep leaves the engine in an unrecoverable state
        (wake_up CUDA invalid argument). We kill the sleeping process
        and let the caller handle restart.
        """
        log.info("Killing sleeping vLLM at port %d for restart", port)
        self.stop_vllm()
        return {"status": "killed_for_restart", "port": port, "elapsed_sec": 0}

    def is_sleeping(self, port: int) -> bool:
        """Check if vLLM server is currently in sleep mode."""
        try:
            req = urllib.request.Request(f"http://localhost:{port}/is_sleeping")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("is_sleeping", False)
        except Exception:
            return False

    def is_vllm_alive(self, port: int) -> bool:
        """Check if vLLM process is still alive (by PID or HTTP)."""
        pgid = self.vllm_pid
        if pgid:
            try:
                os.killpg(pgid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False
        return check_http_status(f"http://localhost:{port}/health") != "❌"

    def is_comfyui_alive(self, port: int = 8188) -> bool:
        """Check if ComfyUI process is still alive (by PID or HTTP)."""
        pgid = self.comfyui_pid
        if pgid:
            try:
                os.killpg(pgid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False
        health_url = f"http://localhost:{port}/system_stats"
        return check_http_status(health_url) != "❌"

    # ─── GPU Cleanup ─────────────────────────────────────────────

    def _wait_gpu_idle(self, timeout: int = 60, force: bool = False) -> dict:
        """P1-2: Wait for GPU to return to idle state after process exit.
        
        Uses a relative baseline: records initial idle usage and checks
        if current usage is within 15% of baseline. This handles desktop
        environments where compositor/CUDA usage varies.
        
        Args:
            force: If True, skip waiting and return immediately.
        """
        if force:
            return {"status": "force", "used_mb": gpu_used_mb()}
        
        # Get baseline idle GPU memory (first call or cached)
        baseline = self._get_gpu_baseline()
        threshold = int(baseline * 1.5) + 512  # 150% of baseline + 512MB margin
        
        for _ in range(timeout):
            used = gpu_used_mb()
            if used is not None and used <= threshold:
                log.info("GPU returned to idle (%d MB, threshold=%d)", used, threshold)
                return {"status": "ok", "used_mb": used}
            time.sleep(1)
        
        # If we timeout but GPU is dropping, give it more time
        used = gpu_used_mb()
        if used is not None and used < threshold * 0.8:
            log.info("GPU still dropping (%d MB), accepting", used)
            return {"status": "ok", "used_mb": used}
        
        return {"status": "timeout", "message": f"GPU did not return to idle (threshold={threshold}MB)"}
    
    def _get_gpu_baseline(self) -> int:
        """Get or cache the baseline GPU memory usage.

        P1-3: Uses a 7-day TTL on the cached baseline to prevent stale
        measurements.  Only persists new measurements when the GPU is
        idle (measured <= cached baseline).  If the cache is expired
        and the GPU is busy, returns the measured value for this call
        but does not persist it — unless the cached value itself is
        unreasonable (>2GB idle), in which case the cache is discarded.
        """
        SEVEN_DAYS = 7 * 86400  # seconds
        REASONABLE_IDLE_MAX = 2048  # MB — idle baseline should never exceed this
        cache_file = Path.home() / ".inferfabric" / "gpu_baseline.json"

        # ── Read cached value ──────────────────────────────────────
        cached_baseline: int | None = None
        cached_ts: float = 0.0
        try:
            if cache_file.exists():
                data = json.loads(cache_file.read_text())
                cached_baseline = int(data.get("baseline_mb", 0)) or None
                cached_ts = float(data.get("timestamp", 0))
        except Exception:
            cached_baseline = None
            cached_ts = 0.0

        # Return cached value if still valid (within TTL)
        if cached_baseline and cached_ts > 0:
            age = time.time() - cached_ts
            if age < SEVEN_DAYS:
                return cached_baseline
            else:
                log.info("GPU baseline cache expired (age=%.0f days), re-sampling", age / 86400)

        # ── Re-sample ──────────────────────────────────────────────
        measured = gpu_used_mb()
        baseline = measured if (measured and measured >= 100) else 512

        # Guard: only persist if GPU is actually idle (current usage
        # is within 150% of a reasonable baseline).  If the GPU is
        # currently loaded, use measured value for this call but don't
        # persist it — and don't return a stale expired cache either.
        if cached_baseline and measured:
            # If cached value is unreasonable (>2GB idle), discard it entirely
            if cached_baseline > REASONABLE_IDLE_MAX:
                log.info(
                    "Discarding unreasonable cached baseline (%d MB > %d MB)",
                    cached_baseline, REASONABLE_IDLE_MAX,
                )
                # Fall through to persist measured value
            elif measured > cached_baseline:
                # GPU is currently loaded — return cached idle value,
                # don't persist measured (would poison the idle baseline)
                log.info(
                    "Skipping baseline persist — GPU appears busy "
                    "(used=%d MB, cached baseline=%d MB)",
                    measured, cached_baseline,
                )
                return cached_baseline
        elif measured and measured < 100:
            # measured < 100 is suspicious; keep cached if available
            if cached_baseline:
                return cached_baseline

        # Persist new baseline with timestamp
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({
                "baseline_mb": baseline,
                "timestamp": time.time(),
            }))
            log.info("GPU baseline updated: %d MB", baseline)
        except Exception:
            pass

        return baseline

    # ─── PID Validation ────────────────────────────────────────────

    def _validate_pid(self, pid: int, expected_substring: str) -> bool:
        """P1-2: Validate that a PID still belongs to the expected process.

        Reads ``/proc/<pid>/cmdline`` (null-separated bytes) and checks
        for ``expected_substring`` (case-insensitive).  Returns False if
        the PID has been recycled by the kernel to an unrelated process.
        """
        cmdline_path = f"/proc/{pid}/cmdline"
        try:
            raw = Path(cmdline_path).read_bytes()
        except FileNotFoundError:
            return False  # PID no longer exists
        except PermissionError:
            # Cannot read cmdline — conservatively assume PID is still valid
            # to avoid accidentally killing an unrelated process via fallback.
            log.debug("Cannot read /proc/%d/cmdline (permission denied), assuming valid", pid)
            return True
        except OSError:
            return False

        # Empty cmdline = kernel thread — PID recycled, not a user process
        if not raw:
            return False

        try:
            cmdline = raw.decode("utf-8", errors="replace")
        except Exception:
            return False

        return expected_substring.lower() in cmdline.lower()

    # ─── Internal Helpers ────────────────────────────────────────

    def _cleanup_pid_files(self, prefix: str):
        """Remove PID files for a given prefix (vllm or comfyui)."""
        for pf in self._log_dir.glob(f"{prefix}*.pid"):
            pf.unlink(missing_ok=True)
        if prefix == "vllm":
            # Also clean legacy PID files
            legacy_dir = Path.home() / "models" / "vllm_logs"
            if legacy_dir.exists():
                for pf in legacy_dir.glob("*.pid"):
                    pf.unlink(missing_ok=True)

    def _reap_zombies(self):
        """Reap zombie child processes."""
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
        except ChildProcessError:
            pass
