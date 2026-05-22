import os
import threading
import time

from app.utils.python_path import add_system_dist_packages

add_system_dist_packages()

try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None
from app.streaming_agent.gpio.relay_controller import RelayController
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager
logger = LoggingManager.get_logger(__name__)


def _env_float(name, default, minimum=None, maximum=None):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def _env_int(name, default, minimum=None):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    return value


class TamperDetection:
    """Detect camera tampering from an existing shared stream frame buffer."""

    def __init__(
        self,
        frame_buffer,
        *,
        camera_role,
        led_controller=None,
        process_every_n_frames=None,
        tamper_confirm_seconds=None,
        tamper_clear_seconds=3.0,
        dark_brightness_threshold=None,
        bright_brightness_threshold=None,
        blur_threshold=None,
        edge_density_threshold=None,
        large_change_threshold=None,
        skip_when=None,
    ):
        self.frame_buffer = frame_buffer
        self.camera_role = camera_role
        self._owns_led_controller = led_controller is None
        self.led_controller = led_controller or RelayController()
        self.process_every_n_frames = (
            _env_int("TAMPER_DETECTOR_EVERY_N_FRAMES", 2, minimum=1)
            if process_every_n_frames is None
            else max(1, int(process_every_n_frames))
        )
        self.tamper_confirm_seconds = (
            _env_float("TAMPER_CONFIRM_SECONDS", 0.5, minimum=0.0)
            if tamper_confirm_seconds is None
            else max(0.0, float(tamper_confirm_seconds))
        )
        self.tamper_clear_seconds = max(0.1, float(tamper_clear_seconds))
        self.dark_brightness_threshold = (
            _env_float("TAMPER_DARK_BRIGHTNESS_THRESHOLD", 25.0, minimum=0.0, maximum=255.0)
            if dark_brightness_threshold is None
            else float(dark_brightness_threshold)
        )
        self.bright_brightness_threshold = (
            _env_float("TAMPER_BRIGHT_BRIGHTNESS_THRESHOLD", 245.0, minimum=0.0, maximum=255.0)
            if bright_brightness_threshold is None
            else float(bright_brightness_threshold)
        )
        self.blur_threshold = (
            _env_float("TAMPER_BLUR_THRESHOLD", 10.0, minimum=0.0)
            if blur_threshold is None
            else float(blur_threshold)
        )
        self.edge_density_threshold = (
            _env_float("TAMPER_EDGE_DENSITY_THRESHOLD", 0.004, minimum=0.0, maximum=1.0)
            if edge_density_threshold is None
            else float(edge_density_threshold)
        )
        self.large_change_threshold = (
            _env_float("TAMPER_LARGE_CHANGE_THRESHOLD", 0.70, minimum=0.0, maximum=1.0)
            if large_change_threshold is None
            else float(large_change_threshold)
        )
        confirm_frame_default = 1 if tamper_confirm_seconds is not None else 2
        clear_frame_default = 1 if tamper_clear_seconds != 3.0 else 2
        self._required_tamper_frames = _env_int("TAMPER_CONFIRM_FRAMES", confirm_frame_default, minimum=1)
        self._required_clear_frames = _env_int("TAMPER_CLEAR_FRAMES", clear_frame_default, minimum=1)
        self.skip_when = skip_when

        self._running = False
        self._thread = None
        self._last_sequence = -1
        self._baseline_gray = None
        self._tamper_started_at = None
        self._last_tamper_seen_at = time.monotonic()
        self._tamper_active = False
        self._tamper_streak = 0
        self._clear_streak = 0
        self._processed_frames = 0
        self._fps_window_started_at = time.monotonic()
        self._last_metrics_log_at = 0.0

    def start(self):
        if self._running:
            logger.info("Tamper detector for %s is already running", self.camera_role)
            return
        if cv2 is None or np is None:
            logger.warning("Tamper detector disabled for %s: OpenCV and NumPy are required", self.camera_role)
            return
        if self.frame_buffer is None:
            logger.warning("Tamper detector disabled for %s: no shared frame buffer available", self.camera_role)
            return

        self.led_controller.start()
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"{self.camera_role}-tamper-detector",
        )
        self._thread.start()
        logger.info("Tamper detector started for %s camera", self.camera_role)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self.led_controller.set_tamper_active(self.camera_role, False)
        if self._owns_led_controller:
            self.led_controller.cleanup()
        logger.info("Tamper detector stopped for %s camera", self.camera_role)

    def _run(self):
        while self._running:
            frame_bytes, sequence, _ = self.frame_buffer.latest()
            if frame_bytes is None or sequence == self._last_sequence:
                time.sleep(0.02)
                continue

            self._last_sequence = sequence
            if sequence % self.process_every_n_frames != 0:
                continue

            try:
                if self._should_skip_detection():
                    self._pause_tamper_state("QR pattern active")
                    continue
                tampered, reason = self._detect_tamper(frame_bytes)
                self._update_tamper_state(tampered, reason)
                self._log_fps()
            except Exception:
                logger.exception("Tamper detection failed for %s camera", self.camera_role)

    def _should_skip_detection(self):
        if self.skip_when is None:
            return False
        try:
            return bool(self.skip_when())
        except Exception:
            logger.exception("Tamper skip check failed for %s camera", self.camera_role)
            return False

    def _pause_tamper_state(self, reason):
        self._tamper_started_at = None
        self._last_tamper_seen_at = time.monotonic()
        self._tamper_streak = 0
        self._clear_streak = 0
        if self._tamper_active:
            self._tamper_active = False
            self.led_controller.set_tamper_active(self.camera_role, False)
            logger.info("Tamper paused for %s camera while %s", self.camera_role, reason)

    def _detect_tamper(self, frame_bytes):
        frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(
            self.frame_buffer.height,
            self.frame_buffer.width,
            self.frame_buffer.channels,
        )
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (160, 120), interpolation=cv2.INTER_AREA)

        brightness = float(np.mean(small))
        blur_score = float(cv2.Laplacian(small, cv2.CV_64F).var())
        edges = cv2.Canny(small, 80, 160)
        edge_density = float(np.count_nonzero(edges)) / float(edges.size)

        dark_or_covered = brightness <= self.dark_brightness_threshold
        overexposed = brightness >= self.bright_brightness_threshold
        texture_missing = blur_score <= self.blur_threshold or edge_density <= self.edge_density_threshold

        if self._baseline_gray is None and not dark_or_covered and not overexposed:
            self._baseline_gray = small.astype(np.float32)
            return False, ""

        scene_change = 0.0
        if self._baseline_gray is not None:
            delta = cv2.absdiff(small, cv2.convertScaleAbs(self._baseline_gray))
            scene_change = float(np.mean(delta > 45))
            if not dark_or_covered and not overexposed and not texture_missing:
                cv2.accumulateWeighted(small.astype(np.float32), self._baseline_gray, 0.02)

        self._maybe_log_metrics(brightness, blur_score, edge_density, scene_change)

        if dark_or_covered:
            return True, f"covered/dark brightness={brightness:.1f} blur={blur_score:.1f} edges={edge_density:.4f}"
        if overexposed:
            return True, f"covered/bright brightness={brightness:.1f} blur={blur_score:.1f} edges={edge_density:.4f}"
        if scene_change >= self.large_change_threshold and texture_missing:
            return True, f"blocked scene_change={scene_change:.2f} blur={blur_score:.1f} edges={edge_density:.4f}"
        return False, ""

    def _update_tamper_state(self, tampered, reason):
        now = time.monotonic()
        if tampered:
            self._last_tamper_seen_at = now
            self._tamper_streak += 1
            self._clear_streak = 0
            if self._tamper_started_at is None:
                self._tamper_started_at = now
                logger.warning("Possible tamper on %s camera: %s", self.camera_role, reason)
            if (
                not self._tamper_active
                and self._tamper_streak >= self._required_tamper_frames
                and now - self._tamper_started_at >= self.tamper_confirm_seconds
            ):
                self._tamper_active = True
                self.led_controller.set_tamper_active(self.camera_role, True)
                logger.warning("Tamper confirmed on %s camera; GPIO LEDs ON: %s", self.camera_role, reason)
            return

        self._tamper_started_at = None
        self._clear_streak += 1
        self._tamper_streak = 0
        if (
            self._tamper_active
            and self._clear_streak >= self._required_clear_frames
            and now - self._last_tamper_seen_at >= self.tamper_clear_seconds
        ):
            self._tamper_active = False
            self.led_controller.set_tamper_active(self.camera_role, False)
            logger.info("Tamper cleared on %s camera; GPIO LEDs may turn OFF if no other detection is active", self.camera_role)

    def _log_fps(self):
        self._processed_frames += 1
        now = time.monotonic()
        elapsed = now - self._fps_window_started_at
        if elapsed < 10:
            return
        logger.info("Tamper detection FPS %.2f for %s camera", self._processed_frames / elapsed, self.camera_role)
        self._processed_frames = 0
        self._fps_window_started_at = now

    def _maybe_log_metrics(self, brightness, blur_score, edge_density, scene_change):
        now = time.monotonic()
        if now - self._last_metrics_log_at < 5:
            return
        self._last_metrics_log_at = now
        logger.info(
            "Tamper metrics for %s camera: brightness=%.1f blur=%.1f edges=%.4f scene_change=%.2f",
            self.camera_role,
            brightness,
            blur_score,
            edge_density,
            scene_change,
        )
