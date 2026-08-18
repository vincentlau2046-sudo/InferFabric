"""
inferfabric/process_manager/asr.py — ASR server process lifecycle sub-manager.
"""

import os
import time
import signal
import shlex
import logging
import subprocess
from pathlib import Path
from typing import Optional

from inferfabric.config import CONDA_ENVS, STOP_SIGTERM_TIMEOUT, ASRConfig
from inferfabric.health import wait_http
from inferfabric.process_manager.base import BaseProcessManager


log = logging.getLogger("inferfabric")


class ASRProcessManager(BaseProcessManager):
    """ASR server process lifecycle: start, stop."""

    # ─── PID accessors ──────────────────────────────────────────

    @property
    def asr_pid(self):
        pid_str = self._state.get("asr_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    def _set_asr_pid(self, pid):
        self._state.set("asr_pid", str(pid) if pid else "")


    def start_asr_server(self, cfg: ASRConfig) -> dict:
        """Start ASR server via conda env with process group isolation.

        Reuses the same Popen + start_new_session pattern as TTS,
        with extra_env injection from model YAML config.
        """
        python_bin = CONDA_ENVS / cfg.conda_env / "bin" / "python"
        if not python_bin.exists():
            log.error("Python binary not found: %s", python_bin)
            return {"status": "error", "message": f"python not found in conda env {cfg.conda_env}"}

        working_dir = cfg.resolved_working_dir
        if not working_dir.exists():
            log.error("ASR working_dir not found: %s", working_dir)
            return {"status": "error", "message": f"working_dir not found: {working_dir}"}

        # Parse start_cmd (may contain quoted arguments)
        cmd = shlex.split(cfg.start_cmd)
        # If start_cmd starts with 'python', replace with conda absolute path
        if cmd[0] == "python":
            cmd[0] = str(python_bin)

        log.info("Starting ASR server cmd: %s", " ".join(cmd))
        env = dict(os.environ)

        # Inject extra_env from model YAML (highest priority)
        if cfg.extra_env:
            for k, v in cfg.extra_env.items():
                env[k] = v
                log.debug("ASR extra_env: %s=%s", k, v)

        # Add conda env's bin/ to PATH
        conda_bin = str(CONDA_ENVS / cfg.conda_env / "bin")
        env["PATH"] = conda_bin + ":" + env.get("PATH", "")

        log_file = self._log_dir / "asr_server.log"
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
            log.error("Failed to start ASR server: %s", e)
            return {"status": "error", "message": f"Popen failed: {e}"}
        finally:
            log_fh.close()

        pgid = proc.pid  # start_new_session → PID == PGID
        self._set_asr_pid(pgid)
        pid_file = self._log_dir / "asr_server.pid"
        pid_file.write_text(str(pgid))
        log.info("ASR server started: PID=%d (PGID=%d)", proc.pid, pgid)

        # Quick check for immediate failure
        for _ in range(6):  # 3 seconds
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = "read log failed"
                log.error("ASR server exited immediately (ret=%d): %s", ret, err[-500:])
                self._set_asr_pid(None)
                self._cleanup_pid_files("asr")
                return {"status": "error", "message": f"ASR server exited with code {ret}", "log": str(log_file)}
            time.sleep(0.5)

        # Wait for health check
        health_url = cfg.health_url or f"http://localhost:{cfg.port}/health"
        timeout = cfg.health_check_timeout or 120
        healthy = wait_http(health_url, timeout=timeout)
        if healthy:
            log.info("ASR server healthy at %s", health_url)
            return {"status": "ok", "pid": proc.pid}
        else:
            log.error("ASR server health check failed after %ds", timeout)
            self.stop_asr_server(port=cfg.port)
            return {"status": "error", "message": f"ASR server health check failed after {timeout}s"}

    def stop_asr_server(self, port: Optional[int] = None) -> dict:
        """Stop ASR server by killing its process group."""
        pgid = self.asr_pid
        if not pgid:
            log.warning("No ASR server PID recorded, attempting port-based cleanup")
            if port:
                self._pkill_by_port(port)
            return {"status": "ok", "message": "no ASR server running"}

        # SIGTERM
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            log.info("ASR server process %d already gone", pgid)
            self._set_asr_pid(None)
            self._cleanup_pid_files("asr")
            self._reap_zombies()
            if port:
                self._pkill_by_port(port)
            return {"status": "ok", "message": "already gone"}

        # Wait for graceful shutdown
        for i in range(STOP_SIGTERM_TIMEOUT):
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                self._set_asr_pid(None)
                self._cleanup_pid_files("asr")
                self._reap_zombies()
                if port:
                    self._pkill_by_port(port)
                return {"status": "ok", "message": f"terminated in {i + 1}s"}
            time.sleep(1)

        # SIGKILL
        log.warning("SIGTERM timeout for ASR PGID %d, sending SIGKILL", pgid)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

        time.sleep(2)
        self._set_asr_pid(None)
        self._cleanup_pid_files("asr")
        self._reap_zombies()
        if port:
            self._pkill_by_port(port)
        return {"status": "ok", "message": "killed (SIGKILL)"}

    # ─── Combined Operations ─────────────────────────────────────

