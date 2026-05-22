import os

from app.streaming_agent.camera_detector import detect_usb_cameras
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager

logger = LoggingManager.get_logger(__name__)

INTERNAL_CAMERA_KEYWORDS = "1.2"

EXTERNAL_CAMERA_KEYWORDS = "1.4"

def assign_camera_roles():
    cameras = detect_usb_cameras(
        retries=int(os.getenv("CAMERA_DETECTION_RETRIES", "10")),
        retry_delay=float(os.getenv("CAMERA_DETECTION_RETRY_DELAY", "1.0")),
    )
    assigned_roles = {
        "internal": None,
        "external": None,
    }
    for camera in cameras:
        usb_path = camera["usb_path"].lower()
        if INTERNAL_CAMERA_KEYWORDS in usb_path:
            assigned_roles["internal"] = camera
        elif EXTERNAL_CAMERA_KEYWORDS in usb_path:
            assigned_roles["external"] = camera

    unassigned_cameras = [
        camera
        for camera in cameras
        if camera is not assigned_roles["internal"] and camera is not assigned_roles["external"]
    ]
    if assigned_roles["internal"] is None and unassigned_cameras:
        assigned_roles["internal"] = unassigned_cameras.pop(0)
        logger.warning(
            "Internal camera USB path match not found; using fallback %s",
            assigned_roles["internal"]["video_device"],
        )
    if assigned_roles["external"] is None and unassigned_cameras:
        assigned_roles["external"] = unassigned_cameras.pop(0)
        logger.warning(
            "External camera USB path match not found; using fallback %s",
            assigned_roles["external"]["video_device"],
        )

    logger.info(
        "Assigned camera roles: internal=%s external=%s",
        assigned_roles["internal"]["video_device"] if assigned_roles["internal"] else None,
        assigned_roles["external"]["video_device"] if assigned_roles["external"] else None,
    )
    return assigned_roles
