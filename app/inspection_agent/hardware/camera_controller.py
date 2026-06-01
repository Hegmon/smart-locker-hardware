from __future__ import annotations

"""Camera capture utilities for inspection tests."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import cv2
except Exception:  # pragma: no cover - optional Pi dependency
    cv2 = None

from app.utils.logger import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class CameraCaptureResult:
    """Structured result for a single camera frame capture."""

    role: str
    device: str
    captured: bool
    message: str
    details: dict[str, Any] | None = None


class CameraController:
    """Best-effort camera resolution and one-frame capture helper."""

    def __init__(self) -> None:
        self._defaults = {
            "internal": os.getenv("INTERNAL_CAMERA_DEVICE", "/dev/video0").strip(),
            "external": os.getenv("EXTERNAL_CAMERA_DEVICE", "/dev/video2").strip(),
        }

    def capture_frame(self, role: str) -> CameraCaptureResult:
        device = self.resolve_device(role)
        if cv2 is None:
            return CameraCaptureResult(role=role, device=device, captured=False, message="OpenCV is unavailable")
        if not device:
            return CameraCaptureResult(role=role, device="", captured=False, message=f"No camera device resolved for {role}")
        if not Path(device).exists():
            return CameraCaptureResult(role=role, device=device, captured=False, message=f"Camera device not found: {device}")

        capture = None
        try:
            capture = cv2.VideoCapture(device, cv2.CAP_V4L2)
            if not capture.isOpened():
                return CameraCaptureResult(role=role, device=device, captured=False, message=f"Unable to open camera: {device}")
            ok, frame = capture.read()
            if not ok or frame is None:
                return CameraCaptureResult(role=role, device=device, captured=False, message="No frame captured")
            height = int(getattr(frame, "shape", [0, 0])[0])
            width = int(getattr(frame, "shape", [0, 0])[1])
            return CameraCaptureResult(
                role=role,
                device=device,
                captured=True,
                message="Frame captured",
                details={"width": width, "height": height},
            )
        except Exception as exc:
            logger.exception("Camera capture failed for role=%s device=%s", role, device)
            return CameraCaptureResult(role=role, device=device, captured=False, message=str(exc))
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    logger.debug("Camera release failed", exc_info=True)

    def resolve_device(self, role: str) -> str:
        normalized = str(role or "").strip().lower()
        device = self._defaults.get(normalized, "")
        return device if device else ""
