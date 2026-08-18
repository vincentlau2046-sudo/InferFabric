"""
inferfabric/process_manager/tts.py — TTS server process lifecycle sub-manager.
"""

import os
import time
import signal
import shlex
import logging
import subprocess
from pathlib import Path
from typing import Optional

from inferfabric.config import CONDA_ENVS, STOP_SIGTERM_TIMEOUT, TTSConfig
from inferfabric.health import wait_http
from inferfabric.process_manager.base import BaseProcessManager


log = logging.getLogger("inferfabric")


class TTSProcessManager(BaseProcessManager):
    """TTS server process lifecycle: start, stop."""

    # ─── PID accessors ──────────────────────────────────────────

    @property
    def tts_pid(self):
        pid_str = self._state.get("tts_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    def _set_tts_pid(self, pid):
        self._state.set("tts_pid", str(pid) if pid else "")


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

    # ─── ASR Server ──────────────────────────────────────────────

