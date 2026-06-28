"""
edge_llm/gpu_lock.py — GPU mutual exclusion via flock.

Extracted from profile_manager.py (v3.0 → v3.1 refactoring).

Design: pure flock, no PID in file — flock auto-releases on process death.
"""

import os
import time
import fcntl
import logging
from pathlib import Path
from typing import Optional

from .config import GPU_LOCK_PATH

log = logging.getLogger("edge_llm")


class GPULock:
    """GPU mutual exclusion via flock. No PID in file — flock auto-releases on process death."""

    def __init__(self, lock_path: Path = GPU_LOCK_PATH):
        self._lock_path = str(lock_path)
        self._fd: Optional[int] = None
        self._reentrant_count: int = 0  # support reentrant acquire/release

    def acquire(self, timeout: float = 0) -> bool:
        """Acquire GPU lock. timeout=0 means non-blocking. Returns True if acquired.
        Reentrant: multiple acquire() calls require matching release() calls."""
        if self._fd is not None:
            self._reentrant_count += 1
            return True  # already held (reentrant)
        try:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            if timeout > 0:
                deadline = time.time() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self._fd = fd
                        return True
                    except BlockingIOError:
                        if time.time() >= deadline:
                            os.close(fd)
                            return False
                        time.sleep(1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return True
        except BlockingIOError:
            try:
                os.close(fd)
            except OSError:
                pass
            return False
        except OSError:
            return False

    def release(self):
        """Release GPU lock. Reentrant: only truly releases when all acquire() calls are matched."""
        if self._fd is None:
            return
        if self._reentrant_count > 0:
            self._reentrant_count -= 1
            return  # still held by outer acquire
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
        except OSError:
            pass
        finally:
            self._fd = None

    def force_clear(self):
        """Emergency: close any stale lock. Only use when no other process could hold it."""
        self.release()
        try:
            os.unlink(self._lock_path)
        except FileNotFoundError:
            pass

    @property
    def is_held(self) -> bool:
        return self._fd is not None
