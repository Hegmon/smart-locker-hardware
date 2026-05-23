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
from app.streaming_agent.config.runtime import StreamingAgentRuntimeConfig
from app.streaming_agent.gpio.relay_controller import RelayController
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager
logger = LoggingManager.get_logger(__name__)

TAMPER_HOLD_SECONDS = 5.0
TAMPER_TRIGGER_FRAMES = 5
TAMPER_CLEAR_FRAMES = 15


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


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
        tamper_clear_seconds=None,
        dark_brightness_threshold=None,
        bright_brightness_threshold=None,
        blur_threshold=None,
        edge_density_threshold=None,
        large_change_threshold=None,
        skip_when=None,
        detection_state_manager=None,
        runtime_config: StreamingAgentRuntimeConfig | None = None,
    ):
        self.runtime_config = runtime_config or StreamingAgentRuntimeConfig.from_env()
        self.frame_buffer = frame_buffer
        self.camera_role = camera_role
        self._owns_led_controller = led_controller is None
        self.led_controller = led_controller or RelayController()
        self.detection_state_manager = detection_state_manager
        self.process_every_n_frames = (
            _env_int("TAMPER_DETECTOR_EVERY_N_FRAMES", 1, minimum=1)
            if process_every_n_frames is None
            else max(1, int(process_every_n_frames))
        )
        self.tamper_confirm_seconds = (
            self.runtime_config.tamper.confirm_seconds
            if tamper_confirm_seconds is None
            else max(0.0, float(tamper_confirm_seconds))
        )
        self.tamper_clear_seconds = (
            self.runtime_config.tamper.clear_seconds
            if tamper_clear_seconds is None
            else max(0.0, float(tamper_clear_seconds))
        )
        self.dark_brightness_threshold = (
            self.runtime_config.tamper.dark_brightness_threshold
            if dark_brightness_threshold is None
            else float(dark_brightness_threshold)
        )
        self.bright_brightness_threshold = (
            self.runtime_config.tamper.bright_brightness_threshold
            if bright_brightness_threshold is None
            else float(bright_brightness_threshold)
        )
        self.blur_threshold = (
            self.runtime_config.tamper.blur_threshold
            if blur_threshold is None
            else float(blur_threshold)
        )
        self.edge_density_threshold = (
            self.runtime_config.tamper.edge_density_threshold
            if edge_density_threshold is None
            else float(edge_density_threshold)
        )
        self.large_change_threshold = (
            self.runtime_config.tamper.large_change_threshold
            if large_change_threshold is None
            else float(large_change_threshold)
        )
        self.hard_change_threshold = _env_float("TAMPER_HARD_CHANGE_THRESHOLD", 0.82, minimum=0.0, maximum=1.0)
        self.change_brightness_delta = _env_float("TAMPER_CHANGE_BRIGHTNESS_DELTA", 28.0, minimum=0.0, maximum=255.0)
        self.cover_brightness_delta = _env_float("TAMPER_COVER_BRIGHTNESS_DELTA", 35.0, minimum=0.0, maximum=255.0)
        self.scene_change_tamper_enabled = self.runtime_config.tamper.scene_change_enabled
        self._stale_clear_seconds = _env_float("TAMPER_STALE_CLEAR_SECONDS", 0.5, minimum=0.05)
        self._baseline_frame_target = _env_int("TAMPER_BASELINE_FRAMES", 5, minimum=1)
        self._required_tamper_frames = _env_int("TAMPER_TRIGGER_FRAMES", TAMPER_TRIGGER_FRAMES, minimum=1)
        self._required_clear_frames = _env_int("TAMPER_CLEAR_FRAMES", TAMPER_CLEAR_FRAMES, minimum=1)
        self.skip_when = skip_when

        self._running = False
        self._thread = None
        self._last_sequence = -1
        self._baseline_gray = None
        self._baseline_brightness = None
        self._baseline_frames_seen = 0
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
        if self.detection_state_manager is not None:
            self.detection_state_manager.clear_tamper(self.camera_role)
        # detectors do not directly control relays
        if self._owns_led_controller:
            self.led_controller.cleanup()
        logger.info("Tamper detector stopped for %s camera", self.camera_role)

    def _run(self):
        while self._running:
            frame_bytes, sequence, _ = self.frame_buffer.latest()
            if frame_bytes is None or sequence == self._last_sequence:
                self._clear_stale_tamper_state()
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
            if self.detection_state_manager is not None:
                self.detection_state_manager.clear_tamper(self.camera_role)
            # detectors do not directly control relays
            logger.info("Tamper paused for %s camera while %s", self.camera_role, reason)

    def _clear_stale_tamper_state(self):
        if not self._tamper_active:
            return
        if self.detection_state_manager is not None:
            self.detection_state_manager.check_timeouts()
            return
        if time.monotonic() - self._last_tamper_seen_at < self._stale_clear_seconds:
            return
        self._tamper_active = False
        self._tamper_started_at = None
        self._tamper_streak = 0
        self._clear_streak = 0
        # detectors do not directly control relays (state manager owns relay state)
        logger.info("No fresh tamper detection on %s camera; Relay 4 OFF", self.camera_role)

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

        texture_missing = blur_score <= self.blur_threshold or edge_density <= self.edge_density_threshold
        raw_dark = brightness <= self.dark_brightness_threshold and texture_missing
        raw_bright = brightness >= self.bright_brightness_threshold and texture_missing

        if self._baseline_frames_seen < self._baseline_frame_target:
            self._learn_baseline(small, brightness)
            self._baseline_frames_seen += 1
            return False, ""

        if self._baseline_gray is None:
            self._baseline_gray = small.astype(np.float32)
            self._baseline_brightness = brightness
            return False, ""

        scene_change = 0.0
        brightness_delta = 0.0
        if self._baseline_gray is not None:
            delta = cv2.absdiff(small, cv2.convertScaleAbs(self._baseline_gray))
            scene_change = float(np.mean(delta > 45))
            brightness_delta = abs(brightness - float(self._baseline_brightness or brightness))
            if not raw_dark and not raw_bright and not texture_missing:
                cv2.accumulateWeighted(small.astype(np.float32), self._baseline_gray, 0.02)
                self._baseline_brightness = (
                    0.98 * float(self._baseline_brightness or brightness)
                    + 0.02 * brightness
                )

        self._maybe_log_metrics(brightness, blur_score, edge_density, scene_change, brightness_delta)

        baseline_brightness = float(self._baseline_brightness or brightness)
        baseline_is_dark = baseline_brightness <= self.dark_brightness_threshold
        baseline_is_bright = baseline_brightness >= self.bright_brightness_threshold
        dark_or_covered = raw_dark and (
            not baseline_is_dark or baseline_brightness - brightness >= self.cover_brightness_delta
        )
        overexposed = raw_bright and (
            not baseline_is_bright or brightness - baseline_brightness >= self.cover_brightness_delta
        )

        if dark_or_covered:
            return True, f"covered/dark brightness={brightness:.1f} blur={blur_score:.1f} edges={edge_density:.4f}"
        if overexposed:
            return True, f"covered/bright brightness={brightness:.1f} blur={blur_score:.1f} edges={edge_density:.4f}"
        severe_blur = (
            blur_score <= self.blur_threshold
            and edge_density <= self.edge_density_threshold
            and brightness_delta >= self.change_brightness_delta * 0.5
        )
        if severe_blur:
            return True, (
                f"lens obstruction blur={blur_score:.1f} edges={edge_density:.4f} "
                f"brightness_delta={brightness_delta:.1f}"
            )
        scene_change_tamper = self.scene_change_tamper_enabled and (
            scene_change >= self.hard_change_threshold
            or (
                scene_change >= self.large_change_threshold
                and brightness_delta >= self.change_brightness_delta
            )
        )
        if scene_change_tamper:
            return True, (
                f"hard scene change={scene_change:.2f} brightness_delta={brightness_delta:.1f} "
                f"blur={blur_score:.1f} edges={edge_density:.4f}"
            )
        return False, ""

    def _learn_baseline(self, small, brightness):
        if self._baseline_gray is None:
            self._baseline_gray = small.astype(np.float32)
            self._baseline_brightness = brightness
            return
        cv2.accumulateWeighted(small.astype(np.float32), self._baseline_gray, 0.25)
        self._baseline_brightness = 0.75 * float(self._baseline_brightness or brightness) + 0.25 * brightness

    def _update_tamper_state(self, tampered, reason):
        now = time.monotonic()
        if tampered:
            self._last_tamper_seen_at = now
            self._tamper_streak += 1
            self._clear_streak = 0
            if self._tamper_started_at is None:
                self._tamper_started_at = now
                logger.warning("Tamper detection candidate start on %s camera: %s", self.camera_role, reason)
            if (
                not self._tamper_active
                and (self._tamper_streak >= self._required_tamper_frames or self._is_immediate_tamper(reason))
                and now - self._tamper_started_at >= self.tamper_confirm_seconds
            ):
                self._tamper_active = True
                if self.detection_state_manager is not None:
                    self.detection_state_manager.update_tamper(
                        self.camera_role,
                        tamper_detected=True,
                        reason=reason,
                    )
                # no direct relay control when no state manager (centralized authority)
                logger.warning("Tamper confirmed on %s camera; Relay 4 ON: %s", self.camera_role, reason)
            elif self._tamper_active and self.detection_state_manager is not None:
                self.detection_state_manager.update_tamper(
                    self.camera_role,
                    tamper_detected=True,
                    reason=reason,
                )
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
            if self.detection_state_manager is not None:
                self.detection_state_manager.update_tamper(self.camera_role, tamper_detected=False)
            # no direct relay control when no state manager (centralized authority)
            clear_age = now - self._last_tamper_seen_at
            logger.info("Tamper cleared on %s camera for %.2fs; Relay 4 OFF", self.camera_role, clear_age)

    @staticmethod
    def _is_immediate_tamper(reason):
        return "covered/" in str(reason or "")

    def _log_fps(self):
        self._processed_frames += 1
        now = time.monotonic()
        elapsed = now - self._fps_window_started_at
        if elapsed < 10:
            return
        logger.info("Tamper detection FPS %.2f for %s camera", self._processed_frames / elapsed, self.camera_role)
        self._processed_frames = 0
        self._fps_window_started_at = now

    def _maybe_log_metrics(self, brightness, blur_score, edge_density, scene_change, brightness_delta):
        now = time.monotonic()
        if now - self._last_metrics_log_at < 5:
            return
        self._last_metrics_log_at = now
        logger.info(
            "Tamper metrics for %s camera: brightness=%.1f blur=%.1f edges=%.4f scene_change=%.2f brightness_delta=%.1f",
            self.camera_role,
            brightness,
            blur_score,
            edge_density,
            scene_change,
            brightness_delta,
        )
