from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import requests

from app.utils.python_path import add_system_dist_packages

add_system_dist_packages()

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - Raspberry Pi runtime dependency
    cv2 = None
    np = None

try:
    from pyzbar.pyzbar import ZBarSymbol, decode as pyzbar_decode
except Exception:  # pragma: no cover - optional Pi dependency
    ZBarSymbol = None
    pyzbar_decode = None

from app.streaming_agent.camera_controls import CameraControlManager
from app.streaming_agent.config_loader import get_device_id
from app.streaming_agent.detection.camera_manager import OpenCVCameraManager, SharedFrameBufferCameraManager
from app.streaming_agent.detection.preprocessing import FrameQualityMetrics, QRPreprocessor
from app.streaming_agent.detection.scanner_config import QRScannerConfig
from app.streaming_agent.gpio.relay_controller import RelayController
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)

LOG_FILE = Path(os.getenv("QR_SCAN_LOG_FILE", "logs/qr_scans.jsonl"))
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_IDENTITY_LOCK = threading.Lock()
_CACHED_QR_DEVICE_ID = None
_CACHED_QR_LOCKER_ID = None


@dataclass(frozen=True)
class QRScanResult:
    raw_value: str
    payload: dict
    debounce_key: str
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    backend_response: Optional[dict] = None
    accepted: Optional[bool] = None
    error: Optional[str] = None


@dataclass
class QRScannerMetrics:
    frames_seen: int = 0
    detection_attempts: int = 0
    detections: int = 0
    duplicate_suppressed: int = 0
    invalid_payloads: int = 0
    backend_success: int = 0
    backend_failure: int = 0
    timeouts: int = 0
    reconnect_attempts: int = 0
    last_detection_ms: float = 0.0
    last_fps: float = 0.0
    last_quality: FrameQualityMetrics = field(default_factory=FrameQualityMetrics.empty)

    def snapshot(self) -> dict:
        return {
            "frames_seen": self.frames_seen,
            "detection_attempts": self.detection_attempts,
            "detections": self.detections,
            "duplicate_suppressed": self.duplicate_suppressed,
            "invalid_payloads": self.invalid_payloads,
            "backend_success": self.backend_success,
            "backend_failure": self.backend_failure,
            "timeouts": self.timeouts,
            "reconnect_attempts": self.reconnect_attempts,
            "last_detection_ms": self.last_detection_ms,
            "last_fps": self.last_fps,
            "last_quality": self.last_quality.as_dict(),
        }


class QrGpioController(RelayController):
    """Backward-compatible name for the centralized relay controller."""

    def __init__(
        self,
        success_pin: int = 20,
        failure_pin: int = 21,
        failure_signal_seconds: float = 15.0,
        active_low: bool = True,
    ):
        super().__init__(
            green_led_pin=success_pin,
            red_led_pin=failure_pin,
            active_low=active_low,
            alert_duration=failure_signal_seconds,
        )


class BackendQRValidator:
    """Backend hook: JWTs are verified remotely; no permanent authorization is stored locally."""

    def __init__(self, config: QRScannerConfig):
        self.config = config

    def __call__(self, payload: dict) -> dict:
        return verify_qr(payload, config=self.config)


class QRScanner:
    """Threaded external-camera QR scanner using OpenCV QRCodeDetector."""

    def __init__(
        self,
        frame_buffer=None,
        *,
        config: QRScannerConfig | None = None,
        video_device: str | None = None,
        gpio_controller: RelayController | None = None,
        camera_controls: CameraControlManager | None = None,
        process_every_n_frames: int | None = None,
        on_qr_detected: Callable[[dict], object] | None = None,
        backend_validator: Callable[[dict], dict] | None = None,
        camera_manager=None,
    ):
        self.config = config or QRScannerConfig.from_env()
        if process_every_n_frames is not None:
            interval = max(self.config.scan_interval_ms, int(process_every_n_frames) * 100)
            self.config = QRScannerConfig(**{**self.config.__dict__, "scan_interval_ms": interval})

        self.frame_buffer = frame_buffer
        self.video_device = video_device
        self.camera_controls = camera_controls or CameraControlManager()
        self.gpio_controller = gpio_controller or RelayController(
            active_low=getattr(self.config, "gpio_active_low", True),
            unlock_seconds=getattr(self.config, "default_unlock_seconds", 5),
            alert_duration=getattr(self.config, "failure_signal_seconds", 15.0),
        )
        self._owns_gpio_controller = gpio_controller is None
        self.on_qr_detected = on_qr_detected
        self.backend_validator = backend_validator or BackendQRValidator(self.config)
        self.camera_manager = camera_manager or self._build_camera_manager(frame_buffer, video_device)
        self.preprocessor = QRPreprocessor(self.config)
        self.metrics = QRScannerMetrics()
        self._detector = None
        self._running = False
        self._thread = None
        self._lock = threading.RLock()
        self._event_condition = threading.Condition(self._lock)
        self._latest_result: QRScanResult | None = None
        self._processing_keys = set()
        self._token_cache = {}
        self._last_sequence = -1
        self._cooldown_until = 0.0
        self._scan_session_started_at = time.monotonic()
        self._last_metrics_log_at = time.monotonic()
        self._last_frame_counter_at = time.monotonic()
        self._frames_since_log = 0
        self._qr_attention_until = 0.0
        self._last_debug_frame_saved_at = 0.0
        self._pyzbar_unavailable_logged = False
        self._opencv_worker_running = False
        self._opencv_worker_lock = threading.Lock()
        self._duplicate_log_last_at = {}

    @property
    def latest_result(self) -> QRScanResult | None:
        with self._lock:
            return self._latest_result

    def metrics_snapshot(self) -> dict:
        with self._lock:
            return self.metrics.snapshot()

    def start(self):
        if self._running:
            logger.info("QR scanner is already running")
            return
        if cv2 is None or np is None:
            logger.warning("QR scanner disabled: OpenCV and NumPy are required")
            return
        if self.camera_manager is None:
            logger.warning("QR scanner disabled: no external frame source is configured")
            return

        self._detector = cv2.QRCodeDetector()
        if hasattr(self._detector, "setEpsX"):
            self._detector.setEpsX(self.config.qr_detector_eps_x)
        if hasattr(self._detector, "setEpsY"):
            self._detector.setEpsY(self.config.qr_detector_eps_y)
        if self.video_device and self.config.autofocus_enabled:
            self.camera_controls.prepare_for_qr_scan(self.video_device, reason="QR scanner startup", force=True)
        if pyzbar_decode is None and getattr(self.config, "pyzbar_enabled", True):
            logger.error(
                "Fast QR scanner is not available because pyzbar/libzbar is missing. "
                "Install with: sudo apt install -y libzbar0 && pip install pyzbar"
            )
        self.camera_manager.start()
        self.gpio_controller.start()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="external-qr-scanner")
        self._thread.start()
        logger.info(
            "QR scanner started: interval=%sms detection_width=%s preprocessing=%s pyzbar=%s opencv_fallback_every=%s cooldown=%.1fs",
            self.config.scan_interval_ms,
            self.config.detection_width,
            self.config.preprocessing_enabled,
            pyzbar_decode is not None and getattr(self.config, "pyzbar_enabled", True),
            getattr(self.config, "opencv_fallback_every_n", 5),
            self.config.cooldown_seconds,
        )

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self.camera_manager:
            self.camera_manager.stop()
        if self._owns_gpio_controller:
            self.gpio_controller.cleanup()
        logger.info("QR scanner stopped")

    def wait_for_scan(self, timeout: float | None = None) -> QRScanResult | None:
        with self._event_condition:
            current = self._latest_result
            if not self._event_condition.wait_for(lambda: self._latest_result is not current, timeout=timeout):
                return None
            return self._latest_result

    def is_qr_attention_active(self):
        with self._lock:
            return time.monotonic() < self._qr_attention_until

    def _build_camera_manager(self, frame_buffer, video_device):
        if frame_buffer is not None:
            return SharedFrameBufferCameraManager(frame_buffer, self.config)
        if video_device:
            return OpenCVCameraManager(video_device, self.config, camera_controls=self.camera_controls)
        return None

    def _run(self):
        next_scan_at = 0.0
        while self._running:
            now = time.monotonic()
            if now < next_scan_at:
                time.sleep(min(0.02, next_scan_at - now))
                continue
            next_scan_at = now + self.config.scan_interval_seconds

            camera_frame = self.camera_manager.latest_frame()
            if camera_frame is None:
                self._handle_scan_timeout()
                continue
            if camera_frame.sequence == self._last_sequence:
                self._handle_scan_timeout()
                continue
            self._last_sequence = camera_frame.sequence

            with self._lock:
                self.metrics.frames_seen += 1
                self._frames_since_log += 1
            try:
                decoded, qr_seen, metrics = self._detect(camera_frame.frame)
                self._log_periodic_metrics(metrics)
                if qr_seen:
                    self._mark_qr_attention()
                if decoded:
                    self._handle_decoded_value(decoded)
            except Exception:
                logger.exception("QR scanner detection loop failed")

    def _detect(self, frame):
        started_at = time.perf_counter()
        if self._detector is None:
            self._detector = cv2.QRCodeDetector()

        metrics = self.preprocessor.quality_metrics(frame)
        with self._lock:
            self.metrics.detection_attempts += 1
            self.metrics.last_quality = metrics

        attempt_index = self.metrics.detection_attempts
        try:
            for candidate in self.preprocessor.candidates(frame, attempt_index=attempt_index):
                decoded = self._detect_with_pyzbar(candidate.image)
                if decoded:
                    with self._lock:
                        self.metrics.detections += 1
                        self.metrics.last_detection_ms = (time.perf_counter() - started_at) * 1000.0
                    self._maybe_save_debug_frame(frame, "decoded")
                    return decoded, True, metrics
        except Exception:
            logger.exception("QR preprocessing failed; skipping frame")

        self._maybe_start_opencv_fallback(frame.copy(), attempt_index)

        with self._lock:
            self.metrics.last_detection_ms = (time.perf_counter() - started_at) * 1000.0
        self._maybe_save_debug_frame(frame, "latest_no_decode")
        return None, False, metrics

    def _maybe_start_opencv_fallback(self, frame, attempt_index: int):
        opencv_every_n = max(1, int(getattr(self.config, "opencv_fallback_every_n", 5)))
        opencv_max_candidates = max(0, int(getattr(self.config, "opencv_max_candidates", 1)))
        if not opencv_max_candidates or attempt_index % opencv_every_n != 0:
            return

        with self._opencv_worker_lock:
            if self._opencv_worker_running:
                return
            self._opencv_worker_running = True

        threading.Thread(
            target=self._opencv_fallback_worker,
            args=(frame, attempt_index, opencv_max_candidates),
            daemon=True,
            name="qr-opencv-fallback",
        ).start()

    def _opencv_fallback_worker(self, frame, attempt_index: int, max_candidates: int):
        started_at = time.perf_counter()
        try:
            detector = cv2.QRCodeDetector()
            if hasattr(detector, "setEpsX"):
                detector.setEpsX(self.config.qr_detector_eps_x)
            if hasattr(detector, "setEpsY"):
                detector.setEpsY(self.config.qr_detector_eps_y)
            for index, candidate in enumerate(self.preprocessor.opencv_candidates(frame, attempt_index=attempt_index)):
                if index >= max_candidates or not self._running:
                    break
                decoded, points = self._detect_with_opencv(candidate.image, detector=detector)
                if points is not None:
                    self._mark_qr_attention()
                if decoded:
                    with self._lock:
                        self.metrics.detections += 1
                        self.metrics.last_detection_ms = (time.perf_counter() - started_at) * 1000.0
                    self._maybe_save_debug_frame(frame, "decoded_opencv")
                    self._handle_decoded_value(decoded)
                    return
        except Exception:
            logger.exception("QR OpenCV fallback worker failed")
        finally:
            with self._opencv_worker_lock:
                self._opencv_worker_running = False

    def _detect_with_opencv(self, image, detector=None):
        detector = detector or self._detector
        try:
            ok, points = detector.detect(image)
        except Exception:
            logger.exception("OpenCV QR detect failed")
            return None, None
        if not ok or points is None:
            return None, None

        try:
            decoded, _straight = detector.decode(image, points)
        except Exception:
            logger.exception("OpenCV QR decode failed")
            return None, points
        decoded = decoded.strip() if decoded else ""
        if decoded:
            return decoded, points

        return None, points

    def _detect_with_pyzbar(self, image):
        if not getattr(self.config, "pyzbar_enabled", True):
            return None
        if pyzbar_decode is None:
            if not self._pyzbar_unavailable_logged:
                self._pyzbar_unavailable_logged = True
                logger.warning(
                    "pyzbar/libzbar is unavailable; install `sudo apt install -y libzbar0` "
                    "and `pip install pyzbar` for fast phone-screen QR decoding."
                )
            return None

        try:
            symbols = [ZBarSymbol.QRCODE] if ZBarSymbol is not None else None
            decoded_items = pyzbar_decode(image, symbols=symbols) if symbols else pyzbar_decode(image)
        except Exception as exc:
            logger.warning("pyzbar QR decode failed: %s", exc)
            return None

        for item in decoded_items:
            value = item.data.decode("utf-8", errors="replace").strip()
            if value:
                return value
        return None

    def _decode_qr(self, frame_bytes):
        """Compatibility helper used by integration tests."""
        expected_size = self.frame_buffer.frame_size if self.frame_buffer else 0
        if not self.frame_buffer or len(frame_bytes) != expected_size:
            if self.frame_buffer:
                logger.error(
                    "QR frame size mismatch: expected=%s actual=%s width=%s height=%s channels=%s",
                    expected_size,
                    len(frame_bytes),
                    self.frame_buffer.width,
                    self.frame_buffer.height,
                    self.frame_buffer.channels,
                )
            return None, False, FrameQualityMetrics.empty().as_dict()
        frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(
            self.frame_buffer.height,
            self.frame_buffer.width,
            self.frame_buffer.channels,
        )
        decoded, qr_seen, metrics = self._detect(frame)
        return decoded, qr_seen, metrics.as_dict()

    def _handle_decoded_value(self, raw_value: str):
        try:
            payload, debounce_key = parse_qr_value(raw_value, require_jwt_shape=self.config.require_jwt_shape)
        except Exception as exc:
            with self._lock:
                self.metrics.invalid_payloads += 1
            logger.warning("Invalid QR payload: %s", exc)
            self.gpio_controller.pulse_failure()
            write_scan_log(raw_value, None, "qr_failure_alert", str(exc))
            return

        if not self._reserve_token(debounce_key):
            return

        result = QRScanResult(raw_value=raw_value, payload=payload, debounce_key=debounce_key)
        with self._event_condition:
            self._latest_result = result
            self._event_condition.notify_all()

        logger.info("QR decoded from external camera: %s", summarize_qr_value(raw_value))
        worker = threading.Thread(
            target=self._process_scan,
            args=(result,),
            daemon=True,
            name="qr-validation",
        )
        worker.start()

    def _reserve_token(self, debounce_key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._expire_token_cache(now)
            if debounce_key in self._processing_keys:
                self.metrics.duplicate_suppressed += 1
                self._log_duplicate_suppressed(debounce_key, reason="backend verification already in progress")
                return False
            if getattr(self.config, "local_duplicate_suppression_enabled", False):
                if now < self._cooldown_until:
                    self.metrics.duplicate_suppressed += 1
                    self._log_duplicate_suppressed(debounce_key, reason="cooldown active")
                    return False
                if debounce_key in self._token_cache:
                    self.metrics.duplicate_suppressed += 1
                    self._log_duplicate_suppressed(debounce_key, reason="local duplicate cache")
                    return False
            self._processing_keys.add(debounce_key)
            if getattr(self.config, "local_duplicate_suppression_enabled", False):
                self._token_cache[debounce_key] = now + self.config.duplicate_cache_seconds
                self._cooldown_until = now + self.config.cooldown_seconds
            self._scan_session_started_at = now
            return True

    def _process_scan(self, result: QRScanResult):
        backend_response = None
        error = None
        try:
            if self.on_qr_detected:
                self.on_qr_detected(result.payload)

            logger.info(
                "QR backend validation started: url=%s token=%s payload_keys=%s",
                self.config.backend_verify_url,
                result.debounce_key,
                sorted(result.payload.keys()),
            )
            backend_response = self.backend_validator(result.payload) if self.backend_validator else None
            logger.info("QR backend validation response: %s", summarize_backend_response(backend_response))
            accepted = should_open_locker(backend_response)
            duration = unlock_duration(backend_response, self.config) if accepted else 0
            if accepted:
                logger.info(
                    "QR backend accepted token=%s; turning on green LED and unlocking locker for %ss",
                    result.debounce_key,
                    duration,
                )
                self.gpio_controller.pulse_success(duration)
                with self._lock:
                    self.metrics.backend_success += 1
                write_scan_log(result.raw_value, backend_response, f"qr_success_unlock_{duration}s")
            else:
                logger.warning(
                    "QR backend denied token=%s; turning on red LED and buzzer for %.1fs",
                    result.debounce_key,
                    getattr(self.gpio_controller, "alert_duration", self.config.failure_signal_seconds),
                )
                self.gpio_controller.pulse_failure()
                with self._lock:
                    self.metrics.backend_failure += 1
                write_scan_log(result.raw_value, backend_response, "qr_failure_alert")
            self._publish_result(result, backend_response=backend_response, accepted=accepted)
        except Exception as exc:
            error = str(exc)
            logger.warning("QR backend validation failed; locker will stay closed: %s", exc)
            self.gpio_controller.pulse_failure()
            with self._lock:
                self.metrics.backend_failure += 1
            write_scan_log(result.raw_value, backend_response, "qr_failure_alert", error)
            self._publish_result(result, backend_response=backend_response, accepted=False, error=error)
        finally:
            with self._lock:
                self._processing_keys.discard(result.debounce_key)

    def _log_duplicate_suppressed(self, debounce_key: str, reason: str):
        now = time.monotonic()
        last_at = self._duplicate_log_last_at.get(debounce_key, 0.0)
        if now - last_at < 3.0:
            return
        self._duplicate_log_last_at[debounce_key] = now
        logger.info(
            "Suppressed QR token %s because %s. "
            "Set QR_LOCAL_DUPLICATE_SUPPRESSION_ENABLED=false to let backend handle repeated tokens.",
            debounce_key,
            reason,
        )

    def _publish_result(self, result, *, backend_response, accepted, error=None):
        updated = QRScanResult(
            raw_value=result.raw_value,
            payload=result.payload,
            debounce_key=result.debounce_key,
            detected_at=result.detected_at,
            backend_response=backend_response,
            accepted=accepted,
            error=error,
        )
        with self._event_condition:
            self._latest_result = updated
            self._event_condition.notify_all()

    def _handle_scan_timeout(self):
        now = time.monotonic()
        if now - self._scan_session_started_at < self.config.scan_timeout_seconds:
            return
        with self._lock:
            self.metrics.timeouts += 1
            self._scan_session_started_at = now
        logger.info("QR scan timeout: no valid QR decoded for %.1fs", self.config.scan_timeout_seconds)

    def _expire_token_cache(self, now: float):
        expired = [token for token, expires_at in self._token_cache.items() if expires_at <= now]
        for token in expired:
            self._token_cache.pop(token, None)

    def _mark_qr_attention(self):
        with self._lock:
            self._qr_attention_until = max(
                self._qr_attention_until,
                time.monotonic() + self.config.attention_hold_seconds,
            )

    def _log_periodic_metrics(self, quality: FrameQualityMetrics):
        now = time.monotonic()
        if now - self._last_metrics_log_at < self.config.metrics_log_interval_seconds:
            return
        elapsed = now - self._last_frame_counter_at
        fps = self._frames_since_log / elapsed if elapsed > 0 else 0.0
        self._last_metrics_log_at = now
        self._last_frame_counter_at = now
        self._frames_since_log = 0
        with self._lock:
            self.metrics.last_fps = fps
            snapshot = self.metrics.snapshot()
        logger.info(
            "QR scanner metrics: fps=%.2f attempts=%s detections=%s duplicates=%s timeout=%s "
            "detect_ms=%.1f brightness=%.1f contrast=%.1f blur=%.1f",
            fps,
            snapshot["detection_attempts"],
            snapshot["detections"],
            snapshot["duplicate_suppressed"],
            snapshot["timeouts"],
            snapshot["last_detection_ms"],
            quality.brightness,
            quality.contrast,
            quality.blur,
        )

    def _maybe_save_debug_frame(self, frame, label):
        if not self.config.debug_preview_enabled or cv2 is None:
            return
        now = time.monotonic()
        if now - self._last_debug_frame_saved_at < self.config.debug_save_interval_seconds:
            return
        self._last_debug_frame_saved_at = now
        try:
            self.config.debug_frame_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            path = self.config.debug_frame_dir / f"{timestamp}_{label}.jpg"
            cv2.imwrite(str(path), frame)
            logger.info("Saved QR debug frame: %s", path)
        except Exception:
            logger.exception("Failed to save QR debug frame")


# Backward-compatible class name used by the existing streaming agent.
QrScanner = QRScanner


def parse_qr_value(raw_value, *, require_jwt_shape: bool = False):
    value = raw_value.strip()
    if not value:
        raise ValueError("QR value is empty")
    locker_id = get_qr_locker_id()
    device_id = get_qr_device_id()

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        if require_jwt_shape and not _JWT_SHAPE.match(value):
            raise ValueError("QR token is not a JWT-shaped temporary token")
        return {
            "token": value,
            "locker_id": locker_id,
            "device_id": device_id,
        }, value

    if not isinstance(parsed, dict):
        raise ValueError("QR JSON must be an object")

    if "qr_payload" in parsed or "qr_data" in parsed:
        payload = dict(parsed)
        payload["locker_id"] = str(payload.get("locker_id") or locker_id)
        payload["device_id"] = str(payload.get("device_id") or device_id)
        debounce_key = _debounce_key_from_payload(payload)
        _validate_token_shape_for_payload(payload, debounce_key, require_jwt_shape)
        return payload, debounce_key

    unique_token = str(parsed.get("unique_token") or parsed.get("token") or "").strip()
    qr_code_id = str(parsed.get("qr_code_id") or "").strip()
    if not unique_token:
        raise ValueError("QR JSON is missing unique_token")
    if require_jwt_shape and not _JWT_SHAPE.match(unique_token):
        raise ValueError("QR unique_token is not JWT-shaped")

    if qr_code_id and _is_minimal_qr_payload(parsed):
        return {
            "qr_code_id": qr_code_id,
            "unique_token": unique_token,
            "locker_id": str(parsed.get("locker_id") or locker_id),
            "device_id": str(parsed.get("device_id") or device_id),
        }, unique_token

    return {
        "qr_payload": parsed,
        "locker_id": locker_id,
        "device_id": device_id,
    }, unique_token


def get_qr_device_id():
    global _CACHED_QR_DEVICE_ID
    if _CACHED_QR_DEVICE_ID:
        return _CACHED_QR_DEVICE_ID

    env_device_id = os.getenv("DEVICE_ID", "").strip()
    if env_device_id and env_device_id != "PI4-001":
        _CACHED_QR_DEVICE_ID = env_device_id
        return env_device_id

    with _IDENTITY_LOCK:
        if _CACHED_QR_DEVICE_ID:
            return _CACHED_QR_DEVICE_ID
        try:
            _CACHED_QR_DEVICE_ID = get_device_id()
        except Exception as exc:
            logger.warning("Could not load QR device_id from backend device config; using fallback: %s", exc)
            _CACHED_QR_DEVICE_ID = env_device_id or "TEWPUH775796"
        return _CACHED_QR_DEVICE_ID


def get_qr_locker_id():
    global _CACHED_QR_LOCKER_ID
    if _CACHED_QR_LOCKER_ID:
        return _CACHED_QR_LOCKER_ID

    env_locker_id = os.getenv("LOCKER_ID", "").strip()
    if env_locker_id:
        _CACHED_QR_LOCKER_ID = env_locker_id
        return env_locker_id

    with _IDENTITY_LOCK:
        if _CACHED_QR_LOCKER_ID:
            return _CACHED_QR_LOCKER_ID
        try:
            _CACHED_QR_LOCKER_ID = get_device_id()
        except Exception as exc:
            logger.warning("Could not load QR locker_id from backend device config; using fallback: %s", exc)
            _CACHED_QR_LOCKER_ID = "TEWPUH775796"
        return _CACHED_QR_LOCKER_ID


def _is_minimal_qr_payload(payload):
    minimal_keys = {"qr_code_id", "unique_token", "token", "locker_id", "device_id"}
    return set(payload.keys()).issubset(minimal_keys)


def _validate_token_shape_for_payload(payload, debounce_key, require_jwt_shape):
    if require_jwt_shape and not _JWT_SHAPE.match(str(debounce_key)):
        raise ValueError("QR payload token is not JWT-shaped")


def _debounce_key_from_payload(payload):
    qr_payload = payload.get("qr_payload")
    if isinstance(qr_payload, dict):
        token = str(qr_payload.get("unique_token") or qr_payload.get("token") or "").strip()
        if token:
            return token
        qr_code_id = str(qr_payload.get("qr_code_id") or "").strip()
        if qr_code_id:
            return qr_code_id

    qr_data = payload.get("qr_data")
    if isinstance(qr_data, str):
        try:
            parsed_data = json.loads(qr_data)
        except json.JSONDecodeError:
            parsed_data = None
        if isinstance(parsed_data, dict):
            token = str(parsed_data.get("unique_token") or parsed_data.get("token") or "").strip()
            if token:
                return token
            qr_code_id = str(parsed_data.get("qr_code_id") or "").strip()
            if qr_code_id:
                return qr_code_id

    token = str(payload.get("unique_token") or payload.get("token") or "").strip()
    if token:
        return token
    qr_code_id = str(payload.get("qr_code_id") or "").strip()
    if qr_code_id:
        return qr_code_id
    return json.dumps(payload, sort_keys=True, default=str)


def summarize_qr_value(raw_value):
    value = raw_value.strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        if len(value) <= 16:
            return value
        return f"token:{value[:6]}...{value[-6:]}"

    if not isinstance(parsed, dict):
        return "non-object-json"
    token = str(parsed.get("unique_token") or parsed.get("token") or "").strip()
    qr_code_id = str(parsed.get("qr_code_id") or "").strip()
    tracking_number = str(parsed.get("tracking_number") or "").strip()
    parts = []
    if qr_code_id:
        parts.append(f"qr_code_id={qr_code_id}")
    if token:
        parts.append(f"token=...{token[-8:]}")
    if tracking_number:
        parts.append(f"tracking_number={tracking_number}")
    return " ".join(parts) or f"json_keys={sorted(parsed.keys())}"


def summarize_backend_response(response):
    if not isinstance(response, dict):
        return type(response).__name__

    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    summary = {
        "success": response.get("success"),
        "can_open_locker": data.get("can_open_locker"),
        "unlock_duration_seconds": data.get("unlock_duration_seconds"),
    }
    message = response.get("message") or response.get("detail") or response.get("error")
    if message:
        summary["message"] = str(message)[:200]
    return summary


def verify_qr(payload, *, config: QRScannerConfig | None = None):
    scanner_config = config or QRScannerConfig.from_env()
    logger.info(
        "Posting QR payload to backend: url=%s timeout=%.1fs payload_keys=%s",
        scanner_config.backend_verify_url,
        scanner_config.backend_timeout_seconds,
        sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
    )
    response = requests.post(
        scanner_config.backend_verify_url,
        json=payload,
        timeout=scanner_config.backend_timeout_seconds,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError:
        logger.warning(
            "QR backend rejected request: status=%s response=%s payload_keys=%s",
            response.status_code,
            response.text[:500],
            sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
        )
        raise
    return response.json()


def should_open_locker(response):
    data = response.get("data") if isinstance(response, dict) else None
    return bool(response.get("success") is True and isinstance(data, dict) and data.get("can_open_locker") is True)


def unlock_duration(response, config: QRScannerConfig | None = None):
    scanner_config = config or QRScannerConfig.from_env()
    data = response.get("data") if isinstance(response, dict) else {}
    try:
        duration = int(data.get("unlock_duration_seconds", scanner_config.default_unlock_seconds))
    except (TypeError, ValueError):
        duration = scanner_config.default_unlock_seconds
    return max(1, duration)


def write_scan_log(decoded_value, backend_response, gpio_action, error=None):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "decoded_qr_value": decoded_value,
        "backend_response": backend_response,
        "gpio_action_taken": gpio_action,
    }
    if error:
        entry["error"] = error

    with LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
