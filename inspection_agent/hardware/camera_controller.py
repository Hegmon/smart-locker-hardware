from __future__ import annotations

"""Camera capture utilities for inspection tests."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import cv2
except Exception:  # pragma: no cover - optional Pi dependency
    cv2 = None

from app.streaming_agent.camera_roles import assign_camera_roles
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
    """Best-effort camera discovery and one-frame capture helper."""

    def __init__(self) -> None:
        self._camera_roles: dict[str, dict[str, Any] | None] | None = None

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
        roles = self._load_roles()
        entry = roles.get(self._normalize_role(role))
        if entry and entry.get("video_device"):
            return str(entry["video_device"])
        return ""

    def _load_roles(self) -> dict[str, dict[str, Any] | None]:
        if self._camera_roles is None:
            self._camera_roles = assign_camera_roles()
        return self._camera_roles

    @staticmethod
    def _normalize_role(role: str) -> str:
        return str(role or "").strip().lower()
