"""
inferfabric/proxy_manager.py — Model switching, health check, and request routing.

Extracted from proxy.py for modularity.
"""

import json
import logging
import os
import threading
import time
from http.client import HTTPConnection
from typing import Optional

from inferfabric.manager import ModelManager
from inferfabric.state import GPUMode
from inferfabric.config import load_aliases, MODELS_DIR

log = logging.getLogger("inferfabric.proxy_manager")


# ─── Config ──────────────────────────────────────────────────────

PROXY_HOST = os.environ.get("EDGE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("EDGE_PROXY_PORT", "8999"))
AUTO_SWITCH = os.environ.get("EDGE_AUTO_SWITCH", "1") == "1"
HEALTH_CHECK_INTERVAL = int(os.environ.get("EDGE_HEALTH_CHECK", "60"))
WATCHDOG_INTERVAL = 20


class ProxyManager:
    """Manages model switching + request routing (v4.0: model-plugin)."""

    def __init__(self, mgr: Optional["ModelManager"] = None, models_dir: str | None = None):
        self.mgr = mgr if mgr is not None else ModelManager(models_dir or str(MODELS_DIR))
        self._aliases = load_aliases()
        self._last_switch = 0.0
        self._cooldown = 10
        self._switch_lock = threading.Lock()
        log.info("Loaded %d model aliases: %s", len(self._aliases), list(self._aliases.keys()))

    @property
    def current(self) -> str:
        """Current active service or 'idle'."""
        return self.mgr.current_service

    def model_to_service(self, model_name: str):
        """Map served_model_name to model config name. Resolves aliases first."""
        resolved = self._aliases.get(model_name, model_name)
        m = self.mgr.find_model_by_served_name(resolved)
        if m:
            log.debug("model_to_service: %s → %s (served=%s)", model_name, resolved, m.name)
            return m.name
        if resolved != model_name:
            m2 = self.mgr.find_model_by_served_name(model_name)
            if m2:
                return m2.name
        return None

    def _wait_healthy(self, target: str, timeout: float = 180) -> bool:
        """Wait for a model to become healthy after switch."""
        model = self.mgr.get_model(target)
        if not model:
            return False
        port = model.port
        if not port:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            conn = None
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                resp.read()
                if resp.status == 200:
                    conn.close()
                    log.info("Model %s healthy on :%d", target, port)
                    return True
                resp.close()
            except Exception:
                pass
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
            time.sleep(2)
        log.warning("Model %s not healthy after %.0fs", target, timeout)
        return False

    def ensure_service(self, target: str) -> bool:
        """Ensure a model is running, auto-switch if needed."""
        if target in self.mgr.active_services:
            return True
        if self.mgr.state.is_manually_stopped(target):
            log.info("Auto-switch to %s blocked: manually stopped by user", target)
            return False
        if not self._switch_lock.acquire(timeout=0):
            log.warning("Switch already in progress, rejecting")
            return None  # caller should send 409
        try:
            if time.time() - self._last_switch < self._cooldown:
                log.warning("Switch cooldown active, skipping")
                return False
            log.info("Auto-switch → %s", target)
            result = self.mgr.switch(target)
            ok = result["status"] == "switched"
            if ok:
                self._last_switch = time.time()
                return self._wait_healthy(target)
            return result["status"] in ("switched", "already_active")
        finally:
            self._switch_lock.release()

    def get_target_port(self, model_name: str):
        """Get port for a served_model_name."""
        resolved = self._aliases.get(model_name, model_name)
        m = self.mgr.find_model_by_served_name(resolved)
        if not m and resolved != model_name:
            m = self.mgr.find_model_by_served_name(model_name)
        return m.port if m else None

    def make_conn(self, port: int, timeout: int = 300) -> HTTPConnection:
        """Create new HTTP connection per request — no pool (thread-safe).

        Each thread gets its own connection to vLLM, avoiding race conditions.
        vLLM handles concurrent connections natively.
        """
        return HTTPConnection("127.0.0.1", port, timeout=timeout)

    def health_check(self):
        try:
            s = self.mgr.status()
            self.mgr.cleanup_dead_services()
            log.info("Health check: gpu_mode=%s services=%s",
                     s.get("gpu_mode"), s.get("active_services"))
            for svc, health in s.get("services_health", {}).items():
                if health == "❌" and s.get("gpu_mode") != GPUMode.IDLE:
                    log.warning("%s unhealthy but GPU not idle — use `iff reconcile`", svc)
            self._clean_manual_stops()
        except Exception as e:
            log.error("Health check exception: %s", e)

    def _clean_manual_stops(self):
        """Remove expired manual_stop records from StateDB."""
        try:
            stops = json.loads(self.mgr.state.get("manual_stops") or "{}")
            expired = [k for k, v in stops.items() if time.time() - v > self.mgr.state.MANUAL_STOP_TTL]
            if expired:
                for k in expired:
                    del stops[k]
                self.mgr.state.set("manual_stops", json.dumps(stops))
                log.debug("Cleaned %d expired manual_stop records", len(expired))
        except Exception as e:
            log.debug("Manual stop cleanup error: %s", e)
