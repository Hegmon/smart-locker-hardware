import threading
import time
try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None
from app.streaming_agent.gpio.led_controller import LedController
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager
logger = LoggingManager.get_logger(__name__)
class TamperDetection:
    """Detect camera tampering from an existing shared stream frame buffer."""

    def __init__(
        self,
        frame_buffer,
        *,
        camera_role,
        led_controller=None,
        process_every_n_frames=3,
        tamper_confirm_seconds=1.0,
        tamper_clear_seconds=3.0,
        dark_brightness_threshold=35.0,
        bright_brightness_threshold=235.0,
        blur_threshold=20.0,
        edge_density_threshold=0.01,
        large_change_threshold=0.55,
        skip_when=None,
    ):
        self.frame_buffer = frame_buffer
        self.camera_role = camera_role
        self._owns_led_controller = led_controller is None
        self.led_controller = led_controller or LedController()
        self.process_every_n_frames = max(1, int(process_every_n_frames))
        self.tamper_confirm_seconds = max(0.1, float(tamper_confirm_seconds))
        self.tamper_clear_seconds = max(0.1, float(tamper_clear_seconds))
        self.dark_brightness_threshold = float(dark_brightness_threshold)
        self.bright_brightness_threshold = float(bright_brightness_threshold)
        self.blur_threshold = float(blur_threshold)
        self.edge_density_threshold = float(edge_density_threshold)
        self.large_change_threshold = float(large_change_threshold)
        self.skip_when = skip_when

        self._running = False
        self._thread = None
        self._last_sequence = -1
        self._baseline_gray = None
        self._tamper_started_at = None
        self._last_tamper_seen_at = 0.0
        self._tamper_active = False
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
        self._last_tamper_seen_at = 0.0
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
            if self._tamper_started_at is None:
                self._tamper_started_at = now
                logger.warning("Possible tamper on %s camera: %s", self.camera_role, reason)
            if not self._tamper_active and now - self._tamper_started_at >= self.tamper_confirm_seconds:
                self._tamper_active = True
                self.led_controller.set_tamper_active(self.camera_role, True)
                logger.warning("Tamper confirmed on %s camera; GPIO LEDs ON: %s", self.camera_role, reason)
            return

        self._tamper_started_at = None
        if self._tamper_active and now - self._last_tamper_seen_at >= self.tamper_clear_seconds:
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
