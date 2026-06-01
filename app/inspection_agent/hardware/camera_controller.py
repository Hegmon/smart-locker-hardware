from __future__ import annotations

"""Camera capture utilities for inspection tests."""

import os
import shutil
import subprocess
import tempfile
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
            "internal": os.getenv("INTERNAL_CAMERA_DEVICE", "").strip(),
            "external": os.getenv("EXTERNAL_CAMERA_DEVICE", "").strip(),
        }

    def capture_frame(self, role: str) -> CameraCaptureResult:
        normalized_role = self._normalize_role(role)

        if normalized_role == "internal":
            libcamera_result = self._capture_via_libcamera(normalized_role)
            if libcamera_result is not None:
                return libcamera_result

        candidates = self.resolve_device_candidates(normalized_role)
        if cv2 is None:
            device = candidates[0] if candidates else ""
            return CameraCaptureResult(role=normalized_role, device=device, captured=False, message="OpenCV is unavailable")

        last_message = f"No camera device resolved for {normalized_role}"
        for device in candidates:
            if not device:
                continue
            result = self._capture_via_opencv(normalized_role, device)
            if result.captured:
                return result
            last_message = result.message

        return CameraCaptureResult(
            role=normalized_role,
            device=candidates[0] if candidates else "",
            captured=False,
            message=last_message,
        )

    def resolve_device(self, role: str) -> str:
        candidates = self.resolve_device_candidates(role)
        return candidates[0] if candidates else ""

    def resolve_device_candidates(self, role: str) -> list[str]:
        normalized = self._normalize_role(role)
        candidates: list[str] = []

        def add_candidate(device: str | None, *, must_exist: bool = True) -> None:
            if not device:
                return
            candidate = str(device).strip()
            if not candidate or candidate in candidates:
                return
            if must_exist and not Path(candidate).exists():
                return
            candidates.append(candidate)

        add_candidate(self._defaults.get(normalized))

        try:
            from app.streaming_agent.camera_roles import assign_camera_roles

            roles = assign_camera_roles()
            role_entry = roles.get(normalized) or {}
            add_candidate(role_entry.get("video_device"))
        except Exception:
            logger.debug("Camera role assignment unavailable", exc_info=True)

        try:
            from app.streaming_agent.camera_detector import detect_usb_cameras

            detected_cameras = detect_usb_cameras(retries=2, retry_delay=0.5)
            role_cameras = self._select_detected_cameras_for_role(normalized, detected_cameras)
            for camera in role_cameras:
                add_candidate(camera.get("video_device"))
            for camera in detected_cameras:
                add_candidate(camera.get("video_device"))
        except Exception:
            logger.debug("USB camera detection unavailable", exc_info=True)

        for index in range(8):
            add_candidate(f"/dev/video{index}")

        return candidates

    def _select_detected_cameras_for_role(self, role: str, detected_cameras: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not detected_cameras:
            return []

        normalized = self._normalize_role(role)
        internal_keywords = ("1.2", "internal")
        external_keywords = ("1.4", "external")

        if normalized == "internal":
            preferred = [
                camera
                for camera in detected_cameras
                if any(keyword in str(camera.get("usb_path", "")).lower() for keyword in internal_keywords)
            ]
            return preferred or detected_cameras[:1]

        if normalized == "external":
            preferred = [
                camera
                for camera in detected_cameras
                if any(keyword in str(camera.get("usb_path", "")).lower() for keyword in external_keywords)
            ]
            if preferred:
                return preferred
            return detected_cameras[1:2] or detected_cameras[:1]

        return detected_cameras[:1]

    def _capture_via_opencv(self, role: str, device: str) -> CameraCaptureResult:
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
            logger.debug("Camera capture failed for role=%s device=%s", role, device, exc_info=True)
            return CameraCaptureResult(role=role, device=device, captured=False, message=str(exc))
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    logger.debug("Camera release failed", exc_info=True)

    def _capture_via_libcamera(self, role: str) -> CameraCaptureResult | None:
        command = self._libcamera_command()
        if command is None:
            return None

        output_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix=f"inspection-{role}-", suffix=".jpg", delete=False) as temp_file:
                output_path = temp_file.name

            result = subprocess.run(
                [*command, "--nopreview", "--timeout", "500", "--output", output_path],
                capture_output=True,
                text=True,
                check=False,
                timeout=10.0,
            )
            if result.returncode != 0:
                logger.debug("libcamera capture failed for %s: %s", role, result.stderr.strip())
                return None

            if not output_path or not Path(output_path).exists() or Path(output_path).stat().st_size <= 0:
                return None

            return CameraCaptureResult(
                role=role,
                device=command[0],
                captured=True,
                message="Frame captured via libcamera",
                details={"method": command[0]},
            )
        except FileNotFoundError:
            return None
        except Exception:
            logger.debug("libcamera capture path failed for role=%s", role, exc_info=True)
            return None
        finally:
            if output_path:
                try:
                    Path(output_path).unlink(missing_ok=True)
                except Exception:
                    logger.debug("Failed to clean up temporary libcamera image %s", output_path, exc_info=True)

    @staticmethod
    def _libcamera_command() -> list[str] | None:
        for command in ("rpicam-still", "libcamera-still"):
            if shutil.which(command):
                return [command]
        return None

    @staticmethod
    def _normalize_role(role: str) -> str:
        return str(role or "").strip().lower()
