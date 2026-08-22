"""inferfabric/health_monitor.py — Model health tracking + cleanup (v5.2).

Extracted from proxy_manager.py for single-responsibility.  
Runs background health checks, reconciles state, and cleans up expired manual stops.
"""

import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inferfabric.manager import ModelManager
    from inferfabric.state import StateDB

log = logging.getLogger("inferfabric.health_monitor")


class HealthMonitor:
    """Background health checker with state reconciliation.

    Usage:
        monitor = HealthMonitor(mgr, state, interval=30)
        monitor.start()
        # ... later ...
        monitor.stop()
    """

    def __init__(self, mgr: "ModelManager", state: "StateDB", interval: float = 60.0):
        self._mgr = mgr
        self._state = state
        self._interval = interval
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="health-monitor")
        self._thread.start()
        log.info("HealthMonitor started (interval=%.0fs)", self._interval)

    def stop(self, timeout: float = 5.0):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                log.warning("Health check error: %s", e)
            self._stop_event.wait(timeout=self._interval)

    def _tick(self):
        """Single health check tick: cleanup stale manual stops + reconcile state."""
        self._clean_manual_stops()
        self._reconcile()

    def _clean_manual_stops(self):
        """Remove manual_stop entries that have exceeded TTL."""
        active = set(self._state.get_active_services())
        all_stops = self._state.get_all_manual_stops() if hasattr(self._state, 'get_all_manual_stops') else {}
        if not all_stops:
            return
        now = time.time()
        cleaned = []
        for name, stop_ts in all_stops.items():
            if now - stop_ts > self._state.MANUAL_STOP_TTL:
                cleaned.append(name)
        for name in cleaned:
            self._state.clear_manual_stop(name)
        if cleaned:
            log.debug("Cleaned %d expired manual stops: %s", len(cleaned), cleaned)

    def _reconcile(self):
        """Reconcile state against actual running processes."""
        try:
            result = self._mgr.reconcile()
            actions = result.get("actions", [])
            if actions:
                log.info("Reconciled: %s", "; ".join(actions[:5]))
        except Exception as e:
            log.warning("Reconcile failed: %s", e)

    def health_check(self, port: int | None = None) -> dict:
        """Run a single health check and return status."""
        try:
            active = self._state.get_active_services()
            gpu_mode = self._state.gpu_mode
            return {
                "status": "ok",
                "gpu_mode": gpu_mode,
                "active_services": active,
                "services": len(active),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}