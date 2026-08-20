"""inferfabric/config_reloader.py — Unified config hot-reload (v5.2).

Replaces: SIGHUP handler in handler.py + config_watcher.detect_drift().
Single entry point for model/auth/cloud/dashboard config reload.
"""

import logging
import signal
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inferfabric.manager import ModelManager
    from inferfabric.proxy.auth import AuthManager
    from inferfabric.cloud_discovery import CloudDiscovery
    from inferfabric.dashboard import invalidate_cache

log = logging.getLogger("inferfabric.config_reloader")


class ConfigReloader:
    """Unified config hot-reload with signal handling (v5.2).

    Usage:
        reloader = ConfigReloader(mgr, auth, cloud)
        reloader.setup()
    """

    def __init__(self, mgr: "ModelManager", auth: "AuthManager" = None,
                 cloud: "CloudDiscovery" = None):
        self._mgr = mgr
        self._auth = auth
        self._cloud = cloud
        self._last_reload = 0.0
        self._cooldown = 5.0  # seconds

    def setup(self):
        """Register SIGHUP / SIGUSR1 handlers."""
        try:
            signal.signal(signal.SIGHUP, self._on_signal)
            signal.signal(signal.SIGUSR1, self._on_signal)
            log.info("ConfigReloader: SIGHUP/SIGUSR1 handlers registered")
        except ValueError:
            # Not in main thread — skip signal registration
            pass

    def _on_signal(self, signum, frame):
        if signum == signal.SIGHUP:
            self.reload_all()
        elif signum == signal.SIGUSR1:
            self.reload_models()

    def reload_all(self):
        """Full reload: models + auth + cloud + dashboard cache."""
        now = time.time()
        if now - self._last_reload < self._cooldown:
            return
        self._last_reload = now
        try:
            self._mgr.reload_models()
            log.info("ConfigReloader: models reloaded")
        except Exception as e:
            log.error("ConfigReloader: model reload failed: %s", e)
        if self._auth:
            try:
                self._auth.reload()
            except Exception as e:
                log.error("ConfigReloader: auth reload failed: %s", e)
        if self._cloud:
            try:
                self._cloud.reload()
            except Exception as e:
                log.error("ConfigReloader: cloud reload failed: %s", e)
        # Invalidate dashboard cache
        try:
            from inferfabric.dashboard import invalidate_cache
            invalidate_cache()
        except Exception:
            log.warning("Dashboard cache invalidation failed")
        log.info("ConfigReloader: full reload complete")

    def reload_models(self):
        """Reload models only (SIGUSR1)."""
        try:
            self._mgr.reload_models()
            log.info("ConfigReloader: models reloaded (SIGUSR1)")
            from inferfabric.dashboard import invalidate_cache
            invalidate_cache()
        except Exception as e:
            log.error("ConfigReloader: model reload failed: %s", e)