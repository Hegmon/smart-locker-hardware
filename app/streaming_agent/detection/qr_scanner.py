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
    from pyzbar.pyzbar import decode as pyzbar_decode
except Exception:
    pyzbar_decode = None

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager
from app.streaming_agent.camera_controls import CameraControlManager


logger = LoggingManager.get_logger(__name__)

VERIFY_URL = os.getenv("BACKEND_QR_VERIFY_URL", "https://backend.qbox.sa/shipments/qr/verify/")
LOCKER_ID = os.getenv("LOCKER_ID", "TEWPUH775796")
DEVICE_ID = os.getenv("DEVICE_ID", "PI4-001")
LOG_FILE = Path(os.getenv("QR_SCAN_LOG_FILE", "logs/qr_scans.jsonl"))

SUCCESS_GPIO_PIN = int(os.getenv("QR_SUCCESS_GPIO_PIN", "15"))
FAILURE_GPIO_PIN = int(os.getenv("QR_FAILURE_GPIO_PIN", "14"))
DEFAULT_UNLOCK_SECONDS = int(os.getenv("QR_DEFAULT_UNLOCK_SECONDS", "5"))
FAILURE_SIGNAL_SECONDS = float(os.getenv("QR_FAILURE_SIGNAL_SECONDS", "2"))
DEBOUNCE_SECONDS = float(os.getenv("QR_DEBOUNCE_SECONDS", "5"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("QR_VERIFY_TIMEOUT_SECONDS", "10"))
PROCESS_EVERY_N_FRAMES = int(os.getenv("QR_PROCESS_EVERY_N_FRAMES", "2"))
NO_FRAME_LOG_SECONDS = float(os.getenv("QR_NO_FRAME_LOG_SECONDS", "5"))
QR_SCAN_DEBUG = os.getenv("QR_SCAN_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}
QR_SHARPEN_ENABLED = os.getenv("QR_SHARPEN_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
QR_FOCUS_RETRY_SECONDS = float(os.getenv("QR_FOCUS_RETRY_SECONDS", "1.5"))
QR_STATUS_LOG_SECONDS = float(os.getenv("QR_STATUS_LOG_SECONDS", "3"))
QR_SAVE_DEBUG_FRAMES = os.getenv("QR_SAVE_DEBUG_FRAMES", "false").strip().lower() in {"1", "true", "yes", "on"}
QR_DEBUG_FRAME_DIR = Path(os.getenv("QR_DEBUG_FRAME_DIR", "logs/qr_debug_frames"))
QR_DEBUG_SAVE_INTERVAL_SECONDS = float(os.getenv("QR_DEBUG_SAVE_INTERVAL_SECONDS", "5"))


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
                if qr_seen and self.video_device:
                    self.camera_controls.prepare_for_qr_scan(self.video_device, reason="QR pattern detected")
                if qr_value:
                    self._queue_scan(qr_value)
                elif self.video_device:
                    self._maybe_retry_focus()
                self._log_scan_status(qr_seen, bool(qr_value), metrics)
                self._log_fps()
            except Exception:
                logger.exception("QR scanner failed")

    def _decode_qr(self, frame_bytes):
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
        qr_seen = False
        for candidate_name, candidate in self._frame_candidates(frame):
            decoded, points = self._decode_candidate(candidate)
            qr_seen = qr_seen or points is not None
            if decoded:
                if candidate_name != "bgr":
                    logger.info("QR decoded from %s preprocessed external frame", candidate_name)
                return decoded, True, metrics
        if qr_seen:
            now = time.monotonic()
            if now - self._last_pattern_decode_failed_log_at >= QR_STATUS_LOG_SECONDS:
                self._last_pattern_decode_failed_log_at = now
                logger.warning("QR pattern detected on external camera but decode failed")
            self._maybe_save_debug_frame(frame, "pattern_seen_decode_failed")
        elif QR_SCAN_DEBUG:
            logger.info("No QR pattern detected in external camera frame")
        return None, qr_seen, metrics

    def _empty_metrics(self):
        return {
            "brightness": 0.0,
            "contrast": 0.0,
            "blur": 0.0,
        }

    def _frame_metrics(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return {
            "brightness": float(np.mean(gray)),
            "contrast": float(np.std(gray)),
            "blur": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        }

    def _frame_candidates(self, frame):
        yield "bgr", frame
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        yield "gray", gray
        downscaled = cv2.resize(frame, None, fx=0.75, fy=0.75, interpolation=cv2.INTER_AREA)
        yield "downscaled_0_75", downscaled
        half_size = cv2.resize(frame, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        yield "downscaled_0_5", half_size
        equalized = cv2.equalizeHist(gray)
        yield "equalized", equalized
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        yield "clahe", clahe
        upscaled = cv2.resize(frame, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
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
        otsu_source = cv2.GaussianBlur(clahe, (3, 3), 0)
        _threshold, otsu = cv2.threshold(otsu_source, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        yield "otsu_threshold", otsu

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

        pyzbar_value = self._decode_with_pyzbar(frame)
        if pyzbar_value:
            return pyzbar_value, points
        return None, points

    def _decode_with_pyzbar(self, frame):
        if pyzbar_decode is None:
            if QR_SCAN_DEBUG and not self._pyzbar_unavailable_logged:
                self._pyzbar_unavailable_logged = True
                logger.info("Skipping pyzbar fallback because pyzbar/libzbar is not installed")
            return None

        try:
            decoded_items = pyzbar_decode(frame)
        except Exception as exc:
            logger.warning("pyzbar QR fallback failed: %s", exc)
            return None

        for item in decoded_items:
            value = item.data.decode("utf-8", errors="replace").strip()
            if value:
                logger.info("QR decoded from external camera using pyzbar fallback")
                return value
        return None

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
        logger.info("QR decoded from external camera: %s", summarize_qr_value(raw_value))
        if self.video_device:
            self.camera_controls.enable_autofocus(self.video_device, reason="QR decoded")

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
            last_time = self._last_seen.get(debounce_key, 0)
            if now - last_time < DEBOUNCE_SECONDS:
                logger.info("Debounced repeated QR token for %.1fs: %s", DEBOUNCE_SECONDS, debounce_key)
                write_scan_log(raw_value, None, "debounced_no_gpio")
                return
            if debounce_key in self._processing_keys:
                logger.info("QR verification already in progress for token: %s", debounce_key)
                return
            self._last_seen[debounce_key] = now
            self._processing_keys.add(debounce_key)

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
        logger.info("QR decode retry: adjusting external camera focus/exposure")
        self.camera_controls.prepare_for_qr_scan(
            self.video_device,
            reason="QR decode retry",
            sweep_focus=True,
        )

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

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {
            "token": value,
            "locker_id": LOCKER_ID,
            "device_id": DEVICE_ID,
        }, value

    if not isinstance(parsed, dict):
        raise ValueError("QR JSON must be an object")

    if "qr_payload" in parsed or "qr_data" in parsed:
        payload = dict(parsed)
        payload["locker_id"] = str(payload.get("locker_id") or LOCKER_ID)
        payload["device_id"] = str(payload.get("device_id") or DEVICE_ID)
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
            "locker_id": str(parsed.get("locker_id") or LOCKER_ID),
            "device_id": str(parsed.get("device_id") or DEVICE_ID),
        }, unique_token

    return {
        "qr_payload": parsed,
        "locker_id": LOCKER_ID,
        "device_id": DEVICE_ID,
    }, unique_token


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
