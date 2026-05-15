from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


class LedController:
    """BCM GPIO LED controller with a no-op fallback for non-Pi environments."""

    def __init__(self, pins=(14, 15)):
        self.pins = tuple(pins)
        self._gpio = None
        self._enabled = False
        self._on = False

    def start(self):
        try:
            import RPi.GPIO as GPIO
        except Exception as exc:
            logger.warning("RPi.GPIO unavailable; person detection LEDs disabled: %s", exc)
            return

        self._gpio = GPIO
        GPIO.setmode(GPIO.BCM)
        for pin in self.pins:
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.LOW)
        self._enabled = True
        logger.info("Person detection LEDs initialized on BCM pins %s", self.pins)

    def set_person_visible(self, visible):
        if not self._enabled or self._gpio is None or self._on == visible:
            return

        state = self._gpio.HIGH if visible else self._gpio.LOW
        for pin in self.pins:
            self._gpio.output(pin, state)
        self._on = visible

    def cleanup(self):
        if not self._enabled or self._gpio is None:
            return

        try:
            self.set_person_visible(False)
            self._gpio.cleanup(self.pins)
        except Exception:
            logger.exception("GPIO cleanup failed")
        finally:
            self._enabled = False
            self._gpio = None
