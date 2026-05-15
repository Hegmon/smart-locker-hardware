from pathlib import Path
import os
import subprocess
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

DETECTION_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_PATH = DETECTION_DIR / "models" / "detect.tflite"
ALT_MODEL_PATH = DETECTION_DIR / "models" / "model.tflite"
DEFAULT_LABELS_PATH = DETECTION_DIR / "labels.txt"
PROJECT_DIR = DETECTION_DIR.parents[2]
MODEL_INSTALLER = PROJECT_DIR / "app" / "scripts" / "install_detection_model.sh"


class PersonDetector:
    """Run lightweight person detection from the streaming agent's shared frame buffer."""

    def __init__(
        self,
        frame_buffer,
        *,
        model_path=DEFAULT_MODEL_PATH,
        labels_path=DEFAULT_LABELS_PATH,
        confidence_threshold=0.5,
        process_every_n_frames=3,
        led_off_delay_seconds=3.0,
    ):
        self.frame_buffer = frame_buffer
        self.model_path = self._resolve_model_path(model_path)
        self.labels_path = Path(labels_path)
        self.confidence_threshold = confidence_threshold
        self.process_every_n_frames = max(1, int(process_every_n_frames))
        self.led_off_delay_seconds = led_off_delay_seconds
        self.led_controller = LedController()

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
        self.led_controller.cleanup()
        logger.info("Person detector stopped")

    def _load_model(self):
        try:
            from tflite_runtime.interpreter import Interpreter
        except Exception as exc:
            raise RuntimeError("tflite-runtime is required for person detection") from exc

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

    def _run(self):
        while self._running:
            frame_bytes, sequence, _ = self.frame_buffer.latest()
            if frame_bytes is None or sequence == self._last_sequence:
                time.sleep(0.01)
                continue

            self._last_sequence = sequence
            if sequence % self.process_every_n_frames != 0:
                self._update_led_state(False)
                continue

            person_detected = False
            try:
                person_detected = self._detect_person(frame_bytes)
            except Exception:
                logger.exception("Person detection failed")

            self._update_led_state(person_detected)
            self._log_fps()

    def _detect_person(self, frame_bytes):
        frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(
            self.frame_buffer.height,
            self.frame_buffer.width,
            self.frame_buffer.channels,
        )
        resized = cv2.resize(frame, (self._input_width, self._input_height), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        input_tensor = self._prepare_input(rgb)

        self._interpreter.set_tensor(self._input_details[0]["index"], input_tensor)
        self._interpreter.invoke()

        scores, classes = self._read_detection_outputs()
        for score, class_id in zip(scores, classes):
            if float(score) >= self.confidence_threshold and int(class_id) in self._person_class_ids:
                return True
        return False

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

        scores = next(
            (
                output
                for output in squeezed
                if output.ndim == 1
                and output.shape[0] > 1
                and np.issubdtype(output.dtype, np.floating)
                and output.size
                and float(np.nanmax(output)) <= 1.0
            ),
            None,
        )
        classes = next(
            (
                output
                for output in squeezed
                if output.ndim == 1
                and output.shape[0] > 1
                and output is not scores
                and output.size
                and float(np.nanmax(output)) > 1.0
            ),
            None,
        )

        if scores is None or classes is None:
            # Typical SSD order is boxes, classes, scores, count.
            classes = squeezed[1] if len(squeezed) > 1 else np.array([], dtype=np.float32)
            scores = squeezed[2] if len(squeezed) > 2 else np.array([], dtype=np.float32)
        return scores, classes

    def _update_led_state(self, person_detected):
        now = time.monotonic()
        if person_detected:
            self._last_person_seen_at = now
            if not self._led_visible:
                logger.info("Person detected; GPIO LEDs ON")
            self.led_controller.set_person_visible(True)
            self._led_visible = True
            return

        if self._last_person_seen_at and now - self._last_person_seen_at >= self.led_off_delay_seconds:
            if self._led_visible:
                logger.info("No person detected for %.1f seconds; GPIO LEDs OFF", self.led_off_delay_seconds)
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
