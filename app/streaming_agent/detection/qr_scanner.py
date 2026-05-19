import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None

try:
    from pyzbar.pyzbar import ZBarSymbol, decode as pyzbar_decode
except Exception:
    ZBarSymbol = None
    pyzbar_decode = None

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager
from app.streaming_agent.camera_controls import CameraControlManager
from app.streaming_agent.config_loader import get_device_id


logger = LoggingManager.get_logger(__name__)

VERIFY_URL = os.getenv("BACKEND_QR_VERIFY_URL", "https://backend.qbox.sa/shipments/qr/verify/")
LOG_FILE = Path(os.getenv("QR_SCAN_LOG_FILE", "logs/qr_scans.jsonl"))

SUCCESS_GPIO_PIN = int(os.getenv("QR_SUCCESS_GPIO_PIN", "15"))
FAILURE_GPIO_PIN = int(os.getenv("QR_FAILURE_GPIO_PIN", "14"))
DEFAULT_UNLOCK_SECONDS = int(os.getenv("QR_DEFAULT_UNLOCK_SECONDS", "5"))
FAILURE_SIGNAL_SECONDS = float(os.getenv("QR_FAILURE_SIGNAL_SECONDS", "2"))
DEBOUNCE_SECONDS = float(os.getenv("QR_DEBOUNCE_SECONDS", "5"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("QR_VERIFY_TIMEOUT_SECONDS", "10"))
PROCESS_EVERY_N_FRAMES = int(os.getenv("QR_PROCESS_EVERY_N_FRAMES", "1"))
NO_FRAME_LOG_SECONDS = float(os.getenv("QR_NO_FRAME_LOG_SECONDS", "5"))
QR_SCAN_DEBUG = os.getenv("QR_SCAN_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}
QR_SHARPEN_ENABLED = os.getenv("QR_SHARPEN_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
QR_FOCUS_RETRY_SECONDS = float(os.getenv("QR_FOCUS_RETRY_SECONDS", "5"))
QR_STATUS_LOG_SECONDS = float(os.getenv("QR_STATUS_LOG_SECONDS", "3"))
QR_SAVE_DEBUG_FRAMES = os.getenv("QR_SAVE_DEBUG_FRAMES", "false").strip().lower() in {"1", "true", "yes", "on"}
QR_DEBUG_FRAME_DIR = Path(os.getenv("QR_DEBUG_FRAME_DIR", "logs/qr_debug_frames"))
QR_DEBUG_SAVE_INTERVAL_SECONDS = float(os.getenv("QR_DEBUG_SAVE_INTERVAL_SECONDS", "5"))
QR_DECODE_MODE = os.getenv("QR_DECODE_MODE", "fast").strip().lower()
QR_UPSCALE_EVERY_N_FRAMES = int(os.getenv("QR_UPSCALE_EVERY_N_FRAMES", "6"))
QR_PYZBAR_EVERY_N_FRAMES = max(1, int(os.getenv("QR_PYZBAR_EVERY_N_FRAMES", "3")))
QR_ATTENTION_HOLD_SECONDS = float(os.getenv("QR_ATTENTION_HOLD_SECONDS", "2.5"))
QR_DETECT_WIDTH = max(240, int(os.getenv("QR_DETECT_WIDTH", "320")))
QR_ACTIVE_ROI_MARGIN = float(os.getenv("QR_ACTIVE_ROI_MARGIN", "0.6"))
QR_PYZBAR_FRAME_WIDTH = max(320, int(os.getenv("QR_PYZBAR_FRAME_WIDTH", "480")))
QR_OPENCV_DETECT_EVERY_N_FRAMES = max(1, int(os.getenv("QR_OPENCV_DETECT_EVERY_N_FRAMES", "8")))
QR_METRICS_WIDTH = max(160, int(os.getenv("QR_METRICS_WIDTH", "320")))
QR_REPEAT_SUPPRESS_SECONDS = float(os.getenv("QR_REPEAT_SUPPRESS_SECONDS", "1.0"))
_IDENTITY_LOCK = threading.Lock()
_CACHED_QR_DEVICE_ID = None
_CACHED_QR_LOCKER_ID = None


class QrGpioController:
    """Independent BCM GPIO control for QR verification result pins."""

    def __init__(self, success_pin=SUCCESS_GPIO_PIN, failure_pin=FAILURE_GPIO_PIN):
        self.success_pin = success_pin
        self.failure_pin = failure_pin
        self._gpio = None
        self._enabled = False
        self._lock = threading.Lock()

    def start(self):
        if self._enabled:
            return
        try:
            import RPi.GPIO as GPIO
        except Exception as exc:
            logger.warning("RPi.GPIO unavailable; QR GPIO actions disabled: %s", exc)
            return

        self._gpio = GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self.success_pin, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(self.failure_pin, GPIO.OUT, initial=GPIO.LOW)
        self._enabled = True
        logger.info("QR GPIO initialized on BCM pins success=%s failure=%s", self.success_pin, self.failure_pin)

    def pulse_success(self, duration_seconds):
        self._pulse(self.success_pin, duration_seconds, "success/open")

    def pulse_failure(self):
        self._pulse(self.failure_pin, FAILURE_SIGNAL_SECONDS, "failure/deny")

    def cleanup(self):
        if not self._enabled or self._gpio is None:
            return
        with self._lock:
            try:
                self._gpio.output(self.success_pin, self._gpio.LOW)
                self._gpio.output(self.failure_pin, self._gpio.LOW)
                self._gpio.cleanup((self.success_pin, self.failure_pin))
            except Exception:
                logger.exception("QR GPIO cleanup failed")
            finally:
                self._enabled = False
                self._gpio = None

    def _pulse(self, pin, duration_seconds, action_name):
        if not self._enabled or self._gpio is None:
            logger.info("QR GPIO dry-run: %s pin=%s duration=%ss", action_name, pin, duration_seconds)
            time.sleep(duration_seconds)
            return

        with self._lock:
            logger.info("QR GPIO ON: %s pin=%s duration=%ss", action_name, pin, duration_seconds)
            self._gpio.output(pin, self._gpio.HIGH)
            try:
                time.sleep(duration_seconds)
            finally:
                self._gpio.output(pin, self._gpio.LOW)
                logger.info("QR GPIO OFF: %s pin=%s", action_name, pin)


class QrScanner:
    """Scan QR codes from the external camera shared frame buffer."""

    def __init__(
        self,
        frame_buffer,
        *,
        video_device=None,
        gpio_controller=None,
        camera_controls=None,
        process_every_n_frames=PROCESS_EVERY_N_FRAMES,
    ):
        self.frame_buffer = frame_buffer
        self.video_device = video_device
        self.gpio_controller = gpio_controller or QrGpioController()
        self.camera_controls = camera_controls or CameraControlManager()
        self.process_every_n_frames = max(1, int(process_every_n_frames))
        self._owns_gpio_controller = gpio_controller is None
        self._running = False
        self._thread = None
        self._detector = None
        self._last_sequence = -1
        self._last_seen = {}
        self._processed_frames = 0
        self._fps_window_started_at = time.monotonic()
        self._last_no_frame_log_at = 0.0
        self._last_focus_retry_at = 0.0
        self._last_status_log_at = 0.0
        self._saw_first_frame = False
        self._last_debug_frame_saved_at = 0.0
        self._pyzbar_unavailable_logged = False
        self._last_pattern_decode_failed_log_at = 0.0
        self._scan_lock = threading.Lock()
        self._processing_keys = set()
        self._decode_attempts = 0
        self._qr_attention_until = 0.0
        self._qr_attention_lock = threading.Lock()
        self._focus_worker_running = False
        self._focus_worker_lock = threading.Lock()
        self._active_qr_rect = None
        self._last_decoded_key = None
        self._last_decoded_at = 0.0

    def start(self):
        if self._running:
            logger.info("QR scanner is already running")
            return
        if self.frame_buffer is None:
            logger.warning("QR scanner disabled: external camera frame buffer is not available")
            return
        if cv2 is None or np is None:
            logger.warning("QR scanner disabled: OpenCV and NumPy are required")
            return

        self._detector = cv2.QRCodeDetector()
        if hasattr(self._detector, "setEpsX"):
            self._detector.setEpsX(float(os.getenv("QR_DETECTOR_EPS_X", "0.4")))
        if hasattr(self._detector, "setEpsY"):
            self._detector.setEpsY(float(os.getenv("QR_DETECTOR_EPS_Y", "0.4")))
        if self.video_device:
            self.camera_controls.prepare_for_qr_scan(self.video_device, reason="QR scanner startup", force=True)
        self.gpio_controller.start()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="external-qr-scanner")
        self._thread.start()
        logger.info(
            "QR scanner started on external camera frame buffer %sx%s",
            self.frame_buffer.width,
            self.frame_buffer.height,
        )
        if pyzbar_decode is None:
            logger.warning(
                "pyzbar QR fallback is unavailable. Install `pyzbar` and system package `libzbar0` "
                "for stronger phone-screen QR decoding."
            )

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self._owns_gpio_controller:
            self.gpio_controller.cleanup()
        logger.info("QR scanner stopped")

    def _run(self):
        while self._running:
            frame_bytes, sequence, _ = self.frame_buffer.latest()
            if frame_bytes is None or sequence == self._last_sequence:
                self._maybe_log_no_frames(sequence)
                time.sleep(0.02)
                continue

            self._last_sequence = sequence
            if sequence % self.process_every_n_frames != 0:
                continue

            try:
                if not self._saw_first_frame:
                    self._saw_first_frame = True
                    logger.info(
                        "QR scanner received first external camera frame: sequence=%s size=%sx%s bytes=%s",
                        sequence,
                        self.frame_buffer.width,
                        self.frame_buffer.height,
                        len(frame_bytes),
                    )
                qr_value, qr_seen, metrics = self._decode_qr(frame_bytes)
                if qr_value:
                    self._mark_qr_attention()
                    self._queue_scan(qr_value)
                elif qr_seen and self.video_device:
                    self._maybe_retry_focus()
                self._log_scan_status(qr_seen, bool(qr_value), metrics)
                self._log_fps()
            except Exception:
                logger.exception("QR scanner failed")

    def _decode_qr(self, frame_bytes):
        self._decode_attempts += 1
        expected_size = self.frame_buffer.frame_size
        actual_size = len(frame_bytes)
        if actual_size != expected_size:
            logger.error(
                "QR frame size mismatch: expected=%s actual=%s width=%s height=%s channels=%s. "
                "Check QR_FRAME_WIDTH/QR_FRAME_HEIGHT/QR_FRAME_CHANNELS and ffmpeg raw pipe scale/pad settings.",
                expected_size,
                actual_size,
                self.frame_buffer.width,
                self.frame_buffer.height,
                self.frame_buffer.channels,
            )
            return None, False, self._empty_metrics()

        frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(
            self.frame_buffer.height,
            self.frame_buffer.width,
            self.frame_buffer.channels,
        )
        metrics = self._frame_metrics(frame)
        self._maybe_save_debug_frame(frame, "latest")
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        decoded = self._decode_phone_screen_qr(frame, gray)
        if decoded:
            return decoded, True, metrics

        decoded, qr_seen = self._scan_active_qr_region(frame, gray)
        if decoded:
            return decoded, True, metrics
        if qr_seen:
            return None, True, metrics

        if self._decode_attempts % QR_OPENCV_DETECT_EVERY_N_FRAMES != 0:
            if QR_SCAN_DEBUG:
                logger.info("No QR pattern detected in external camera frame")
            return None, False, metrics

        points = self._detect_qr_points(gray)
        if points is None:
            if QR_SCAN_DEBUG:
                logger.info("No QR pattern detected in external camera frame")
            return None, False, metrics

        self._mark_qr_attention()
        self._active_qr_rect = self._rect_from_points(points, frame.shape, margin=QR_ACTIVE_ROI_MARGIN)

        decoded = self._decode_detected_qr(frame, gray, points)
        if decoded:
            return decoded, True, metrics

        now = time.monotonic()
        if now - self._last_pattern_decode_failed_log_at >= QR_STATUS_LOG_SECONDS:
            self._last_pattern_decode_failed_log_at = now
            logger.warning("QR pattern detected on external camera but decode failed")
        self._maybe_save_debug_frame(frame, "pattern_seen_decode_failed")
        return None, True, metrics

    def _empty_metrics(self):
        return {
            "brightness": 0.0,
            "contrast": 0.0,
            "blur": 0.0,
        }

    def _frame_metrics(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if gray.shape[1] > QR_METRICS_WIDTH:
            scale = QR_METRICS_WIDTH / float(gray.shape[1])
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        return {
            "brightness": float(np.mean(gray)),
            "contrast": float(np.std(gray)),
            "blur": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        }

    def _frame_candidates(self, frame, *, gray=None, qr_seen=False):
        if gray is None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        should_try_expensive = (
            qr_seen
            or QR_DECODE_MODE == "thorough"
            or self._decode_attempts % QR_UPSCALE_EVERY_N_FRAMES == 0
        )
        if not should_try_expensive:
            return

        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        yield "clahe", clahe

        if QR_DECODE_MODE == "thorough" or self._decode_attempts % QR_UPSCALE_EVERY_N_FRAMES == 0:
            downscaled = cv2.resize(frame, None, fx=0.75, fy=0.75, interpolation=cv2.INTER_AREA)
            yield "downscaled_0_75", downscaled
            upscaled = cv2.resize(frame, None, fx=1.35, fy=1.35, interpolation=cv2.INTER_CUBIC)
            yield "upscaled", upscaled

        if QR_SHARPEN_ENABLED:
            blurred = cv2.GaussianBlur(gray, (0, 0), 1.0)
            sharpened = cv2.addWeighted(gray, 1.7, blurred, -0.7, 0)
            yield "sharpened", sharpened
        adaptive = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            2,
        )
        yield "adaptive_threshold", adaptive
        if should_try_expensive:
            otsu_source = cv2.GaussianBlur(clahe, (3, 3), 0)
            _threshold, otsu = cv2.threshold(otsu_source, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            yield "otsu_threshold", otsu

    def _scan_active_qr_region(self, frame, gray):
        if not self.is_qr_attention_active() or self._active_qr_rect is None:
            return None, False

        cropped_frame, cropped_gray, offset = self._crop_active_region(frame, gray)
        if cropped_frame is None:
            self._active_qr_rect = None
            return None, False

        points = self._detect_qr_points(cropped_gray)
        if points is None:
            pyzbar_value = self._decode_with_pyzbar(cropped_frame, gray=cropped_gray, prefer_gray=True)
            if pyzbar_value:
                logger.info("QR decoded from active external QR crop using pyzbar")
                return pyzbar_value, True
            return None, False

        self._mark_qr_attention()
        full_points = points.copy()
        full_points[..., 0] += offset[0]
        full_points[..., 1] += offset[1]
        self._active_qr_rect = self._rect_from_points(full_points, frame.shape, margin=QR_ACTIVE_ROI_MARGIN)

        decoded = self._decode_detected_qr(cropped_frame, cropped_gray, points)
        if decoded:
            logger.info("QR decoded from active external QR crop")
            return decoded, True
        return None, True

    def _decode_phone_screen_qr(self, frame, gray):
        pyzbar_result = self._decode_with_pyzbar(
            frame,
            gray=gray,
            prefer_gray=True,
            scaled_width=QR_PYZBAR_FRAME_WIDTH,
            return_rect=True,
            fast=True,
            log_success=False,
        )
        if not pyzbar_result:
            return None

        value, rect = pyzbar_result
        if rect is not None:
            self._active_qr_rect = self._expand_rect(rect, frame.shape, margin=QR_ACTIVE_ROI_MARGIN)
        self._mark_qr_attention()
        return value

    def _crop_active_region(self, frame, gray):
        if self._active_qr_rect is None:
            return None, None, (0, 0)
        x0, y0, x1, y1 = self._active_qr_rect
        x0 = max(0, min(frame.shape[1] - 1, int(x0)))
        y0 = max(0, min(frame.shape[0] - 1, int(y0)))
        x1 = max(x0 + 1, min(frame.shape[1], int(x1)))
        y1 = max(y0 + 1, min(frame.shape[0], int(y1)))
        return frame[y0:y1, x0:x1], gray[y0:y1, x0:x1], (x0, y0)

    def _detect_qr_points(self, gray):
        scale = 1.0
        detect_gray = gray
        if gray.shape[1] > QR_DETECT_WIDTH:
            scale = QR_DETECT_WIDTH / float(gray.shape[1])
            detect_gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

        points = self._detect_points_on_candidate(detect_gray)
        if points is None:
            equalized = cv2.equalizeHist(detect_gray)
            points = self._detect_points_on_candidate(equalized)
        if points is None:
            return None
        if scale != 1.0:
            points = points / scale
        return points.astype(np.float32)

    def _detect_points_on_candidate(self, candidate):
        try:
            ok, points = self._detector.detect(candidate)
        except Exception:
            logger.exception("QR point detection failed")
            return None
        if ok and points is not None:
            return points

        if not hasattr(self._detector, "detectMulti"):
            return None
        try:
            ok, points = self._detector.detectMulti(candidate)
        except Exception:
            logger.exception("QR multi-point detection failed")
            return None
        if ok and points is not None:
            return points
        return None

    def _decode_detected_qr(self, frame, gray, points):
        decoded = self._decode_with_points(gray, points)
        if decoded:
            logger.info("QR decoded from detected external QR points")
            return decoded

        decoded = self._decode_from_points(frame, gray, points)
        if decoded:
            logger.info("QR decoded from perspective-corrected external frame")
            return decoded

        for candidate_name, candidate in self._focused_candidates(frame, gray, points):
            decoded, extra_points = self._decode_candidate(candidate)
            if extra_points is not None:
                self._mark_qr_attention()
            if decoded:
                logger.info("QR decoded from focused %s external frame", candidate_name)
                return decoded

        roi = self._qr_roi(frame, points)
        if roi is not None:
            pyzbar_value = self._decode_with_pyzbar(roi, prefer_gray=True)
            if pyzbar_value:
                logger.info("QR decoded from focused external QR ROI using pyzbar")
                return pyzbar_value
        return None

    def _decode_with_points(self, gray, points):
        if not hasattr(self._detector, "decode"):
            return None
        for qr_points in self._iter_point_sets(points):
            try:
                decoded, _straight = self._detector.decode(gray, qr_points)
            except Exception:
                logger.exception("QR point decode failed")
                continue
            decoded = decoded.strip() if decoded else ""
            if decoded:
                return decoded
        return None

    def _iter_point_sets(self, points):
        pts = np.asarray(points, dtype=np.float32)
        if pts.ndim == 2:
            yield pts.reshape(1, -1, 2)
            return
        if pts.ndim == 3 and pts.shape[0] == 1:
            yield pts
            return
        for index in range(pts.shape[0]):
            yield pts[index].reshape(1, -1, 2)

    def _focused_candidates(self, frame, gray, points):
        roi = self._qr_roi(frame, points)
        if roi is None:
            return

        roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        yield "roi_gray", roi_gray
        yield "roi_upscaled", cv2.resize(roi_gray, None, fx=1.8, fy=1.8, interpolation=cv2.INTER_CUBIC)
        if QR_SHARPEN_ENABLED:
            blurred = cv2.GaussianBlur(roi_gray, (0, 0), 1.0)
            yield "roi_sharpened", cv2.addWeighted(roi_gray, 1.8, blurred, -0.8, 0)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(roi_gray)
        yield "roi_clahe", clahe

    def _decode_from_points(self, frame, gray, points):
        for candidate_name, candidate in self._warped_qr_candidates(frame, gray, points):
            decoded, _points = self._decode_candidate(candidate)
            if decoded:
                logger.info("QR decoded from warped %s candidate", candidate_name)
                return decoded

            pyzbar_value = self._decode_with_pyzbar(candidate, prefer_gray=True)
            if pyzbar_value:
                logger.info("QR decoded from warped %s pyzbar candidate", candidate_name)
                return pyzbar_value
        return None

    def _warped_qr_candidates(self, frame, gray, points):
        for point_set in self._iter_point_sets(points):
            ordered = self._ordered_qr_points(point_set)
            if ordered is None:
                continue

            top_width = np.linalg.norm(ordered[1] - ordered[0])
            bottom_width = np.linalg.norm(ordered[2] - ordered[3])
            left_height = np.linalg.norm(ordered[3] - ordered[0])
            right_height = np.linalg.norm(ordered[2] - ordered[1])
            side = int(max(top_width, bottom_width, left_height, right_height))
            if side < 24:
                continue

            side = min(max(side, 180), 720)
            destination = np.array(
                [[0, 0], [side - 1, 0], [side - 1, side - 1], [0, side - 1]],
                dtype=np.float32,
            )
            matrix = cv2.getPerspectiveTransform(ordered, destination)
            for source_name, source in (("gray", gray), ("bgr", frame)):
                warped = cv2.warpPerspective(source, matrix, (side, side))
                bordered = cv2.copyMakeBorder(
                    warped,
                    max(8, side // 12),
                    max(8, side // 12),
                    max(8, side // 12),
                    max(8, side // 12),
                    cv2.BORDER_CONSTANT,
                    value=255,
                )
                yield source_name, bordered
                yield f"{source_name}_upscaled", cv2.resize(
                    bordered,
                    None,
                    fx=1.6,
                    fy=1.6,
                    interpolation=cv2.INTER_CUBIC,
                )

    def _rect_from_points(self, points, frame_shape, *, margin=0.35):
        pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
        if pts.size == 0:
            return None

        x_min, y_min = np.min(pts, axis=0)
        x_max, y_max = np.max(pts, axis=0)
        width = x_max - x_min
        height = y_max - y_min
        if width < 8 or height < 8:
            return None

        pad = max(width, height) * margin
        x0 = max(0, int(x_min - pad))
        y0 = max(0, int(y_min - pad))
        x1 = min(frame_shape[1], int(x_max + pad))
        y1 = min(frame_shape[0], int(y_max + pad))
        if x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1, y1

    def _ordered_qr_points(self, points):
        if points is None:
            return None
        pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
        if len(pts) < 4:
            return None

        sums = pts.sum(axis=1)
        diffs = np.diff(pts, axis=1).reshape(-1)
        ordered = np.array(
            [
                pts[np.argmin(sums)],
                pts[np.argmin(diffs)],
                pts[np.argmax(sums)],
                pts[np.argmax(diffs)],
            ],
            dtype=np.float32,
        )
        if len({tuple(point) for point in ordered}) < 4:
            return None
        return ordered

    def _qr_roi(self, frame, points):
        if points is None:
            return None
        pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
        if pts.size == 0:
            return None

        x_min, y_min = np.min(pts, axis=0)
        x_max, y_max = np.max(pts, axis=0)
        width = x_max - x_min
        height = y_max - y_min
        if width < 12 or height < 12:
            return None

        margin = max(width, height) * 0.35
        x0 = max(0, int(x_min - margin))
        y0 = max(0, int(y_min - margin))
        x1 = min(frame.shape[1], int(x_max + margin))
        y1 = min(frame.shape[0], int(y_max + margin))
        if x1 <= x0 or y1 <= y0:
            return None
        return frame[y0:y1, x0:x1]

    def _decode_candidate(self, frame):
        decoded, points, _straight = self._detector.detectAndDecode(frame)
        decoded = decoded.strip() if decoded else ""
        if decoded:
            return decoded, points

        if hasattr(self._detector, "detectAndDecodeMulti"):
            ok, decoded_values, points, _straight = self._detector.detectAndDecodeMulti(frame)
            if ok and decoded_values:
                for decoded_value in decoded_values:
                    decoded_value = decoded_value.strip() if decoded_value else ""
                    if decoded_value:
                        return decoded_value, points
            if points is not None:
                return None, points

        if hasattr(self._detector, "detectAndDecodeCurved"):
            decoded, points, _straight = self._detector.detectAndDecodeCurved(frame)
            decoded = decoded.strip() if decoded else ""
            if decoded:
                return decoded, points
            if points is not None:
                return None, points

        return None, points

    def _decode_with_pyzbar(
        self,
        frame,
        *,
        gray=None,
        prefer_gray=False,
        scaled_width=None,
        return_rect=False,
        fast=False,
        log_success=True,
    ):
        if pyzbar_decode is None:
            if QR_SCAN_DEBUG and not self._pyzbar_unavailable_logged:
                self._pyzbar_unavailable_logged = True
                logger.info("Skipping pyzbar fallback because pyzbar/libzbar is not installed")
            return None

        candidates = []
        scale = 1.0
        if len(frame.shape) == 3:
            gray = gray if gray is not None else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if scaled_width and gray.shape[1] > scaled_width:
                scale = scaled_width / float(gray.shape[1])
                small_gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                small_frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            else:
                small_gray = gray
                small_frame = frame
            if prefer_gray:
                candidates.append(("gray", small_gray, scale))
                if not fast:
                    candidates.append(("bgr", small_frame, scale))
            else:
                candidates.append(("bgr", small_frame, scale))
                if not fast:
                    candidates.append(("gray", small_gray, scale))
            if QR_SHARPEN_ENABLED and not fast:
                blurred = cv2.GaussianBlur(small_gray, (0, 0), 1.0)
                candidates.append(("sharpened", cv2.addWeighted(small_gray, 1.7, blurred, -0.7, 0), scale))
            if not fast and self._decode_attempts % QR_PYZBAR_EVERY_N_FRAMES == 0:
                clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(small_gray)
                candidates.append(("clahe", clahe, scale))
        else:
            if scaled_width and frame.shape[1] > scaled_width:
                scale = scaled_width / float(frame.shape[1])
                frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            candidates.append(("gray", frame, scale))

        for candidate_name, candidate, candidate_scale in candidates:
            try:
                if ZBarSymbol is not None:
                    decoded_items = pyzbar_decode(candidate, symbols=[ZBarSymbol.QRCODE])
                else:
                    decoded_items = pyzbar_decode(candidate)
            except Exception as exc:
                logger.warning("pyzbar QR fallback failed: %s", exc)
                return None

            for item in decoded_items:
                value = item.data.decode("utf-8", errors="replace").strip()
                if value:
                    if log_success:
                        logger.info("QR decoded from external camera using pyzbar %s fallback", candidate_name)
                    rect = self._pyzbar_item_rect(item, candidate_scale)
                    return (value, rect) if return_rect else value
        return None

    def _pyzbar_item_rect(self, item, scale):
        points = []
        polygon = getattr(item, "polygon", None)
        if polygon:
            points = [(point.x / scale, point.y / scale) for point in polygon]
        elif getattr(item, "rect", None):
            rect = item.rect
            points = [
                (rect.left / scale, rect.top / scale),
                ((rect.left + rect.width) / scale, rect.top / scale),
                ((rect.left + rect.width) / scale, (rect.top + rect.height) / scale),
                (rect.left / scale, (rect.top + rect.height) / scale),
            ]
        if not points:
            return None
        pts = np.asarray(points, dtype=np.float32)
        x_min, y_min = np.min(pts, axis=0)
        x_max, y_max = np.max(pts, axis=0)
        return int(x_min), int(y_min), int(x_max), int(y_max)

    def _expand_rect(self, rect, frame_shape, *, margin=0.35):
        x0, y0, x1, y1 = rect
        width = max(1, x1 - x0)
        height = max(1, y1 - y0)
        pad = max(width, height) * margin
        expanded = (
            max(0, int(x0 - pad)),
            max(0, int(y0 - pad)),
            min(frame_shape[1], int(x1 + pad)),
            min(frame_shape[0], int(y1 + pad)),
        )
        if expanded[2] <= expanded[0] or expanded[3] <= expanded[1]:
            return None
        return expanded

    def _mark_qr_attention(self):
        until = time.monotonic() + QR_ATTENTION_HOLD_SECONDS
        with self._qr_attention_lock:
            if until > self._qr_attention_until:
                self._qr_attention_until = until

    def is_qr_attention_active(self):
        with self._qr_attention_lock:
            return time.monotonic() < self._qr_attention_until

    def _maybe_save_debug_frame(self, frame, label):
        if not QR_SAVE_DEBUG_FRAMES or cv2 is None:
            return

        now = time.monotonic()
        if now - self._last_debug_frame_saved_at < QR_DEBUG_SAVE_INTERVAL_SECONDS:
            return
        self._last_debug_frame_saved_at = now

        try:
            QR_DEBUG_FRAME_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            path = QR_DEBUG_FRAME_DIR / f"{timestamp}_{label}_{self.frame_buffer.width}x{self.frame_buffer.height}.jpg"
            cv2.imwrite(str(path), frame)
            logger.info("Saved QR debug frame: %s", path)
        except Exception:
            logger.exception("Failed to save QR debug frame")

    def _queue_scan(self, raw_value):
        try:
            payload, debounce_key = parse_qr_value(raw_value)
        except Exception as exc:
            logger.warning("Invalid QR payload: %s", exc)
            threading.Thread(
                target=self._reject_invalid_scan,
                args=(raw_value, str(exc)),
                daemon=True,
                name="qr-invalid-scan",
            ).start()
            return

        now = time.monotonic()
        with self._scan_lock:
            if debounce_key == self._last_decoded_key and now - self._last_decoded_at < QR_REPEAT_SUPPRESS_SECONDS:
                return
            self._last_decoded_key = debounce_key
            self._last_decoded_at = now

            last_time = self._last_seen.get(debounce_key, 0)
            if now - last_time < DEBOUNCE_SECONDS:
                if QR_SCAN_DEBUG:
                    logger.info("Debounced repeated QR token for %.1fs: %s", DEBOUNCE_SECONDS, debounce_key)
                return
            if debounce_key in self._processing_keys:
                logger.info("QR verification already in progress for token: %s", debounce_key)
                return
            self._last_seen[debounce_key] = now
            self._processing_keys.add(debounce_key)

        logger.info("QR decoded from external camera: %s", summarize_qr_value(raw_value))
        if self.video_device:
            self._maybe_retry_focus()
        logger.info("Queued QR verification worker for token: %s", debounce_key)
        threading.Thread(
            target=self._process_scan,
            args=(raw_value, payload, debounce_key),
            daemon=True,
            name="qr-verification",
        ).start()

    def _reject_invalid_scan(self, raw_value, error):
        self.gpio_controller.pulse_failure()
        write_scan_log(raw_value, None, "failure_gpio_14", error)

    def _process_scan(self, raw_value, payload, debounce_key):
        backend_response = None

        try:
            try:
                logger.info("Verifying QR with backend: %s", VERIFY_URL)
                backend_response = verify_qr(payload)
                logger.info("QR backend response: %s", backend_response)
            except Exception as exc:
                logger.warning("QR backend verification error; locker will stay closed: %s", exc)
                self.gpio_controller.pulse_failure()
                write_scan_log(raw_value, backend_response, "failure_gpio_14", str(exc))
                return

            if should_open_locker(backend_response):
                duration = unlock_duration(backend_response)
                self.gpio_controller.pulse_success(duration)
                write_scan_log(raw_value, backend_response, f"success_gpio_15_{duration}s")
                return

            logger.info("QR backend denied access; locker will stay closed")
            self.gpio_controller.pulse_failure()
            write_scan_log(raw_value, backend_response, "failure_gpio_14")
        finally:
            self._finish_processing(debounce_key)

    def _finish_processing(self, debounce_key):
        with self._scan_lock:
            self._processing_keys.discard(debounce_key)

    def _log_fps(self):
        self._processed_frames += 1
        now = time.monotonic()
        elapsed = now - self._fps_window_started_at
        if elapsed < 10:
            return
        logger.info("QR scanner FPS %.2f on external camera", self._processed_frames / elapsed)
        self._processed_frames = 0
        self._fps_window_started_at = now

    def _maybe_retry_focus(self):
        now = time.monotonic()
        if now - self._last_focus_retry_at < QR_FOCUS_RETRY_SECONDS:
            return
        self._last_focus_retry_at = now
        with self._focus_worker_lock:
            if self._focus_worker_running:
                return
            self._focus_worker_running = True
        threading.Thread(
            target=self._focus_retry_worker,
            daemon=True,
            name="qr-focus-retry",
        ).start()

    def _focus_retry_worker(self):
        try:
            logger.info("QR decode retry: QR pattern seen, nudging external camera autofocus")
            self.camera_controls.enable_autofocus(self.video_device, reason="QR decode retry")
        finally:
            with self._focus_worker_lock:
                self._focus_worker_running = False

    def _log_scan_status(self, qr_seen, decoded, metrics):
        now = time.monotonic()
        if decoded or now - self._last_status_log_at < QR_STATUS_LOG_SECONDS:
            return
        self._last_status_log_at = now
        logger.info(
            "QR scan status: external frames active, size=%sx%s, brightness=%.1f, contrast=%.1f, blur=%.1f, "
            "qr_pattern_seen=%s, decoded=no",
            self.frame_buffer.width,
            self.frame_buffer.height,
            metrics["brightness"],
            metrics["contrast"],
            metrics["blur"],
            "yes" if qr_seen else "no",
        )

    def _maybe_log_no_frames(self, sequence):
        now = time.monotonic()
        if now - self._last_no_frame_log_at < NO_FRAME_LOG_SECONDS:
            return
        self._last_no_frame_log_at = now
        if sequence == self._last_sequence and sequence >= 0:
            logger.warning(
                "QR scanner has not received a new external frame for %.1fs; "
                "if ffmpeg logs 'Device or resource busy', stop other camera users or systemd instances.",
                NO_FRAME_LOG_SECONDS,
            )
            return
        logger.warning("QR scanner is waiting for the first external camera frame")


def parse_qr_value(raw_value):
    value = raw_value.strip()
    if not value:
        raise ValueError("QR value is empty")
    locker_id = get_qr_locker_id()
    device_id = get_qr_device_id()

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
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
        return payload, debounce_key

    unique_token = str(parsed.get("unique_token") or parsed.get("token") or "").strip()
    qr_code_id = str(parsed.get("qr_code_id") or "").strip()
    if not unique_token:
        raise ValueError("QR JSON is missing unique_token")

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


def verify_qr(payload):
    response = requests.post(VERIFY_URL, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
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


def unlock_duration(response):
    data = response.get("data") if isinstance(response, dict) else {}
    try:
        duration = int(data.get("unlock_duration_seconds", DEFAULT_UNLOCK_SECONDS))
    except (TypeError, ValueError):
        duration = DEFAULT_UNLOCK_SECONDS
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
