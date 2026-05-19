from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - Raspberry Pi runtime dependency
    cv2 = None
    np = None

from app.streaming_agent.camera_controls import CameraControlManager
from app.streaming_agent.detection.scanner_config import QRScannerConfig
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


@dataclass(frozen=True)
class CameraFrame:
    frame: object
    sequence: int
    captured_at: float


class SharedFrameBufferCameraManager:
    """Read latest frames produced by the external ffmpeg livestream process."""

    def __init__(self, frame_buffer, config: QRScannerConfig):
        self.frame_buffer = frame_buffer
        self.config = config
        self._last_sequence = -1
        self._last_frame_at = 0.0
        self._last_watchdog_log_at = 0.0

    def start(self) -> bool:
        return self.frame_buffer is not None

    def stop(self) -> None:
        return None

    def reconnect(self) -> bool:
        logger.warning("QR frame source is the livestream frame buffer; reconnect is delegated to StreamWatchdog")
        return False

    def latest_frame(self) -> Optional[CameraFrame]:
        if self.frame_buffer is None or np is None:
            return None

        frame_bytes, sequence, updated_at = self.frame_buffer.latest()
        if frame_bytes is None or sequence == self._last_sequence:
            self._maybe_watchdog_log(sequence)
            return None

        expected_size = self.frame_buffer.frame_size
        if len(frame_bytes) != expected_size:
            logger.error(
                "QR frame size mismatch: expected=%s actual=%s width=%s height=%s channels=%s",
                expected_size,
                len(frame_bytes),
                self.frame_buffer.width,
                self.frame_buffer.height,
                self.frame_buffer.channels,
            )
            return None

        self._last_sequence = sequence
        self._last_frame_at = time.monotonic()
        frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(
            self.frame_buffer.height,
            self.frame_buffer.width,
            self.frame_buffer.channels,
        )
        return CameraFrame(frame=frame, sequence=sequence, captured_at=updated_at)

    def _maybe_watchdog_log(self, sequence: int) -> None:
        now = time.monotonic()
        if self._last_frame_at and now - self._last_frame_at < self.config.camera_watchdog_seconds:
            return
        if now - self._last_watchdog_log_at < self.config.camera_watchdog_seconds:
            return
        self._last_watchdog_log_at = now
        if sequence >= 0:
            logger.warning(
                "QR scanner has not received a fresh external frame for %.1fs",
                self.config.camera_watchdog_seconds,
            )
        else:
            logger.warning("QR scanner is waiting for the first external camera frame")


class OpenCVCameraManager:
    """Standalone low-latency capture fallback for deployments without ffmpeg frame split."""

    def __init__(
        self,
        video_device: str,
        config: QRScannerConfig,
        *,
        camera_controls: CameraControlManager | None = None,
    ):
        self.video_device = video_device
        self.config = config
        self.camera_controls = camera_controls or CameraControlManager()
        self._capture = None
        self._running = False
        self._lock = threading.Lock()
        self._latest = None
        self._sequence = 0
        self._thread = None
        self._last_reconnect_at = 0.0

    def start(self) -> bool:
        if cv2 is None:
            logger.warning("OpenCV unavailable; direct QR camera capture disabled")
            return False
        self._running = True
        opened = self._open_capture()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True, name="qr-camera-capture")
        self._thread.start()
        return opened

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self._release_capture()

    def reconnect(self) -> bool:
        now = time.monotonic()
        if now - self._last_reconnect_at < self.config.camera_reconnect_backoff_seconds:
            return False
        self._last_reconnect_at = now
        logger.warning("Reconnecting QR camera capture on %s", self.video_device)
        self._release_capture()
        return self._open_capture()

    def latest_frame(self) -> Optional[CameraFrame]:
        with self._lock:
            return self._latest

    def _open_capture(self) -> bool:
        self._release_capture()
        capture = cv2.VideoCapture(self.video_device, cv2.CAP_V4L2)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.camera_resolution[0])
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.camera_resolution[1])
        capture.set(cv2.CAP_PROP_FPS, max(5, int(1000 / self.config.scan_interval_ms)))
        if self.config.autofocus_enabled:
            self.camera_controls.prepare_for_qr_scan(self.video_device, reason="direct QR camera startup", force=True)
        self._capture = capture
        opened = capture.isOpened()
        if not opened:
            logger.error("Failed to open QR camera capture on %s", self.video_device)
        return opened

    def _release_capture(self) -> None:
        if self._capture is not None:
            try:
                self._capture.release()
            except Exception:
                logger.exception("Failed to release QR camera capture")
            finally:
                self._capture = None

    def _capture_loop(self) -> None:
        while self._running:
            if self._capture is None or not self._capture.isOpened():
                self.reconnect()
                time.sleep(self.config.camera_reconnect_backoff_seconds)
                continue

            ok, frame = self._capture.read()
            if not ok or frame is None:
                self.reconnect()
                time.sleep(0.05)
                continue

            with self._lock:
                self._sequence += 1
                self._latest = CameraFrame(frame=frame, sequence=self._sequence, captured_at=time.monotonic())
