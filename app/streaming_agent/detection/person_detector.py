from pathlib import Path
import os
import subprocess
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

DETECTION_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_PATH = DETECTION_DIR / "models" / "detect.tflite"
ALT_MODEL_PATH = DETECTION_DIR / "models" / "model.tflite"
DEFAULT_LABELS_PATH = DETECTION_DIR / "labels.txt"
PROJECT_DIR = DETECTION_DIR.parents[2]
MODEL_INSTALLER = PROJECT_DIR / "app" / "scripts" / "install_detection_model.sh"
DETECTION_HOLD_SECONDS = 8.0
PERSON_TRIGGER_FRAMES = 3
PERSON_CLEAR_FRAMES = 10
MOTION_TRIGGER_FRAMES = 2
MOTION_CLEAR_FRAMES = 8
FACE_TRIGGER_FRAMES = 2
FACE_CLEAR_FRAMES = 6
HAND_TRIGGER_FRAMES = 2
HAND_CLEAR_FRAMES = 6

# Hysteresis thresholds to eliminate oscillation around the decision boundary
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


def _env_roi(name, default):
    raw = os.getenv(name, default)
    try:
        values = [float(part.strip()) for part in raw.split(",")]
    except (AttributeError, ValueError):
        values = [float(part) for part in default.split(",")]
    if len(values) != 4:
        values = [float(part) for part in default.split(",")]
    left, top, right, bottom = values
    left = min(max(left, 0.0), 0.95)
    top = min(max(top, 0.0), 0.95)
    right = min(max(right, left + 0.05), 1.0)
    bottom = min(max(bottom, top + 0.05), 1.0)
    return left, top, right, bottom


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


class PersonDetector:
    """Run lightweight person detection from the streaming agent's shared frame buffer."""

    def __init__(
        self,
        frame_buffer,
        *,
        model_path=DEFAULT_MODEL_PATH,
        labels_path=DEFAULT_LABELS_PATH,
        confidence_threshold=None,
        process_every_n_frames=None,
        led_off_delay_seconds=DETECTION_HOLD_SECONDS,
        led_controller=None,
        detection_state_manager=None,
        runtime_config: StreamingAgentRuntimeConfig | None = None,
    ):
        self.runtime_config = runtime_config or StreamingAgentRuntimeConfig.from_env()
        self.frame_buffer = frame_buffer
        self.model_path = self._resolve_model_path(model_path)
        self.labels_path = Path(labels_path)
        self.confidence_threshold = (
            max(0.6, self.runtime_config.person.confidence_threshold)
            if confidence_threshold is None
            else float(confidence_threshold)
        )
        self.process_every_n_frames = (
            _env_int("PERSON_DETECTOR_EVERY_N_FRAMES", 1, minimum=1)
            if process_every_n_frames is None
            else max(1, int(process_every_n_frames))
        )
        self._model_every_n_frames = _env_int("PERSON_MODEL_EVERY_N_FRAMES", 2, minimum=1)
        self.led_off_delay_seconds = _env_float("SECURITY_HOLD_SECONDS", _env_float("DETECTION_HOLD_SECONDS", led_off_delay_seconds, minimum=0.0), minimum=0.0)
        self._owns_led_controller = led_controller is None
        self.led_controller = led_controller or RelayController()
        self.detection_state_manager = detection_state_manager
        self._top_detection_log_seconds = _env_float("PERSON_DETECTOR_LOG_TOP_SECONDS", 10.0, minimum=0.0)
        self._required_detection_frames = _env_int("PERSON_TRIGGER_FRAMES", PERSON_TRIGGER_FRAMES, minimum=1)
        self._required_clear_frames = _env_int("PERSON_CLEAR_FRAMES", PERSON_CLEAR_FRAMES, minimum=1)
        self._motion_trigger_frames = _env_int("MOTION_TRIGGER_FRAMES", MOTION_TRIGGER_FRAMES, minimum=1)
        self._motion_clear_frames = _env_int("MOTION_CLEAR_FRAMES", MOTION_CLEAR_FRAMES, minimum=1)
        self._face_trigger_frames = _env_int("FACE_TRIGGER_FRAMES", FACE_TRIGGER_FRAMES, minimum=1)
        self._face_clear_frames = _env_int("FACE_CLEAR_FRAMES", FACE_CLEAR_FRAMES, minimum=1)
        self._hand_trigger_frames = _env_int("HAND_TRIGGER_FRAMES", HAND_TRIGGER_FRAMES, minimum=1)
        self._hand_clear_frames = _env_int("HAND_CLEAR_FRAMES", HAND_CLEAR_FRAMES, minimum=1)
        self._clear_seconds = _env_float("PERSON_DETECTION_CLEAR_SECONDS", self.led_off_delay_seconds, minimum=0.0)
        self._confidence_smoothing_alpha = _env_float(
            "PERSON_CONFIDENCE_SMOOTHING_ALPHA",
            0.4,
            minimum=0.01,
            maximum=1.0,
        )
        self._stale_clear_seconds = _env_float("PERSON_DETECTION_STALE_CLEAR_SECONDS", 0.35, minimum=0.05)
        self._min_box_area = _env_float("PERSON_DETECTION_MIN_BOX_AREA", 0.04, minimum=0.0, maximum=1.0)
        self._max_box_area = _env_float("PERSON_DETECTION_MAX_BOX_AREA", 0.95, minimum=0.01, maximum=1.0)
        self._near_object_enabled = _env_bool("PERSON_NEAR_OBJECT_ENABLED", False)
        self._near_change_threshold = _env_float("PERSON_NEAR_CHANGE_THRESHOLD", 0.22, minimum=0.01, maximum=1.0)
        self._near_brightness_delta = _env_float("PERSON_NEAR_BRIGHTNESS_DELTA", 6.0, minimum=0.0, maximum=255.0)
        self._near_edge_density_min = _env_float("PERSON_NEAR_EDGE_DENSITY_MIN", 0.004, minimum=0.0, maximum=1.0)
        self._near_roi = _env_roi("PERSON_NEAR_ROI", "0.10,0.10,0.90,0.90")
        self._baseline_learning_rate = _env_float("PERSON_BASELINE_LEARNING_RATE", 0.01, minimum=0.0, maximum=1.0)
        self._near_baseline_gray = None
        self._near_baseline_brightness = None
        self._motion_enabled = self.runtime_config.person.motion_enabled
        self._motion_threshold = self.runtime_config.person.motion_threshold
        legacy_motion_area = _env_float("PERSON_MOTION_MIN_CONTOUR_AREA", 0.02, minimum=0.0001, maximum=1.0)
        self._motion_min_contour_area = _env_float(
            "MOTION_MINIMUM_AREA",
            self.runtime_config.person.motion_minimum_area or legacy_motion_area,
            minimum=0.0001,
            maximum=1.0,
        )
        self._motion_pixel_delta = _env_int("PERSON_MOTION_PIXEL_DELTA", 28, minimum=1)
        self._motion_roi = _env_roi("PERSON_MOTION_ROI", "0.05,0.05,0.95,0.95")
        self._motion_baseline_gray = None
        self._motion_subtractor = cv2.createBackgroundSubtractorMOG2(history=80, varThreshold=20, detectShadows=False) if cv2 is not None else None
        self._last_motion_roi = None
        self._motion_rebaseline_seconds = _env_float("PERSON_MOTION_REBASELINE_SECONDS", 1.5, minimum=0.1)
        self._motion_candidate_started_at = None
        self._motion_retrigger_cooldown_seconds = _env_float("PERSON_MOTION_RETRIGGER_COOLDOWN_SECONDS", 1.0, minimum=0.0)
        self._motion_suppressed_until = 0.0
        self._face_enabled = _env_bool("FACE_DETECTION_ENABLED", False)
        self._hand_enabled = _env_bool("HAND_DETECTION_ENABLED", False)
        self._face_cascade = self._load_face_cascade()
        self._face_min_area = _env_float("FACE_MIN_AREA", 0.006, minimum=0.0001, maximum=1.0)
        self._hand_min_area = _env_float("HAND_MIN_AREA", 0.015, minimum=0.0001, maximum=1.0)
        self._human_score_threshold = _env_float("HUMAN_SCORE_THRESHOLD", 0.7, minimum=0.1, maximum=1.0)

        self._running = False
        self._thread = None
        self._interpreter = None
        self._input_details = None
        self._output_details = None
        self._input_height = 0
        self._input_width = 0
        self._input_dtype = None
        self._labels = []
        self._person_class_ids = {0, 1}
        self._last_person_seen_at = 0.0
        self._last_motion_seen_at = 0.0
        self._last_sequence = -1
        self._processed_frames = 0
        self._fps_window_started_at = time.monotonic()
        self._led_visible = False
        self._last_top_detection_log_at = 0.0
        self._detection_streak = 0
        self._clear_streak = 0
        self._motion_streak = 0
        self._motion_clear_streak = 0
        self._face_streak = 0
        self._face_clear_streak = 0
        self._hand_streak = 0
        self._hand_clear_streak = 0
        self._face_active = False
        self._hand_active = False
        self._person_active = False
        self._motion_active = False
        self._person_confidence_ema = 0.0

    def start(self):
        if self._running:
            return
        if self.frame_buffer is None:
            logger.warning("Person detector disabled: no shared frame buffer available")
            return
        if cv2 is None or np is None:
            logger.warning("Person/body detector disabled: opencv-python-headless and numpy are required")
            return

        if not self.model_path.exists():
            self._install_missing_model()
            self.model_path = self._resolve_model_path(self.model_path)

        if self.model_path.exists():
            try:
                self._load_model()
            except Exception as exc:
                logger.warning("Person model disabled; body-motion detection will continue: %s", exc)
                self._disable_model()
        else:
            logger.warning(
                "Person model disabled: model not found at %s. "
                "Place detect.tflite in app/streaming_agent/detection/models/ "
                "set PERSON_DETECTOR_MODEL_PATH, or run app/scripts/install_detection_model.sh. "
                "Body-motion detection will continue without the model.",
                self.model_path,
            )
            self._disable_model()

        self.led_controller.start()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="person-detector")
        self._thread.start()
        logger.info(
            "Person/body detector started; model_enabled=%s model=%s hold=%.1fs threshold=%.2f person_frames=%s/%s motion_frames=%s/%s model_every=%s",
            self._model_enabled(),
            self.model_path,
            self._clear_seconds,
            self.confidence_threshold,
            self._required_detection_frames,
            self._required_clear_frames,
            self._motion_trigger_frames,
            self._motion_clear_frames,
            self._model_every_n_frames,
        )

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self.detection_state_manager is not None:
            self.detection_state_manager.clear_presence("internal")
        # detectors do not directly control relays
        if self._owns_led_controller:
            self.led_controller.cleanup()
        logger.info("Person detector stopped")

    def _load_model(self):
        runtime_name = "tflite-runtime"
        try:
            from tflite_runtime.interpreter import Interpreter
        except Exception as tflite_exc:
            try:
                from ai_edge_litert.interpreter import Interpreter
                runtime_name = "ai-edge-litert"
            except Exception as litert_exc:
                raise RuntimeError(
                    "tflite-runtime or ai-edge-litert is required for person detection "
                    f"(tflite-runtime import failed: {tflite_exc}; "
                    f"ai-edge-litert import failed: {litert_exc})"
                ) from litert_exc

        self._labels = self._load_labels()
        self._person_class_ids = self._label_ids_for_person(self._labels)
        self._interpreter = Interpreter(model_path=str(self.model_path), num_threads=1)
        self._interpreter.allocate_tensors()
        self._input_details = self._interpreter.get_input_details()
        self._output_details = self._interpreter.get_output_details()

        input_shape = self._input_details[0]["shape"]
        self._input_height = int(input_shape[1])
        self._input_width = int(input_shape[2])
        self._input_dtype = self._input_details[0]["dtype"]
        logger.info(
            "Person detector model loaded: input=%sx%s dtype=%s person_class_ids=%s threshold=%.2f",
            self._input_width,
            self._input_height,
            self._input_dtype,
            sorted(self._person_class_ids),
            self.confidence_threshold,
        )
        logger.info("Person detector inference runtime: %s", runtime_name)

    def _disable_model(self):
        self._interpreter = None
        self._input_details = None
        self._output_details = None
        self._input_height = 0
        self._input_width = 0
        self._input_dtype = None

    def _model_enabled(self):
        return (
            self._interpreter is not None
            and self._input_details is not None
            and self._output_details is not None
            and self._input_width > 0
            and self._input_height > 0
        )

    def _run(self):
        while self._running:
            frame_bytes, sequence, _ = self.frame_buffer.latest()
            if frame_bytes is None or sequence == self._last_sequence:
                self._clear_stale_led_state()
                time.sleep(0.01)
                continue

            self._last_sequence = sequence
            if sequence % self.process_every_n_frames != 0:
                continue

            person_detected = False
            face_detected = False
            hand_detected = False
            motion_detected = False
            human_score = 0.0
            reason = ""
            try:
                face_detected, hand_detected, person_detected, motion_detected, human_score, reason = self._detect_presence(frame_bytes, sequence)
            except Exception:
                logger.exception("Person detection failed")

            self._update_led_state(
                human_score >= self._human_score_threshold,
                reason,
                face_detected=face_detected,
                hand_detected=hand_detected,
                person_detected=person_detected,
                motion_detected=motion_detected,
                human_score=human_score,
            )
            self._log_fps()

    def _clear_stale_led_state(self):
        if not self._led_visible:
            return
        last_seen_at = max(self._last_person_seen_at, self._last_motion_seen_at)
        if time.monotonic() - last_seen_at < self._stale_clear_seconds:
            return
        if self.detection_state_manager is not None:
            self.detection_state_manager.check_timeouts()
            return
        logger.info("Internal presence state went stale; clearing detector state")
        self._clear_person_state()

    def _detect_presence(self, frame_bytes, sequence):
        frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(
            self.frame_buffer.height,
            self.frame_buffer.width,
            self.frame_buffer.channels,
        )
        face_detected, face_reason = self._detect_face(frame)
        hand_detected, hand_reason = self._detect_hand(frame)
        near_detected, near_reason = self._detect_near_object(frame)
        motion_detected, motion_reason = self._detect_motion(frame)
        if motion_detected and time.monotonic() < self._motion_suppressed_until:
            motion_detected = False
            motion_reason = ""

        model_detected = False
        model_reason = ""
        run_model = self._model_enabled() and (
            sequence % self._model_every_n_frames == 0
            or motion_detected
        )
        if run_model:
            model_detected, model_reason = self._detect_model_person(frame)

        person_detected = near_detected or model_detected
        human_score = self._human_score(face_detected, hand_detected, person_detected, motion_detected)
        reasons = [reason for reason in (face_reason, hand_reason, near_reason, model_reason, motion_reason) if reason]
        if human_score >= self._human_score_threshold:
            logger.info("Human presence score=%.2f signals face=%s hand=%s person=%s motion=%s", human_score, face_detected, hand_detected, person_detected, motion_detected)
        return face_detected, hand_detected, person_detected, motion_detected, human_score, "; ".join(reasons)

    @staticmethod
    def _human_score(face_detected, hand_detected, person_detected, motion_detected):
        score = 0.0
        if person_detected:
            score += 0.7
        if motion_detected:
            score += 0.4
        return score

    def _detect_model_person(self, frame):
        resized = cv2.resize(frame, (self._input_width, self._input_height), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        input_tensor = self._prepare_input(rgb)

        self._interpreter.set_tensor(self._input_details[0]["index"], input_tensor)
        self._interpreter.invoke()

        scores, classes, boxes = self._read_detection_outputs()
        self._maybe_log_top_detection(scores, classes, boxes)
        best_person_score = 0.0
        for index, (score, class_id) in enumerate(zip(scores, classes)):
            if int(class_id) not in self._person_class_ids:
                continue
            if not self._box_area_is_valid(boxes, index):
                continue
            best_person_score = max(best_person_score, float(score))

        alpha = self._confidence_smoothing_alpha
        self._person_confidence_ema = alpha * best_person_score + (1.0 - alpha) * self._person_confidence_ema
        if self._person_confidence_ema >= self.confidence_threshold:
            return True, f"person_model score={best_person_score:.2f} smooth={self._person_confidence_ema:.2f}"
        return False, ""

    def _detect_face(self, frame):
        if not self._face_enabled or self._face_cascade is None:
            return False, ""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape[:2]
        scale_width = 320
        scale = scale_width / float(width) if width > scale_width else 1.0
        small = cv2.resize(gray, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA) if scale < 1.0 else gray
        faces = self._face_cascade.detectMultiScale(
            small,
            scaleFactor=1.1,
            minNeighbors=4,
            minSize=(24, 24),
        )
        if len(faces) == 0:
            return False, ""
        frame_area = float(small.shape[0] * small.shape[1])
        largest = max((w * h for (_x, _y, w, h) in faces), default=0) / frame_area
        if largest >= self._face_min_area:
            return True, f"face area={largest:.3f}"
        return False, ""

    def _detect_hand(self, frame):
        if not self._hand_enabled:
            return False, ""
        height, width = frame.shape[:2]
        scale_width = 320
        scale = scale_width / float(width) if width > scale_width else 1.0
        small = cv2.resize(frame, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA) if scale < 1.0 else frame
        ycrcb = cv2.cvtColor(small, cv2.COLOR_BGR2YCrCb)
        lower = np.array([0, 133, 77], dtype=np.uint8)
        upper = np.array([255, 173, 127], dtype=np.uint8)
        mask = cv2.inRange(ycrcb, lower, upper)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), dtype=np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), dtype=np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return False, ""
        area = max(float(cv2.contourArea(contour)) for contour in contours) / float(mask.size)
        if area >= self._hand_min_area:
            return True, f"hand area={area:.3f}"
        return False, ""

    def _detect_near_object(self, frame):
        if not self._near_object_enabled:
            return False, ""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        left, top, right, bottom = self._near_roi
        height, width = gray.shape[:2]
        x1 = int(width * left)
        y1 = int(height * top)
        x2 = max(x1 + 1, int(width * right))
        y2 = max(y1 + 1, int(height * bottom))
        roi = gray[y1:y2, x1:x2]
        small = cv2.resize(roi, (120, 90), interpolation=cv2.INTER_AREA)
        brightness = float(np.mean(small))

        if self._near_baseline_gray is None:
            self._near_baseline_gray = small.astype(np.float32)
            self._near_baseline_brightness = brightness
            return False, ""

        baseline_u8 = cv2.convertScaleAbs(self._near_baseline_gray)
        delta = cv2.absdiff(small, baseline_u8)
        changed_fraction = float(np.mean(delta > 45))
        brightness_delta = abs(brightness - float(self._near_baseline_brightness or brightness))
        edges = cv2.Canny(small, 80, 160)
        edge_density = float(np.count_nonzero(edges)) / float(edges.size)

        near_detected = (
            changed_fraction >= self._near_change_threshold
            and brightness_delta >= self._near_brightness_delta
            and edge_density >= self._near_edge_density_min
        )
        if not near_detected and self._baseline_learning_rate > 0:
            cv2.accumulateWeighted(small.astype(np.float32), self._near_baseline_gray, self._baseline_learning_rate)
            self._near_baseline_brightness = (
                (1.0 - self._baseline_learning_rate) * float(self._near_baseline_brightness or brightness)
                + self._baseline_learning_rate * brightness
            )
        if near_detected:
            return True, (
                f"near_object change={changed_fraction:.2f} "
                f"brightness_delta={brightness_delta:.1f} edges={edge_density:.4f} roi={self._near_roi}"
            )
        return False, ""

    def _detect_motion(self, frame):
        if not self._motion_enabled:
            return False, ""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        left, top, right, bottom = self._motion_roi
        height, width = gray.shape[:2]
        x1 = int(width * left)
        y1 = int(height * top)
        x2 = max(x1 + 1, int(width * right))
        y2 = max(y1 + 1, int(height * bottom))
        roi = gray[y1:y2, x1:x2]
        small = cv2.resize(roi, (160, 120), interpolation=cv2.INTER_AREA)
        blurred = cv2.GaussianBlur(small, (5, 5), 0)

        if self._motion_baseline_gray is None:
            self._motion_baseline_gray = blurred.astype(np.float32)
            if self._motion_subtractor is not None:
                self._motion_subtractor.apply(blurred)
            return False, ""

        baseline_u8 = cv2.convertScaleAbs(self._motion_baseline_gray)
        delta = cv2.absdiff(blurred, baseline_u8)
        _, mask = cv2.threshold(delta, self._motion_pixel_delta, 255, cv2.THRESH_BINARY)
        if self._motion_subtractor is not None:
            fg_mask = self._motion_subtractor.apply(blurred, learningRate=0.01)
            _, fg_mask = cv2.threshold(fg_mask, 180, 255, cv2.THRESH_BINARY)
            mask = cv2.bitwise_or(mask, fg_mask)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), dtype=np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), dtype=np.uint8))
        mask = cv2.dilate(mask, None, iterations=2)

        changed_fraction = float(np.count_nonzero(mask)) / float(mask.size)
        largest_area = 0.0
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            largest_contour = max(contours, key=cv2.contourArea)
            largest_area = float(cv2.contourArea(largest_contour)) / float(mask.size)
            x, y, w, h = cv2.boundingRect(largest_contour)
            self._last_motion_roi = (x / mask.shape[1], y / mask.shape[0], (x + w) / mask.shape[1], (y + h) / mask.shape[0])
        else:
            self._last_motion_roi = None

        motion_detected = (
            changed_fraction >= self._motion_threshold
            and largest_area >= self._motion_min_contour_area
        )
        if motion_detected and self._motion_rebaseline_seconds > 0:
            now = time.monotonic()
            if self._motion_candidate_started_at is None:
                self._motion_candidate_started_at = now
            elif now - self._motion_candidate_started_at >= self._motion_rebaseline_seconds:
                self._motion_baseline_gray = blurred.astype(np.float32)
                self._motion_candidate_started_at = None
                return False, ""
        else:
            self._motion_candidate_started_at = None

        if not motion_detected and self._baseline_learning_rate > 0:
            cv2.accumulateWeighted(
                blurred.astype(np.float32),
                self._motion_baseline_gray,
                self._baseline_learning_rate,
            )
        if motion_detected:
            return True, (
                f"body_motion change={changed_fraction:.3f} "
                f"largest_area={largest_area:.3f} roi={self._motion_roi}"
            )
        return False, ""

    def _reset_presence_baselines(self):
        self._near_baseline_gray = None
        self._near_baseline_brightness = None
        self._motion_baseline_gray = None
        self._motion_subtractor = cv2.createBackgroundSubtractorMOG2(history=80, varThreshold=20, detectShadows=False) if cv2 is not None else None
        self._last_motion_roi = None
        self._motion_candidate_started_at = None
        self._motion_suppressed_until = 0.0

    def _clear_person_state(self):
        if self.detection_state_manager is not None:
            self.detection_state_manager.clear_presence("internal")
        # detectors do not directly control relays
        self._led_visible = False
        self._detection_streak = 0
        self._clear_streak = 0
        self._motion_streak = 0
        self._motion_clear_streak = 0
        self._face_streak = 0
        self._face_clear_streak = 0
        self._hand_streak = 0
        self._hand_clear_streak = 0
        self._person_active = False
        self._motion_active = False
        self._face_active = False
        self._hand_active = False
        self._last_person_seen_at = 0.0
        self._last_motion_seen_at = 0.0
        self._reset_presence_baselines()

    def _prepare_input(self, rgb_frame):
        tensor = np.expand_dims(rgb_frame, axis=0)
        if self._input_dtype == np.float32:
            return (np.float32(tensor) - 127.5) / 127.5

        if self._input_dtype == np.int8:
            scale, zero_point = self._input_details[0].get("quantization", (1.0, 0))
            if scale:
                tensor = np.round(np.float32(tensor) / scale + zero_point)
            return np.clip(tensor, -128, 127).astype(np.int8)

        return tensor.astype(self._input_dtype)

    def _read_detection_outputs(self):
        outputs = [self._interpreter.get_tensor(detail["index"]) for detail in self._output_details]
        squeezed = [np.squeeze(output) for output in outputs]
        output_names = [str(detail.get("name", "")).lower() for detail in self._output_details]

        count = None
        boxes = None
        for output, name in zip(squeezed, output_names):
            if "num" in name and output.size:
                count = int(np.ravel(output)[0])
                break

        scores = None
        classes = None
        for output, name in zip(squeezed, output_names):
            flattened = np.ravel(output)
            if not flattened.size:
                continue
            if "score" in name:
                scores = flattened
            elif "class" in name:
                classes = flattened
            elif "box" in name and output.size:
                boxes = np.reshape(output, (-1, 4))

        if scores is not None and classes is not None:
            return self._normalize_detection_vectors(scores, classes, boxes, count)

        if boxes is None and squeezed:
            first = np.asarray(squeezed[0])
            if first.size and first.shape[-1] == 4:
                boxes = np.reshape(first, (-1, 4))

        scores = next(
            (
                np.ravel(output)
                for output in squeezed
                if np.ravel(output).shape[0] > 1
                and np.issubdtype(output.dtype, np.floating)
                and np.ravel(output).size
                and float(np.nanmax(np.ravel(output))) <= 1.0
            ),
            None,
        )
        classes = next(
            (
                np.ravel(output)
                for output in squeezed
                if np.ravel(output).shape[0] > 1
                and output is not scores
                and np.ravel(output).size
                and float(np.nanmax(np.ravel(output))) > 1.0
            ),
            None,
        )

        if scores is None or classes is None:
            # Typical SSD order is boxes, classes, scores, count.
            classes = np.ravel(squeezed[1]) if len(squeezed) > 1 else np.array([], dtype=np.float32)
            scores = np.ravel(squeezed[2]) if len(squeezed) > 2 else np.array([], dtype=np.float32)
            if count is None and len(squeezed) > 3 and np.ravel(squeezed[3]).size:
                count = int(np.ravel(squeezed[3])[0])
        if scores is None:
            scores = np.array([], dtype=np.float32)
        if classes is None:
            classes = np.array([], dtype=np.float32)
        return self._normalize_detection_vectors(scores, classes, boxes, count)

    def _normalize_detection_vectors(self, scores, classes, boxes=None, count=None):
        scores = np.ravel(scores).astype(np.float32, copy=False)
        classes = np.ravel(classes).astype(np.float32, copy=False)
        length = min(scores.size, classes.size)
        if boxes is not None:
            boxes = np.reshape(boxes, (-1, 4)).astype(np.float32, copy=False)
            length = min(length, boxes.shape[0])
        if count is not None:
            length = min(length, max(0, int(count)))
        if boxes is None:
            boxes = np.empty((0, 4), dtype=np.float32)
        return scores[:length], classes[:length], boxes[:length]

    def _maybe_log_top_detection(self, scores, classes, boxes):
        if self._top_detection_log_seconds <= 0 or scores.size == 0 or classes.size == 0:
            return
        now = time.monotonic()
        if now - self._last_top_detection_log_at < self._top_detection_log_seconds:
            return
        self._last_top_detection_log_at = now
        best_index = int(np.argmax(scores))
        area = self._box_area(boxes[best_index]) if best_index < len(boxes) else None
        logger.info(
            "Person detector top detection: class=%s score=%.2f threshold=%.2f area=%s person_class_ids=%s",
            int(classes[best_index]),
            float(scores[best_index]),
            self.confidence_threshold,
            f"{area:.3f}" if area is not None else "unknown",
            sorted(self._person_class_ids),
        )

    def _box_area_is_valid(self, boxes, index):
        if boxes is None or index >= len(boxes):
            return True
        area = self._box_area(boxes[index])
        if area is None:
            return True
        return self._min_box_area <= area <= self._max_box_area

    @staticmethod
    def _box_area(box):
        if box is None or len(box) != 4:
            return None
        y_min, x_min, y_max, x_max = [float(value) for value in box]
        width = max(0.0, min(1.0, x_max) - max(0.0, x_min))
        height = max(0.0, min(1.0, y_max) - max(0.0, y_min))
        area = width * height
        return area if area > 0 else None

    def _update_led_state(
        self,
        detected,
        reason="",
        *,
        face_detected=None,
        hand_detected=None,
        person_detected=None,
        motion_detected=None,
        human_score=0.0,
    ):
        now = time.monotonic()
        if face_detected is None:
            face_detected = False
        if hand_detected is None:
            hand_detected = False
        if person_detected is None:
            person_detected = bool(detected)
        if motion_detected is None:
            motion_detected = bool(detected)

        # face debouncing (prevents flicker from Haar false positives)
        if face_detected:
            self._face_streak += 1
            self._face_clear_streak = 0
            if not self._face_active and self._face_streak >= self._face_trigger_frames:
                self._face_active = True
                logger.info("Face detected on internal camera: %s", reason or "face")
        else:
            self._face_clear_streak += 1
            self._face_streak = 0
            if self._face_active and self._face_clear_streak >= self._face_clear_frames:
                self._face_active = False
                logger.info("Face detection cleared after consecutive misses")

        # hand debouncing (color blob can flicker)
        if hand_detected:
            self._hand_streak += 1
            self._hand_clear_streak = 0
            if not self._hand_active and self._hand_streak >= self._hand_trigger_frames:
                self._hand_active = True
                logger.info("Hand detected on internal camera: %s", reason or "hand")
        else:
            self._hand_clear_streak += 1
            self._hand_streak = 0
            if self._hand_active and self._hand_clear_streak >= self._hand_clear_frames:
                self._hand_active = False
                logger.info("Hand detection cleared after consecutive misses")

        if person_detected:
            self._last_person_seen_at = now
            self._detection_streak += 1
            self._clear_streak = 0
            if not self._person_active and self._detection_streak >= self._required_detection_frames:
                self._person_active = True
                logger.info("Person detected on internal camera: %s", reason or "person")
        else:
            self._clear_streak += 1
            self._detection_streak = 0
            clear_age = now - self._last_person_seen_at if self._last_person_seen_at else 0.0
            if (
                self._person_active
                and self._clear_streak >= self._required_clear_frames
                and clear_age > self._clear_seconds
            ):
                self._person_active = False
                logger.info("Person detection cleared after %.2fs", clear_age)

        if motion_detected:
            self._last_motion_seen_at = now
            self._motion_streak += 1
            self._motion_clear_streak = 0
            if not self._motion_active and self._motion_streak >= self._motion_trigger_frames:
                self._motion_active = True
                logger.info("Body movement detected on internal camera: %s", reason or "body_motion")
        else:
            self._motion_clear_streak += 1
            self._motion_streak = 0
            clear_age = now - self._last_motion_seen_at if self._last_motion_seen_at else 0.0
            if (
                self._motion_active
                and self._motion_clear_streak >= self._motion_clear_frames
                and clear_age > self._clear_seconds
            ):
                self._motion_active = False
                self._motion_suppressed_until = now + self._motion_retrigger_cooldown_seconds
                logger.info("Motion detection cleared after %.2fs", clear_age)

        # Only person and motion are valid security triggers for the internal camera.
        relay_active = self._person_active or self._motion_active
        if self.detection_state_manager is not None:
            self.detection_state_manager.update_presence(
                "internal",
                face_detected=self._face_active,
                hand_detected=self._hand_active,
                person_detected=self._person_active,
                motion_detected=self._motion_active,
                human_score=human_score,
                reason=reason if relay_active else "",
            )
            if not relay_active:
                self.detection_state_manager.check_timeouts()
        # detectors never directly drive security relays; DetectionStateManager is authoritative

        self._led_visible = relay_active

    def _log_fps(self):
        self._processed_frames += 1
        now = time.monotonic()
        elapsed = now - self._fps_window_started_at
        if elapsed < 10:
            return
        logger.info("Person detection FPS %.2f", self._processed_frames / elapsed)
        self._processed_frames = 0
        self._fps_window_started_at = now

    def _load_labels(self):
        if not self.labels_path.exists():
            return []
        return [line.strip() for line in self.labels_path.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def _label_ids_for_person(labels):
        person_ids = {index for index, label in enumerate(labels) if label.strip().lower() == "person"}
        # Some SSD MobileNet TFLite exports include a background label, while others return 0-based COCO ids.
        person_ids.update(index - 1 for index in list(person_ids) if index > 0)
        return person_ids or {0, 1}

    @staticmethod
    def _resolve_model_path(model_path):
        env_path = os.environ.get("PERSON_DETECTOR_MODEL_PATH", "").strip()
        if env_path:
            return Path(env_path)
        path = Path(model_path)
        if path.exists():
            return path
        if ALT_MODEL_PATH.exists():
            return ALT_MODEL_PATH
        return path

    @staticmethod
    def _load_face_cascade():
        if cv2 is None:
            return None
        cascade_candidates = []
        data_module = getattr(cv2, "data", None)
        haar_dir = getattr(data_module, "haarcascades", "") if data_module is not None else ""
        if haar_dir:
            cascade_candidates.append(Path(haar_dir) / "haarcascade_frontalface_default.xml")
        cascade_candidates.extend(
            [
                Path("/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"),
                Path("/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml"),
                Path("/usr/local/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"),
            ]
        )
        for cascade_path in cascade_candidates:
            if not cascade_path.exists():
                continue
            cascade = cv2.CascadeClassifier(str(cascade_path))
            if not cascade.empty():
                logger.info("Face cascade loaded from %s", cascade_path)
                return cascade
        logger.warning("Face cascade not found; face signal disabled")
        return None

    def _install_missing_model(self):
        if os.environ.get("PERSON_DETECTOR_AUTO_INSTALL_MODEL", "true").strip().lower() in {"0", "false", "no"}:
            return
        if not MODEL_INSTALLER.exists():
            logger.warning("Person detector model installer not found: %s", MODEL_INSTALLER)
            return

        logger.info("Person detector model missing; running installer %s", MODEL_INSTALLER)
        try:
            result = subprocess.run(
                [str(MODEL_INSTALLER)],
                cwd=str(PROJECT_DIR),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except Exception as exc:
            logger.warning("Person detector model installer failed to run: %s", exc)
            return

        if result.stdout.strip():
            logger.info("Model installer output: %s", result.stdout.strip().replace("\n", " | "))
        if result.stderr.strip():
            logger.warning("Model installer stderr: %s", result.stderr.strip().replace("\n", " | "))
        if result.returncode != 0:
            logger.warning("Person detector model installer exited with status %s", result.returncode)
