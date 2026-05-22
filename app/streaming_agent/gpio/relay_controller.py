import os
import threading
import time

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager
from app.utils.python_path import add_system_dist_packages


add_system_dist_packages()


logger = LoggingManager.get_logger(__name__)

RED_LED_PIN = 21
GREEN_LED_PIN = 20
LOCKER_PIN = 16
BUZZER_PIN = 12
RELAY_INPUTS = {
    "IN1": "Red LED",
    "IN2": "Green LED",
    "IN3": "Locker relay",
    "IN4": "Buzzer",
}

QR_SUCCESS_UNLOCK_TIME = 5
ALERT_DURATION = 15
DETECTION_SOURCE_TTL_SECONDS = 2.5


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name, default, minimum=None):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return int(default)


class RelayController:
    """Thread-safe BCM GPIO controller for the four-channel relay board.

    Most Raspberry Pi relay boards used with IN1-IN4 are active-low. The
    default follows that wiring and can be overridden with RELAY_ACTIVE_LOW.
    """

    def __init__(
        self,
        *,
        red_led_pin=RED_LED_PIN,
        green_led_pin=GREEN_LED_PIN,
        locker_pin=LOCKER_PIN,
        buzzer_pin=BUZZER_PIN,
        active_low=None,
        unlock_seconds=None,
        alert_duration=None,
    ):
        self.red_led_pin = _env_int("RELAY_RED_LED_PIN", red_led_pin)
        self.green_led_pin = _env_int("RELAY_GREEN_LED_PIN", green_led_pin)
        self.locker_pin = _env_int("RELAY_LOCKER_PIN", locker_pin)
        self.buzzer_pin = _env_int("RELAY_BUZZER_PIN", buzzer_pin)
        self.active_low = _env_bool("RELAY_ACTIVE_LOW", True) if active_low is None else bool(active_low)
        self.unlock_seconds = _env_float("QR_SUCCESS_UNLOCK_TIME", QR_SUCCESS_UNLOCK_TIME, minimum=0.1)
        self.alert_duration = _env_float("ALERT_DURATION", ALERT_DURATION, minimum=0.1)
        if unlock_seconds is not None:
            self.unlock_seconds = max(0.1, float(unlock_seconds))
        if alert_duration is not None:
            self.alert_duration = max(0.1, float(alert_duration))

        self._gpio = None
        self._enabled = False
        self._lock = threading.RLock()
        self._red_sources = set()
        self._buzzer_sources = set()
        self._alert_until = {}
        self._alert_threads = set()
        self._detection_source_until = {}
        self._detection_expiry_thread = None
        self._qr_success_running = False
        self._red_on = False
        self._buzzer_on = False
        self._green_on = False
        self._locker_unlocked = False

    @property
    def pins(self):
        return (self.red_led_pin, self.green_led_pin, self.locker_pin, self.buzzer_pin)

    @property
    def success_pin(self):
        return self.green_led_pin

    @property
    def failure_pin(self):
        return self.red_led_pin

    def start(self):
        with self._lock:
            if self._enabled:
                return
            gpio_source = "RPi.GPIO"
            try:
                import RPi.GPIO as GPIO
            except Exception as exc:
                try:
                    GPIO = _LgpioCompat()
                    gpio_source = "lgpio"
                except Exception as lgpio_exc:
                    logger.warning("GPIO unavailable; relay actions disabled: RPi.GPIO=%s lgpio=%s", exc, lgpio_exc)
                    return

            try:
                self._configure_gpio(GPIO)
            except Exception as gpio_exc:
                logger.warning("%s GPIO setup failed, trying lgpio fallback: %s", gpio_source, gpio_exc)
                self._cleanup_gpio_object(GPIO)
                try:
                    GPIO = _LgpioCompat()
                    gpio_source = "lgpio"
                    self._configure_gpio(GPIO)
                except Exception as lgpio_exc:
                    logger.warning("GPIO unavailable; relay actions disabled: %s", lgpio_exc)
                    self._gpio = None
                    return

            self._enabled = True
            self._red_sources.clear()
            self._buzzer_sources.clear()
            self._detection_source_until.clear()
            self._red_on = False
            self._buzzer_on = False
            self._green_on = False
            self._locker_unlocked = False
            self.lock_locker()
            self.red_led_off()
            self.green_led_off()
            self.buzzer_off()
            logger.info(
                "Relay controller initialized in BCM mode: IN1 red=%s IN2 green=%s IN3 locker=%s IN4 buzzer=%s active_low=%s",
                self.red_led_pin,
                self.green_led_pin,
                self.locker_pin,
                self.buzzer_pin,
                self.active_low,
            )
            logger.info(
                "Relay mapping: IN1->GPIO%s red, IN2->GPIO%s green, IN3->GPIO%s locker, IN4->GPIO%s buzzer",
                self.red_led_pin,
                self.green_led_pin,
                self.locker_pin,
                self.buzzer_pin,
            )
            logger.info("Relay GPIO backend: %s", gpio_source)

    def red_led_on(self):
        self._set_red_source("manual", True)

    def red_led_off(self):
        self._set_red_source("manual", False)

    def green_led_on(self):
        with self._lock:
            if self._green_on:
                return
            self._green_on = True
            self._write(self.green_led_pin, True, "Green LED")

    def green_led_off(self):
        with self._lock:
            if not self._green_on:
                self._write(self.green_led_pin, False, "Green LED")
                return
            self._green_on = False
            self._write(self.green_led_pin, False, "Green LED")

    def buzzer_on(self):
        self._set_buzzer_source("manual", True)

    def buzzer_off(self):
        self._set_buzzer_source("manual", False)

    def unlock_locker(self):
        with self._lock:
            if self._locker_unlocked:
                return
            self._locker_unlocked = True
            self._write(self.locker_pin, True, "Locker relay")
            logger.info("Locker unlocked")

    def lock_locker(self):
        with self._lock:
            was_unlocked = self._locker_unlocked
            self._locker_unlocked = False
            self._write(self.locker_pin, False, "Locker relay")
            if was_unlocked:
                logger.info("Locker locked")
            else:
                logger.info("Locker locked/default state confirmed")

    def set_person_visible(self, visible):
        source = "person"
        if visible:
            changed = self._set_detection_source(source, True, red=True, buzzer=False)
            if changed:
                logger.info("Person/body movement detection start; Relay 1 ON while detection is active")
        else:
            changed = self._set_detection_source(source, False, red=True, buzzer=False)
            if changed:
                logger.info("Person/body movement detection end; Relay 1 OFF")

    def set_tamper_active(self, camera_role, active):
        source = f"tamper:{camera_role}"
        if active:
            changed = self._set_detection_source(source, True, red=False, buzzer=True)
            if changed:
                logger.warning("Tamper detection start on %s camera; Relay 4 ON while tamper is active", camera_role)
        else:
            changed = self._set_detection_source(source, False, red=False, buzzer=True)
            if changed:
                logger.info("Tamper detection end on %s camera; Relay 4 OFF", camera_role)

    def trigger_tamper_alert(self, camera_role="camera"):
        self.trigger_alert(f"tamper:{camera_role}", self.alert_duration, log_name="Tamper detected")

    def qr_success(self, duration_seconds=None):
        duration = self.unlock_seconds if duration_seconds is None else max(0.1, float(duration_seconds))
        with self._lock:
            if self._qr_success_running:
                logger.info("QR success ignored because locker unlock cycle is already running")
                return
            self._qr_success_running = True

        threading.Thread(
            target=self._qr_success_worker,
            args=(duration,),
            daemon=True,
            name="relay-qr-success",
        ).start()

    def qr_failure(self):
        logger.warning("QR failure")
        self.trigger_alert("qr_failure", self.alert_duration, log_name="QR failure")

    def pulse_success(self, duration_seconds=None):
        self.qr_success(duration_seconds)

    def pulse_failure(self):
        self.qr_failure()

    def trigger_alert(self, source, duration_seconds=None, *, log_name="Relay alert"):
        source = str(source or "alert")
        duration = self.alert_duration if duration_seconds is None else max(0.1, float(duration_seconds))
        until = time.monotonic() + duration
        with self._lock:
            self._alert_until[source] = until
            self._set_red_source(source, True)
            self._set_buzzer_source(source, True)
            if source in self._alert_threads:
                return
            self._alert_threads.add(source)
        logger.warning("%s; red LED and buzzer ON for %.1fs", log_name, duration)
        threading.Thread(
            target=self._alert_worker,
            args=(source,),
            daemon=True,
            name=f"relay-alert-{source}",
        ).start()

    def cleanup(self):
        with self._lock:
            self._red_sources.clear()
            self._buzzer_sources.clear()
            self._alert_until.clear()
            self._alert_threads.clear()
            self._detection_source_until.clear()
            self._qr_success_running = False
            try:
                self.lock_locker()
                self.green_led_off()
                self._apply_red_locked()
                self._apply_buzzer_locked()
                if self._enabled and self._gpio is not None:
                    self._gpio.cleanup(self.pins)
            except Exception:
                logger.exception("Relay GPIO cleanup failed")
            finally:
                self._enabled = False
                self._gpio = None

    def _qr_success_worker(self, duration):
        try:
            logger.info("QR success; unlocking locker for %.1fs", duration)
            with self._lock:
                self._clear_alert_source_locked("qr_failure")
            self.green_led_on()
            self.unlock_locker()
            time.sleep(duration)
        except Exception:
            logger.exception("QR success relay operation failed")
        finally:
            try:
                self.lock_locker()
                self.green_led_off()
            finally:
                with self._lock:
                    self._qr_success_running = False

    def _alert_worker(self, source):
        try:
            while True:
                with self._lock:
                    until = self._alert_until.get(source, 0.0)
                remaining = until - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.1))
        finally:
            with self._lock:
                self._alert_until.pop(source, None)
                self._alert_threads.discard(source)
                self._set_red_source(source, False)
                self._set_buzzer_source(source, False)
            logger.info("Relay alert cleared for source=%s", source)

    def _set_detection_source(self, source, active, *, red=True, buzzer=True):
        source = str(source or "detection")
        ttl = _env_float("DETECTION_RELAY_SOURCE_TTL_SECONDS", DETECTION_SOURCE_TTL_SECONDS, minimum=0.0)
        with self._lock:
            if active:
                changed = (
                    (red and source not in self._red_sources)
                    or (buzzer and source not in self._buzzer_sources)
                )
                if ttl > 0:
                    self._detection_source_until[source] = time.monotonic() + ttl
                else:
                    self._detection_source_until.pop(source, None)
                if red:
                    self._red_sources.add(source)
                if buzzer:
                    self._buzzer_sources.add(source)
                self._apply_red_locked()
                self._apply_buzzer_locked()
                if ttl > 0:
                    self._ensure_detection_expiry_thread_locked()
                return changed

            changed = (
                (red and source in self._red_sources)
                or (buzzer and source in self._buzzer_sources)
            )
            self._detection_source_until.pop(source, None)
            if red:
                self._red_sources.discard(source)
            if buzzer:
                self._buzzer_sources.discard(source)
            self._apply_red_locked()
            self._apply_buzzer_locked()
            return changed

    def _ensure_detection_expiry_thread_locked(self):
        if self._detection_expiry_thread and self._detection_expiry_thread.is_alive():
            return
        self._detection_expiry_thread = threading.Thread(
            target=self._detection_expiry_worker,
            daemon=True,
            name="relay-detection-expiry",
        )
        self._detection_expiry_thread.start()

    def _detection_expiry_worker(self):
        while True:
            with self._lock:
                now = time.monotonic()
                expired = [
                    source
                    for source, until in self._detection_source_until.items()
                    if until <= now
                ]
                for source in expired:
                    self._detection_source_until.pop(source, None)
                    self._red_sources.discard(source)
                    self._buzzer_sources.discard(source)
                    logger.warning("Detection relay source expired without refresh: %s", source)
                if expired:
                    self._apply_red_locked()
                    self._apply_buzzer_locked()
                if not self._detection_source_until:
                    return
                sleep_for = max(0.05, min(self._detection_source_until.values()) - now)
            time.sleep(min(sleep_for, 0.25))

    def _set_red_source(self, source, active):
        with self._lock:
            source = str(source or "manual")
            if active:
                self._red_sources.add(source)
            else:
                self._red_sources.discard(source)
            self._apply_red_locked()

    def _set_buzzer_source(self, source, active):
        with self._lock:
            source = str(source or "manual")
            if active:
                self._buzzer_sources.add(source)
            else:
                self._buzzer_sources.discard(source)
            self._apply_buzzer_locked()

    def _clear_alert_source_locked(self, source):
        self._alert_until.pop(source, None)
        self._red_sources.discard(source)
        self._buzzer_sources.discard(source)
        self._apply_red_locked()
        self._apply_buzzer_locked()

    def _apply_red_locked(self):
        active = bool(self._red_sources)
        if self._red_on == active:
            return
        self._red_on = active
        self._write(self.red_led_pin, active, "Red LED")

    def _apply_buzzer_locked(self):
        active = bool(self._buzzer_sources)
        if self._buzzer_on == active:
            return
        self._buzzer_on = active
        self._write(self.buzzer_pin, active, "Buzzer")

    def _write(self, pin, active, label):
        if not self._enabled or self._gpio is None:
            logger.info("Relay dry-run: %s %s on BCM GPIO%s", label, "ON" if active else "OFF", pin)
            return
        state = self._active_state() if active else self._inactive_state()
        self._gpio.output(pin, state)
        logger.info("%s %s on BCM GPIO%s", label, "ON" if active else "OFF", pin)

    def _configure_gpio(self, gpio):
        self._gpio = gpio
        gpio.setmode(gpio.BCM)
        gpio.setwarnings(False)
        for pin in self.pins:
            gpio.setup(pin, gpio.OUT, initial=self._inactive_state())

    def _cleanup_gpio_object(self, gpio):
        try:
            gpio.cleanup(self.pins)
        except Exception:
            logger.debug("GPIO cleanup after setup failure failed", exc_info=True)

    def _active_state(self):
        if self._gpio is None:
            return None
        return self._gpio.LOW if self.active_low else self._gpio.HIGH

    def _inactive_state(self):
        if self._gpio is None:
            return None
        return self._gpio.HIGH if self.active_low else self._gpio.LOW


class _LgpioCompat:
    """Small subset of RPi.GPIO backed by lgpio for newer Raspberry Pi OS."""

    BCM = "BCM"
    OUT = "OUT"
    HIGH = 1
    LOW = 0

    def __init__(self):
        import lgpio

        self._lgpio = lgpio
        self._chip_number = None
        self._chip = None
        last_error = None
        for chip_number in (0, 1, 2, 3, 4, 5):
            try:
                self._chip = lgpio.gpiochip_open(chip_number)
                self._chip_number = chip_number
                logger.info("lgpio opened /dev/gpiochip%s", chip_number)
                break
            except Exception as exc:
                last_error = exc
        if self._chip is None:
            raise RuntimeError(f"could not open any gpiochip 0-5: {last_error}")
        self._claimed = set()

    def setmode(self, mode):
        if mode != self.BCM:
            raise ValueError("RelayController only supports BCM GPIO mode")

    def setwarnings(self, enabled):
        return None

    def setup(self, pin, direction, initial=None):
        if direction != self.OUT:
            raise ValueError("RelayController only supports GPIO output pins")
        level = self.LOW if initial is None else int(initial)
        self._lgpio.gpio_claim_output(self._chip, int(pin), level)
        self._claimed.add(int(pin))

    def output(self, pin, state):
        pin = int(pin)
        if pin not in self._claimed:
            self.setup(pin, self.OUT, initial=state)
            return
        self._lgpio.gpio_write(self._chip, pin, int(state))

    def cleanup(self, pins=None):
        pins = tuple(self._claimed if pins is None else pins)
        for pin in pins:
            pin = int(pin)
            if pin in self._claimed:
                try:
                    self._lgpio.gpio_free(self._chip, pin)
                except Exception:
                    logger.debug("lgpio free failed for GPIO%s", pin, exc_info=True)
                self._claimed.discard(pin)
        if not self._claimed:
            try:
                self._lgpio.gpiochip_close(self._chip)
            except Exception:
                logger.debug("lgpio chip close failed", exc_info=True)
