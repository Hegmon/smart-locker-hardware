import os
import subprocess
import time

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)

AUTOFOCUS_ENABLED = os.getenv("QR_AUTOFOCUS_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
AUTOFOCUS_COOLDOWN_SECONDS = float(os.getenv("QR_AUTOFOCUS_COOLDOWN_SECONDS", "3"))
AUTOFOCUS_CONTROLS = (
    "focus_auto=1",
    "focus_automatic_continuous=1",
    "auto_focus=1",
    "auto_focus_start=1",
)
QR_CAMERA_CONTROLS = (
    "auto_exposure=3",
    "exposure_auto=3",
    "exposure_auto_priority=0",
    "white_balance_automatic=1",
    "white_balance_temperature_auto=1",
    "backlight_compensation=0",
)
QR_CONTROL_COOLDOWN_SECONDS = float(os.getenv("QR_CAMERA_CONTROL_COOLDOWN_SECONDS", "2"))
FOCUS_SWEEP_ENABLED = os.getenv("QR_FOCUS_SWEEP_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
FOCUS_SWEEP_VALUES = tuple(
    int(value.strip())
    for value in os.getenv("QR_FOCUS_SWEEP_VALUES", "0,10,20,30,40,55,70,90,115,145,180,220").split(",")
    if value.strip()
)


class CameraControlManager:
    """Best-effort v4l2 camera controls that do not open the video stream."""

    def __init__(self):
        self._last_autofocus_at = {}
        self._last_qr_controls_at = {}
        self._focus_sweep_indexes = {}
        self._last_focus_warning_at = {}
        self._unsupported_controls = set()

    def prepare_for_qr_scan(self, video_device, *, reason="QR scan", force=False, sweep_focus=False):
        if not video_device:
            return False

        applied = self.enable_autofocus(video_device, reason=reason, force=force)
        applied = self._apply_qr_camera_controls(video_device, force=force) or applied
        if FOCUS_SWEEP_ENABLED and (sweep_focus or not applied):
            applied = self.sweep_manual_focus(video_device, reason=reason) or applied
        return applied

    def enable_autofocus(self, video_device, *, reason="startup", force=False):
        if not AUTOFOCUS_ENABLED:
            return False
        if not video_device:
            return False

        now = time.monotonic()
        last_run = self._last_autofocus_at.get(video_device, 0)
        if not force and now - last_run < AUTOFOCUS_COOLDOWN_SECONDS:
            return False

        self._last_autofocus_at[video_device] = now
        applied = False
        for control in AUTOFOCUS_CONTROLS:
            if self._set_control(video_device, control):
                applied = True

        if applied:
            logger.info("Autofocus enabled for %s (%s)", video_device, reason)
        else:
            logger.warning(
                "Autofocus controls were not accepted for %s. Check `v4l2-ctl -d %s --list-ctrls`.",
                video_device,
                video_device,
            )
        return applied

    def sweep_manual_focus(self, video_device, *, reason="QR scan"):
        if not FOCUS_SWEEP_VALUES:
            return False

        index = self._focus_sweep_indexes.get(video_device, 0) % len(FOCUS_SWEEP_VALUES)
        focus_value = FOCUS_SWEEP_VALUES[index]
        self._focus_sweep_indexes[video_device] = index + 1

        applied = self._set_control(video_device, "focus_auto=0")
        applied = self._set_control(video_device, f"focus_absolute={focus_value}") or applied
        if applied:
            logger.info("Manual focus sweep set %s to %s (%s)", video_device, focus_value, reason)
        else:
            now = time.monotonic()
            last_warning = self._last_focus_warning_at.get(video_device, 0)
            if now - last_warning >= 10:
                self._last_focus_warning_at[video_device] = now
                logger.warning(
                    "Manual focus sweep was not accepted for %s. Check `v4l2-ctl -d %s --list-ctrls` "
                    "for a focus_absolute or equivalent focus control.",
                    video_device,
                    video_device,
                )
        return applied

    def _apply_qr_camera_controls(self, video_device, *, force=False):
        now = time.monotonic()
        last_run = self._last_qr_controls_at.get(video_device, 0)
        if not force and now - last_run < QR_CONTROL_COOLDOWN_SECONDS:
            return False

        self._last_qr_controls_at[video_device] = now
        applied = False
        for control in QR_CAMERA_CONTROLS:
            if self._set_control(video_device, control):
                applied = True
        if applied:
            logger.info("QR camera controls applied on %s", video_device)
        return applied

    def _set_control(self, video_device, control):
        unsupported_key = (video_device, control.split("=", 1)[0])
        if unsupported_key in self._unsupported_controls:
            return False

        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", video_device, "--set-ctrl", control],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
        except FileNotFoundError:
            logger.warning("v4l2-ctl not found; cannot configure autofocus for %s", video_device)
            return False
        except subprocess.TimeoutExpired:
            logger.warning("v4l2-ctl timed out while setting %s on %s", control, video_device)
            return False
        except Exception:
            logger.exception("Failed to set camera control %s on %s", control, video_device)
            return False

        if result.returncode == 0:
            logger.info("Camera control applied on %s: %s", video_device, control)
            return True

        stderr = result.stderr.strip()
        if self._is_unsupported_control_error(stderr):
            self._unsupported_controls.add(unsupported_key)
        if stderr:
            logger.debug("Camera control unsupported on %s: %s (%s)", video_device, control, stderr)
        return False

    @staticmethod
    def _is_unsupported_control_error(stderr):
        normalized = stderr.lower()
        return (
            "unknown control" in normalized
            or "invalid argument" in normalized
            or "not found" in normalized
            or "unsupported" in normalized
        )
