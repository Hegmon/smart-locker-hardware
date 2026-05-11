from app.streaming_agent.camera_detector import detect_usb_cameras
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager

logger = LoggingManager.get_logger(__name__)

INTERNAL_CAMERA_KEYWORDS = "1.2"

EXTERNAL_CAMERA_KEYWORDS = "1.4"

def assign_camera_roles():
    cameras = detect_usb_cameras()
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

    logger.info(
        "Assigned camera roles: internal=%s external=%s",
        assigned_roles["internal"]["video_device"] if assigned_roles["internal"] else None,
        assigned_roles["external"]["video_device"] if assigned_roles["external"] else None,
    )
    return assigned_roles
