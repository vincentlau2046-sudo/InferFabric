"""
inferfabric/watchdog.py — Runtime health watchdog for active models.

Periodically checks /health endpoints of active models.
Consecutive failures trigger alerts and optional auto-restart.
"""

import threading
import time
import logging
from typing import Optional

log = logging.getLogger("inferfabric")


class ModelWatchdog:
    """Background thread that monitors active model health.

    Config:
      - check_interval: seconds between health checks (default 30)
      - fail_threshold_alert: consecutive failures before alert (default 3)
      - fail_threshold_restart: consecutive failures before auto-restart (default 5)
      - auto_restart: whether to attempt auto-restart on persistent failure (default True)
    """

    def __init__(
        self,
        manager,  # ModelManager instance
        check_interval: float = 30.0,
        fail_threshold_alert: int = 3,
        fail_threshold_restart: int = 5,
        auto_restart: bool = True,
    ):
        self._manager = manager
        self._check_interval = check_interval
        self._fail_threshold_alert = fail_threshold_alert
        self._fail_threshold_restart = fail_threshold_restart
        self._auto_restart = auto_restart
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._fail_counts: dict[str, int] = {}  # model_name → consecutive_failures
        self._restarting: set[str] = set()  # models currently being restarted
        self._lock = threading.Lock()

    def start(self):
        """Start the watchdog background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="iff-watchdog")
        self._thread.start()
        log.info("Watchdog started (interval=%.0fs, alert=%d, restart=%d)",
                 self._check_interval, self._fail_threshold_alert, self._fail_threshold_restart)

    def stop(self):
        """Stop the watchdog background thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("Watchdog stopped")

    def _run(self):
        """Main watchdog loop."""
        while not self._stop_event.wait(self._check_interval):
            try:
                self._check_all()
            except Exception as e:
                log.error("Watchdog check error: %s", e)

    def _check_all(self):
        """Check health of all active models."""
        from .health import check_http_status
        from .state import ProfileState

        active = list(self._manager.active_services)
        if not active:
            return

        for svc_name in active:
            model = self._manager.get_model(svc_name)
            if not model or not model.port:
                continue

            with self._lock:
                # Skip if a restart is already in progress for this model
                # P2-2: Add timeout — if a model stays in _restarting > 120s,
                # treat as stuck and allow retry.
                if svc_name in self._restarting:
                    _restart_deadline = getattr(self, "_restart_started", {}).get(svc_name, 0)
                    if _restart_deadline and (time.time() - _restart_deadline < 120):
                        continue
                    log.warning("Watchdog: restart of %s appears stuck (>120s), retrying", svc_name)
                    self._restarting.discard(svc_name)

            health_url = f"http://localhost:{model.port}/health"
            status = check_http_status(health_url)

            with self._lock:
                if status == "✅":
                    self._fail_counts[svc_name] = 0
                elif status in ("⏳", "❌"):
                    self._fail_counts[svc_name] = self._fail_counts.get(svc_name, 0) + 1
                    count = self._fail_counts[svc_name]

                    if count >= self._fail_threshold_restart and self._auto_restart:
                        log.warning("Watchdog: %s failed %d times — auto-restarting", svc_name, count)
                        self._restarting.add(svc_name)
                        # Don't clear fail_counts yet — only on successful restart
                        # Trigger restart in a separate thread to avoid blocking watchdog
                        threading.Thread(
                            target=self._restart_model,
                            args=(svc_name,),
                            daemon=True,
                            name=f"iff-watchdog-restart-{svc_name}",
                        ).start()
                    elif count >= self._fail_threshold_alert:
                        log.warning("Watchdog: %s failed %d times — alerting (profile_state=ERROR)", svc_name, count)
                        self._manager.state.set("profile_state", ProfileState.ERROR)

    def _restart_model(self, name: str):
        """Attempt to restart a failed model.

        P2-2: When invoked from a restart-triggered context, "already_active"
        from switch() is NOT a success — it means reconcile() failed to clean
        up a stale entry, or the process was still registered but non-responsive.
        Only "switched" counts as a successful restart.
        """
        try:
            with self._lock:
                if not hasattr(self, "_restart_started"):
                    self._restart_started = {}
                self._restart_started[name] = time.time()
            log.info("Watchdog: reconciling before restart of %s", name)
            self._manager.reconcile()  # Clean up dead service entries
            if name not in self._manager.active_services:
                # Reconcile removed it — now switch will actually deploy
                log.info("Watchdog: deploying %s after reconcile", name)
                result = self._manager.switch(name)
                if result.get("status") == "switched":
                    log.info("Watchdog: %s restart succeeded", name)
                    with self._lock:
                        self._fail_counts[name] = 0
                else:
                    # P2-2: "already_active" in restart context = stale entry.
                    # Do not clear fail count — let the next cycle retry.
                    if result.get("status") == "already_active":
                        log.warning(
                            "Watchdog: %s returned already_active after reconcile — "
                            "stale entry, will force-clean and retry", name
                        )
                        # Stop the process first to avoid orphan / port conflict,
                        # then remove the stale active_services entry.
                        try:
                            self._manager.stop_service(name)
                        except Exception:
                            pass
                        self._manager.state.remove_active_service(name)
                    else:
                        log.error("Watchdog: %s restart failed: %s", name, result)
            else:
                # P2-2: Still active after reconcile — could be a zombie.
                # Stop the process first to avoid orphan, then force-clean state.
                log.warning(
                    "Watchdog: %s still in active_services after reconcile — "
                    "force-stopping and retrying in next cycle", name
                )
                try:
                    self._manager.stop_service(name)
                except Exception:
                    pass
                self._manager.state.remove_active_service(name)
        except Exception as e:
            log.error("Watchdog: %s restart exception: %s", name, e)
        finally:
            with self._lock:
                self._restarting.discard(name)

    @property
    def fail_counts(self) -> dict[str, int]:
        """Current failure counts (for dashboard/status)."""
        with self._lock:
            return dict(self._fail_counts)

    @property
    def running(self) -> bool:
        """Whether the watchdog thread is currently running."""
        return self._thread is not None and self._thread.is_alive()
