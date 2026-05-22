import signal
import sys
import threading
import time
import os
try:
    import fcntl
except Exception:
    fcntl = None

from app.core.config import MQTT_HOST, MQTT_PASSWORD, MQTT_PORT, MQTT_USERNAME
from app.streaming_agent.detection import person_detector as person_detector_module
from app.streaming_agent.detection import qr_scanner as qr_scanner_module
from app.streaming_agent.detection.person_detector import PersonDetector
from app.streaming_agent.detection.qr_scanner import BackendQRValidator, QrScanner, summarize_qr_value
from app.streaming_agent.detection.scanner_config import QRScannerConfig
from app.streaming_agent.detection.tamper_detection import TamperDetection
from app.streaming_agent.gpio.relay_controller import RelayController
from app.streaming_agent.health_monitor import HealthMonitor
from app.streaming_agent.hot_plug_monitor import HotPlugMonitor
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager
from app.streaming_agent.mqtt_publisher import MQTTPublisher
from app.streaming_agent.streaming_manager import StreamingManager


logger = LoggingManager.get_logger(__name__)

DETECTION_LED_HOLD_SECONDS = float(os.getenv("DETECTION_LED_HOLD_SECONDS", "2.5"))


class StreamingAgent:
    def __init__(self):
        self.stream_manager = None
        self.health_monitor = None
        self.hot_plug_monitor = None
        self.mqtt_publisher = None
        self.person_detector = None
        self.qr_scanner = None
        self.qr_scanner_config = QRScannerConfig.from_env()
        self.tamper_detectors = []
        self.relay_controller = RelayController()
        self.keyboard_thread = None
        self.running = False
        self._stopping = False
        self._stop_lock = threading.Lock()
        self._lock_file = None

    def initialize(self):
        logger.info("Initializing streaming agent")
        self._log_detection_runtime_diagnostics()
        self._acquire_single_instance_lock()
        self.stream_manager = StreamingManager()
        self.stream_manager.initialize()
        self.person_detector = PersonDetector(
            self.stream_manager.get_frame_buffer("internal"),
            led_controller=self.relay_controller,
            led_off_delay_seconds=DETECTION_LED_HOLD_SECONDS,
        )
        self.qr_scanner = QrScanner(
            self.stream_manager.get_frame_buffer("external"),
            video_device=self.stream_manager.get_camera_device("external"),
            camera_controls=self.stream_manager.camera_controls,
            config=self.qr_scanner_config,
            gpio_controller=self.relay_controller,
            on_qr_detected=self._handle_qr_detected,
            backend_validator=self._validate_qr_with_backend,
        )
        self.tamper_detectors = []
        for role, frame_buffer in self.stream_manager.frame_buffers.items():
            if role != "external":
                logger.info("Skipping tamper detector for %s camera; tamper relay is external-camera only", role)
                continue
            if role == "external" and os.getenv("EXTERNAL_TAMPER_DETECTION_ENABLED", "true").strip().lower() not in {
                "1",
                "true",
                "yes",
                "on",
            }:
                logger.info("External tamper detection disabled; external camera is dedicated to QR scanning")
                continue
            skip_when = self.qr_scanner.is_qr_attention_active if role == "external" and self.qr_scanner else None
            self.tamper_detectors.append(
                TamperDetection(
                    frame_buffer,
                    camera_role=role,
                    led_controller=self.relay_controller,
                    skip_when=skip_when,
                )
            )
        self.health_monitor = HealthMonitor(stream_registry=self.stream_manager.streams)
        self.hot_plug_monitor = HotPlugMonitor(stream_manager=self.stream_manager)
        self.mqtt_publisher = MQTTPublisher(
            stream_manager=self.stream_manager,
            health_monitor=self.health_monitor,
            broker_host="69.62.125.223",
            broker_port=1883,
            username=MQTT_USERNAME,
            password=MQTT_PASSWORD,
        )
        logger.info("Streaming agent initialized successfully")

    def _log_detection_runtime_diagnostics(self):
        model_path = PersonDetector._resolve_model_path(person_detector_module.DEFAULT_MODEL_PATH)
        logger.info(
            "Detection runtime diagnostics: cv2=%s numpy=%s qr_cv2=%s qr_numpy=%s model_exists=%s model_path=%s",
            bool(person_detector_module.cv2),
            bool(person_detector_module.np),
            bool(qr_scanner_module.cv2),
            bool(qr_scanner_module.np),
            model_path.exists(),
            model_path,
        )
        try:
            import RPi.GPIO  # noqa: F401

            gpio_status = "RPi.GPIO"
        except Exception as rpi_exc:
            try:
                import lgpio  # noqa: F401

                gpio_status = "lgpio"
            except Exception as lgpio_exc:
                gpio_status = f"unavailable RPi.GPIO={rpi_exc} lgpio={lgpio_exc}"
        logger.info("Detection GPIO diagnostics: %s", gpio_status)
        try:
            from ai_edge_litert.interpreter import Interpreter  # noqa: F401

            logger.info("Detection TFLite runtime diagnostics: ai-edge-litert available")
        except Exception as litert_exc:
            try:
                from tflite_runtime.interpreter import Interpreter  # noqa: F401

                logger.info("Detection TFLite runtime diagnostics: tflite-runtime available")
            except Exception as tflite_exc:
                logger.warning(
                    "Detection TFLite runtime unavailable: ai-edge-litert=%s tflite-runtime=%s",
                    litert_exc,
                    tflite_exc,
                )

    def _handle_qr_detected(self, payload):
        """One-time scan event hook for telemetry, MQTT fanout, or local audit actions."""
        logger.info("QR scan event received: payload_keys=%s", sorted(payload.keys()))

    def _validate_qr_with_backend(self, payload):
        """Backend validation hook. JWTs are not trusted locally and are verified remotely."""
        logger.info("Validating QR payload with backend: %s", summarize_qr_value(str(payload.get("token") or payload)))
        return BackendQRValidator(self.qr_scanner_config)(payload)

    def _acquire_single_instance_lock(self):
        if fcntl is None:
            logger.warning("Single-instance lock unavailable on this platform")
            return
        lock_path = os.getenv("STREAMING_AGENT_LOCK_FILE", "/tmp/smartlocker-streaming-agent.lock")
        self._lock_file = open(lock_path, "w", encoding="utf-8")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Another streaming agent instance is already running. "
                "Stop the existing process/service before starting a new one, otherwise cameras show 'Device or resource busy'."
            ) from exc
        self._lock_file.write(str(os.getpid()))
        self._lock_file.flush()

    def start(self):
        logger.info("Starting streaming agent")
        self.running = True
        self.stream_manager.start_all()
        self.relay_controller.start()
        if self.person_detector:
            try:
                self.person_detector.start()
                logger.info("Person detector running=%s", getattr(self.person_detector, "_running", False))
            except Exception:
                logger.exception("Person detector startup failed")
        if self.qr_scanner:
            try:
                self.qr_scanner.start()
                logger.info("QR scanner running=%s", getattr(self.qr_scanner, "_running", False))
            except Exception:
                logger.exception("QR scanner startup failed")
        for tamper_detector in self.tamper_detectors:
            try:
                tamper_detector.start()
                logger.info(
                    "Tamper detector running=%s camera=%s",
                    getattr(tamper_detector, "_running", False),
                    getattr(tamper_detector, "camera_role", "unknown"),
                )
            except Exception:
                logger.exception("Tamper detector startup failed for %s", getattr(tamper_detector, "camera_role", "unknown"))
        self.health_monitor.start()
        self.mqtt_publisher.start()
        self.hot_plug_monitor.start()
        self._start_keyboard_listener()
        logger.info("Streaming agent started successfully")
        logger.info("Press Ctrl+C or type q then Enter to stop the streaming agent")

    def stop(self):
        with self._stop_lock:
            if self._stopping:
                logger.info("Streaming agent stop already in progress")
                return
            self._stopping = True
            self.running = False

        logger.info("Stopping streaming agent")
        try:
            if self.hot_plug_monitor:
                self.hot_plug_monitor.stop()
            if self.mqtt_publisher:
                self.mqtt_publisher.stop()
            if self.health_monitor:
                self.health_monitor.stop()
            if self.person_detector:
                self.person_detector.stop()
            for tamper_detector in self.tamper_detectors:
                tamper_detector.stop()
            if self.qr_scanner:
                self.qr_scanner.stop()
            self.relay_controller.cleanup()
            if self.stream_manager:
                self.stream_manager.stop_all()
            if self._lock_file:
                self._lock_file.close()
                self._lock_file = None
        finally:
            logger.info("Streaming agent stopped successfully")

    def run_forever(self):
        self.initialize()
        self.start()

        while self.running:
            time.sleep(1)

    def _start_keyboard_listener(self):
        if not sys.stdin or not sys.stdin.isatty() or self.keyboard_thread:
            return

        self.keyboard_thread = threading.Thread(
            target=self._keyboard_loop,
            daemon=True,
            name="streaming-keyboard-listener",
        )
        self.keyboard_thread.start()

    def _keyboard_loop(self):
        while self.running:
            try:
                command = sys.stdin.readline()
            except Exception:
                return
            if not command:
                return
            if command.strip().lower() in {"q", "quit", "exit", "stop"}:
                logger.info("Keyboard stop requested")
                self.stop()
                return


agent = StreamingAgent()


def signal_handler(sig, frame):
    logger.info("Signal received: %s", sig)
    agent.stop()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, stopping agent")
        agent.stop()
        sys.exit(0)
    except Exception:
        logger.exception("Unexpected streaming agent error")
        agent.stop()
        sys.exit(1)
