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

from app.streaming_agent.gpio.relay_controller import RelayController
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)

DETECTION_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_PATH = DETECTION_DIR / "models" / "detect.tflite"
ALT_MODEL_PATH = DETECTION_DIR / "models" / "model.tflite"
DEFAULT_LABELS_PATH = DETECTION_DIR / "labels.txt"
PROJECT_DIR = DETECTION_DIR.parents[2]
MODEL_INSTALLER = PROJECT_DIR / "app" / "scripts" / "install_detection_model.sh"


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
        led_off_delay_seconds=3.0,
        led_controller=None,
    ):
        self.frame_buffer = frame_buffer
        self.model_path = self._resolve_model_path(model_path)
        self.labels_path = Path(labels_path)
        self.confidence_threshold = (
            _env_float("PERSON_DETECTION_CONFIDENCE", 0.55, minimum=0.05, maximum=0.95)
            if confidence_threshold is None
            else float(confidence_threshold)
        )
        self.process_every_n_frames = (
            _env_int("PERSON_DETECTOR_EVERY_N_FRAMES", 2, minimum=1)
            if process_every_n_frames is None
            else max(1, int(process_every_n_frames))
        )
        self._model_every_n_frames = _env_int("PERSON_MODEL_EVERY_N_FRAMES", 3, minimum=1)
        self.led_off_delay_seconds = led_off_delay_seconds
        self._owns_led_controller = led_controller is None
        self.led_controller = led_controller or RelayController()
        self._top_detection_log_seconds = _env_float("PERSON_DETECTOR_LOG_TOP_SECONDS", 10.0, minimum=0.0)
        self._required_detection_frames = _env_int("PERSON_DETECTION_CONFIRM_FRAMES", 2, minimum=1)
        self._required_clear_frames = _env_int("PERSON_DETECTION_CLEAR_FRAMES", 2, minimum=1)
        self._clear_seconds = _env_float("PERSON_DETECTION_CLEAR_SECONDS", 0.0, minimum=0.0)
        self._stale_clear_seconds = _env_float("PERSON_DETECTION_STALE_CLEAR_SECONDS", 1.0, minimum=0.05)
        self._min_box_area = _env_float("PERSON_DETECTION_MIN_BOX_AREA", 0.04, minimum=0.0, maximum=1.0)
        self._max_box_area = _env_float("PERSON_DETECTION_MAX_BOX_AREA", 0.95, minimum=0.01, maximum=1.0)
        self._near_object_enabled = os.getenv("PERSON_NEAR_OBJECT_ENABLED", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._near_change_threshold = _env_float("PERSON_NEAR_CHANGE_THRESHOLD", 0.22, minimum=0.01, maximum=1.0)
        self._near_brightness_delta = _env_float("PERSON_NEAR_BRIGHTNESS_DELTA", 6.0, minimum=0.0, maximum=255.0)
        self._near_edge_density_min = _env_float("PERSON_NEAR_EDGE_DENSITY_MIN", 0.004, minimum=0.0, maximum=1.0)
        self._near_roi = _env_roi("PERSON_NEAR_ROI", "0.10,0.10,0.90,0.90")
        self._baseline_learning_rate = _env_float("PERSON_BASELINE_LEARNING_RATE", 0.01, minimum=0.0, maximum=1.0)
        self._near_baseline_gray = None
        self._near_baseline_brightness = None
        self._motion_enabled = _env_bool("PERSON_MOTION_ENABLED", True)
        self._motion_threshold = _env_float("PERSON_MOTION_THRESHOLD", 0.025, minimum=0.001, maximum=1.0)
        self._motion_min_contour_area = _env_float("PERSON_MOTION_MIN_CONTOUR_AREA", 0.006, minimum=0.0001, maximum=1.0)
        self._motion_pixel_delta = _env_int("PERSON_MOTION_PIXEL_DELTA", 28, minimum=1)
        self._motion_roi = _env_roi("PERSON_MOTION_ROI", "0.05,0.05,0.95,0.95")
        self._motion_baseline_gray = None

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
        self._last_sequence = -1
        self._processed_frames = 0
        self._fps_window_started_at = time.monotonic()
        self._led_visible = False
        self._last_top_detection_log_at = 0.0
        self._detection_streak = 0
        self._clear_streak = 0

    def start(self):
        if self._running:
            return
        if self.frame_buffer is None:
            logger.warning("Person detector disabled: no shared frame buffer available")
            return
        if not self.model_path.exists():
            self._install_missing_model()
            self.model_path = self._resolve_model_path(self.model_path)

        if not self.model_path.exists():
            logger.warning(
                "Person detector disabled: model not found at %s. "
                "Place detect.tflite in app/streaming_agent/detection/models/ "
                "set PERSON_DETECTOR_MODEL_PATH, or run app/scripts/install_detection_model.sh.",
                self.model_path,
            )
            return
        if cv2 is None or np is None:
            logger.warning("Person detector disabled: opencv-python-headless and numpy are required")
            return

        try:
            self._load_model()
        except Exception as exc:
            logger.warning("Person detector disabled: %s", exc)
            return
        self.led_controller.start()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="person-detector")
        self._thread.start()
        logger.info("Person detector started with model %s", self.model_path)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self.led_controller.set_person_visible(False)
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
            reason = ""
            try:
                person_detected, reason = self._detect_person(frame_bytes, sequence)
            except Exception:
                logger.exception("Person detection failed")

            self._update_led_state(person_detected, reason)
            self._log_fps()

    def _clear_stale_led_state(self):
        if not self._led_visible:
            return
        if time.monotonic() - self._last_person_seen_at < self._stale_clear_seconds:
            return
        logger.info("No fresh person detection; GPIO LEDs OFF")
        self.led_controller.set_person_visible(False)
        self._led_visible = False
        self._detection_streak = 0
        self._clear_streak = 0

    def _detect_person(self, frame_bytes, sequence):
        frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(
            self.frame_buffer.height,
            self.frame_buffer.width,
            self.frame_buffer.channels,
        )
        near_detected, near_reason = self._detect_near_object(frame)
        if near_detected:
            return True, near_reason
        motion_detected, motion_reason = self._detect_motion(frame)
        if motion_detected:
            return True, motion_reason

        if sequence % self._model_every_n_frames != 0:
            return False, ""

        resized = cv2.resize(frame, (self._input_width, self._input_height), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        input_tensor = self._prepare_input(rgb)

        self._interpreter.set_tensor(self._input_details[0]["index"], input_tensor)
        self._interpreter.invoke()

        scores, classes, boxes = self._read_detection_outputs()
        self._maybe_log_top_detection(scores, classes, boxes)
        model_detected = False
        for index, (score, class_id) in enumerate(zip(scores, classes)):
            if float(score) >= self.confidence_threshold and int(class_id) in self._person_class_ids:
                if not self._box_area_is_valid(boxes, index):
                    continue
                model_detected = True
                break

        if model_detected:
            return True, "person_model"
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
            return False, ""

        baseline_u8 = cv2.convertScaleAbs(self._motion_baseline_gray)
        delta = cv2.absdiff(blurred, baseline_u8)
        _, mask = cv2.threshold(delta, self._motion_pixel_delta, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), dtype=np.uint8))
        mask = cv2.dilate(mask, None, iterations=2)

        changed_fraction = float(np.count_nonzero(mask)) / float(mask.size)
        largest_area = 0.0
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            largest_area = max(float(cv2.contourArea(contour)) for contour in contours) / float(mask.size)

        motion_detected = (
            changed_fraction >= self._motion_threshold
            and largest_area >= self._motion_min_contour_area
        )
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

    def _update_led_state(self, person_detected, reason=""):
        now = time.monotonic()
        if person_detected:
            self._last_person_seen_at = now
            self._detection_streak += 1
            self._clear_streak = 0
            if self._detection_streak < self._required_detection_frames:
                return
            if not self._led_visible:
                logger.info("Person detected; GPIO LEDs ON: %s", reason or "person")
            self.led_controller.set_person_visible(True)
            self._led_visible = True
            return

        self._clear_streak += 1
        self._detection_streak = 0
        clear_age = now - self._last_person_seen_at
        if (
            self._led_visible
            and self._clear_streak >= self._required_clear_frames
            and clear_age >= self._clear_seconds
        ):
            logger.info("No person detected; GPIO LEDs OFF")
            self.led_controller.set_person_visible(False)
            self._led_visible = False

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
