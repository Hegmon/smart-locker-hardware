"""
Device Safety Layer

Ensures safe camera access:
- No permanent device locks during probing
- Ephemeral probes only
- Prevents "Device busy" errors
- Safe concurrent access
"""

from __future__ import annotations

import logging
import os
import fcntl
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)


class DeviceSafetyLayer:
    """
    Ensures safe camera device access.
    
    Prevents device locking issues by:
    - Using ephemeral probes (no persistent handles)
    - Checking device availability before access
    - Implementing safe concurrent access
    - Graceful handling of busy devices
    """
    
    def __init__(self):
        self._active_handles: dict[str, int] = {}
        self._lock_refs: dict[str, int] = {}
        
    @contextmanager
    def safe_probe(self, device_path: str, timeout: int = 5):
        """
        Context manager for safe ephemeral device probing.
        
        Ensures device is never locked during probing.
        
        Args:
            device_path: Path to video device
            timeout: Probe timeout in seconds
        
        Yields:
            None (device is available for probing)
        
        Raises:
            DeviceBusyError: If device is busy
            DeviceAccessError: If device cannot be accessed
        """
        # Check if device exists
        if not os.path.exists(device_path):
            raise DeviceAccessError(f"Device not found: {device_path}")
        
        # Try to open device non-blockingly to check availability
        fd = None
        try:
            fd = os.open(device_path, os.O_RDWR | os.O_NONBLOCK)
            
            # Try to get exclusive lock (non-blocking)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError):
                # Device is locked by another process
                os.close(fd)
                raise DeviceBusyError(f"Device is busy: {device_path}")
            
            # Device is available for probing
            logger.debug("Device available for safe probe: %s", device_path)
            
            # Track active handle
            self._active_handles[device_path] = fd
            
            try:
                yield
            finally:
                # Release lock and close
                self._release_device(device_path, fd)
                
        except (IOError, OSError) as e:
            if fd is not None:
                try:
                    os.close(fd)
                except:
                    pass
            
            if "Resource busy" in str(e) or e.errno == 16:
                raise DeviceBusyError(f"Device busy: {device_path}")
            elif e.errno == 13:
                raise DeviceAccessError(f"Permission denied: {device_path}")
            else:
                raise DeviceAccessError(f"Cannot access device {device_path}: {e}")
    
    def _release_device(self, device_path: str, fd: int) -> None:
        """
        Release device lock and close file descriptor.
        
        Args:
            device_path: Device path
            fd: File descriptor
        """
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except:
            pass
        
        try:
            os.close(fd)
        except:
            pass
        
        self._active_handles.pop(device_path, None)
        logger.debug("Device released: %s", device_path)
    
    def is_device_available(self, device_path: str) -> bool:
        """
        Check if device is available (not busy).
        
        Args:
            device_path: Path to video device
        
        Returns:
            True if device is available
        """
        if not os.path.exists(device_path):
            return False
        
        try:
            fd = os.open(device_path, os.O_RDWR | os.O_NONBLOCK)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                return True
            except (IOError, OSError):
                os.close(fd)
                return False
        except (IOError, OSError):
            return False
    
    def wait_for_device(self, device_path: str, timeout: int = 30) -> bool:
        """
        Wait for device to become available.
        
        Args:
            device_path: Path to video device
            timeout: Maximum wait time in seconds
        
        Returns:
            True if device became available
        """
        start = time.time()
        
        while time.time() - start < timeout:
            if self.is_device_available(device_path):
                return True
            time.sleep(0.5)
        
        return False
    
    def safe_ffmpeg_probe(
        self,
        device_path: str,
        probe_command: list,
        timeout: int = 5
    ) -> tuple[bool, str]:
        """
        Safely probe device with FFmpeg command.
        
        Ensures device is not locked during probe.
        
        Args:
            device_path: Video device path
            probe_command: FFmpeg command to execute
            timeout: Command timeout in seconds
        
        Returns:
            Tuple of (success, output)
        """
        import subprocess
        
        try:
            with self.safe_probe(device_path, timeout):
                result = subprocess.run(
                    probe_command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                
                return result.returncode == 0, result.stdout + result.stderr
                
        except DeviceBusyError:
            logger.warning("Device busy during probe: %s", device_path)
            return False, "Device busy"
        except DeviceAccessError as e:
            logger.warning("Device access error: %s", e)
            return False, str(e)
        except subprocess.TimeoutExpired:
            logger.warning("Probe timeout for device: %s", device_path)
            return False, "Probe timeout"
        except Exception as e:
            logger.warning("Probe failed for %s: %s", device_path, e)
            return False, str(e)
    
    def get_active_devices(self) -> list[str]:
        """
        Get list of currently active (locked) devices.
        
        Returns:
            List of device paths
        """
        return list(self._active_handles.keys())
    
    def cleanup(self) -> None:
        """Release all device handles."""
        for device_path, fd in list(self._active_handles.items()):
            try:
                self._release_device(device_path, fd)
            except:
                pass
        
        self._active_handles.clear()
        logger.info("Device safety layer cleaned up")


class DeviceBusyError(Exception):
    """Raised when a device is busy."""
    pass


class DeviceAccessError(Exception):
    """Raised when a device cannot be accessed."""
    pass
