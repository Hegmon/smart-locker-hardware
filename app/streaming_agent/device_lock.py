"""
Device Lock Manager

Provides a process-wide lock registry to ensure a single pipeline owns
each physical camera device. Locks are reentrant per owner string.
"""
from __future__ import annotations
import threading
from typing import Optional, Dict


class DeviceLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.owner: Optional[str] = None

    def acquire(self, owner: str, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        acquired = self._lock.acquire(blocking, timeout) if timeout is not None else self._lock.acquire(blocking)
        if acquired:
            self.owner = owner
        return acquired

    def release(self, owner: str) -> None:
        if self.owner != owner:
            # Only the owner may release; ignore otherwise
            return
        try:
            self._lock.release()
        finally:
            # Clear owner when fully released
            if not self._lock._is_owned():
                self.owner = None


class DeviceLockManager:
    """Singleton-like manager for device locks."""

    def __init__(self) -> None:
        self._locks: Dict[str, DeviceLock] = {}
        self._global = threading.RLock()

    def _get_lock(self, device: str) -> DeviceLock:
        with self._global:
            if device not in self._locks:
                self._locks[device] = DeviceLock()
            return self._locks[device]

    def acquire(self, device: str, owner: str, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        lock = self._get_lock(device)
        return lock.acquire(owner, blocking=blocking, timeout=timeout)

    def release(self, device: str, owner: str) -> None:
        lock = self._get_lock(device)
        lock.release(owner)

    def is_locked(self, device: str) -> bool:
        lock = self._get_lock(device)
        return lock.owner is not None


# Module-level manager instance
manager = DeviceLockManager()
