"""
inferfabric/process_manager/ninfer.py — NInfer Docker container lifecycle.

Mirrors VLLMProcessManager / SGLangProcessManager structure (D1).
start_ninfer / stop_ninfer migrated from the inline docker run/stop in
engine_adapter/ninfer.py — container_name now a parameter (single source
of truth = ModelConfig.container_name, threaded by the adapter).
"""

import os
import time
import logging
import subprocess
from pathlib import Path
from typing import Optional

from inferfabric.health import wait_http, check_http_status
from inferfabric.process_manager.base import BaseProcessManager


log = logging.getLogger("inferfabric")


class NInferProcessManager(BaseProcessManager):
    """NInfer Docker container lifecycle: start, stop. Mirrors SGLangProcessManager."""

    # ─── PID / container accessors ──────────────────────────────

    @property
    def ninfer_pid(self):
        pid_str = self._state.get("ninfer_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    def _set_ninfer_pid(self, pid):
        self._state.set("ninfer_pid", str(pid) if pid else "")

    @property
    def ninfer_container(self):
        c = self._state.get("ninfer_container")
        return c or None

    def _set_ninfer_container(self, name):
        self._state.set("ninfer_container", name or "")

    # ─── start ──────────────────────────────────────────────────

    def start_ninfer(self, cfg, container_name: str) -> dict:
        """Start NInfer via Docker container.

        Migrated from engine_adapter/ninfer.py:63-124. container_name is now
        a parameter (was cfg.container_name or f"ninfer-{cfg.port}"). Preserves
        the pre-start cleanup (docker stop stale same-name + 2s settle).
        """
        if not cfg:
            return {"status": "error", "message": "No ninfer config"}

        # Pre-start cleanup: stop any stale container with the same name (避免重名冲突)
        subprocess.run(["docker", "stop", container_name], timeout=10,
                       capture_output=True, check=False)
        time.sleep(2)

        weight_path = Path(cfg.weight_path).expanduser()
        weight_dir = str(weight_path.parent)

        cmd = [
            "docker", "run", "--gpus", "all", "--rm",
            "-v", f"{weight_dir}:/workspace",
            "-p", f"{cfg.port}:8080",
            "--name", container_name,
            "-e", "NVIDIA_DISABLE_REQUIRE=1",
            cfg.docker_image,
            "ninfer-serve", weight_path.name,
            "--model-id", cfg.model_id,
            "--host", "0.0.0.0", "--port", "8080",
            "--max-concurrency", str(cfg.max_concurrency),
            "--max-context", str(cfg.max_context),
            "--kv-capacity", "auto" if cfg.kv_capacity == 0 else str(cfg.kv_capacity),
            "--default-max-tokens", str(cfg.default_max_tokens),
            "--pending-timeout-ms", str(cfg.pending_timeout_ms),
            "--kv-dtype", cfg.kv_dtype,
            "--prefill-chunk", str(cfg.prefill_chunk),
        ]
        if cfg.enable_mtp:
            cmd.extend(["--spec", "mtp", "--draft-tokens", str(cfg.draft_tokens)])
        if cfg.enable_lm_head_draft:
            cmd.append("--lm-head-draft")

        log.info("Starting NInfer: %s", " ".join(cmd))
        log_file = Path(cfg.log_file or f"/tmp/ninfer-{cfg.port}.log")
        log_file.write_text("")

        try:
            proc = subprocess.Popen(
                cmd, stdout=log_file.open("a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:
            log.error("Failed to start NInfer: %s", e)
            return {"status": "error", "message": f"Popen failed: {e}"}

        pgid = proc.pid  # start_new_session → PID == PGID
        self._set_ninfer_pid(pgid)
        self._set_ninfer_container(container_name)
        log.info("NInfer docker PID=%s container=%s", proc.pid, container_name)

        # Immediate-exit detection (保留 ninfer adapter:117-120 的 6 轮 poll)
        for _ in range(6):
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = "read log failed"
                log.error("NInfer exited immediately (ret=%d): %s", ret, err[-500:])
                self._set_ninfer_pid(None)
                self._set_ninfer_container(None)
                return {"status": "error", "message": f"NInfer exited with code {ret}",
                        "log": str(log_file)}
            time.sleep(0.5)

        timeout = cfg.startup_timeout or 120
        healthy = wait_http(f"http://localhost:{cfg.port}/v1/models", timeout=timeout)
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        # 健康超时 → 清理
        self.stop_ninfer(container_name)
        return {"status": "timeout",
                "message": f"NInfer didn't become healthy within {timeout}s"}

    # ─── stop ───────────────────────────────────────────────────

    def stop_ninfer(self, container_name: str, timeout: int = 30) -> dict:
        """Stop NInfer container via `docker stop` (graceful SIGTERM→SIGKILL).

        Migrated from base._stop_docker_container — preserves the full guard
        (None-name / TimeoutExpired / FileNotFoundError / non-zero rc) and the
        fix-1 log.warning on each failure branch. No `docker rm`: ninfer's
        `docker run --rm` auto-removes on stop.
        """
        if not container_name:
            msg = "docker deployment has no container_name — cannot stop"
            log.warning(msg)
            return {"status": "warning", "message": msg}
        log.info("Stopping NInfer container: %s", container_name)
        try:
            result = subprocess.run(
                ["docker", "stop", container_name],
                timeout=timeout, capture_output=True, check=False)
        except subprocess.TimeoutExpired:
            msg = f"docker stop {container_name} timed out"
            log.warning(msg)
            return {"status": "warning", "message": msg}
        except FileNotFoundError:
            msg = "docker not found on PATH"
            log.warning(msg)
            return {"status": "warning", "message": msg}
        if result.returncode == 0:
            self._set_ninfer_pid(None)
            self._set_ninfer_container(None)
            return {"status": "ok", "message": f"Container {container_name} stopped"}
        stderr_fragment = result.stderr.decode()[:200] if result.stderr else ""
        msg = f"docker stop exit {result.returncode}: {stderr_fragment}"
        log.warning(msg)
        self._set_ninfer_pid(None)
        self._set_ninfer_container(None)
        return {"status": "warning", "message": msg}

    # ─── liveness ───────────────────────────────────────────────

    def is_ninfer_alive(self, port: int) -> bool:
        return check_http_status(f"http://localhost:{port}/v1/models", timeout=2) == "✅"
