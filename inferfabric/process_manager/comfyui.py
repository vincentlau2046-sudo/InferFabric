"""
inferfabric/process_manager/comfyui.py — ComfyUI process lifecycle sub-manager.
"""

import os
import re as _re
import time
import signal
import shlex
import logging
import subprocess
from pathlib import Path
from typing import Optional

from inferfabric.config import CONDA_ENVS, COMFYUI_DIR, STOP_SIGTERM_TIMEOUT, ComfyUIConfig
from inferfabric.health import wait_http
from inferfabric.process_manager.base import BaseProcessManager


log = logging.getLogger("inferfabric")


class ComfyUIProcessManager(BaseProcessManager):
    """ComfyUI process lifecycle: start, stop."""

    # ─── PID accessors ──────────────────────────────────────────

    @property
    def comfyui_pid(self):
        pid_str = self._state.get("comfyui_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    def _set_comfyui_pid(self, pid):
        self._state.set("comfyui_pid", str(pid) if pid else "")


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

