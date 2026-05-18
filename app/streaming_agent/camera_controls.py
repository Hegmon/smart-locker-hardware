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
)


class CameraControlManager:
    """Best-effort v4l2 camera controls that do not open the video stream."""

    def __init__(self):
        self._last_autofocus_at = {}

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

    @staticmethod
    def _set_control(video_device, control):
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
        if stderr:
            logger.debug("Camera control unsupported on %s: %s (%s)", video_device, control, stderr)
        return False
