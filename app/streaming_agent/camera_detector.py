from pathlib import Path
import re
import subprocess
import time

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


def detect_usb_cameras(retries=1, retry_delay=1.0):
    for attempt in range(1, max(1, int(retries)) + 1):
        cameras = _detect_usb_cameras_once()
        cameras = [camera for camera in cameras if Path(camera["video_device"]).exists()]
        if cameras:
            return cameras
        if attempt < retries:
            logger.warning("No live USB camera video devices found; retrying in %.1fs", retry_delay)
            time.sleep(max(0.1, float(retry_delay)))
    logger.warning("No live USB camera video devices found after %s attempt(s)", retries)
    return []


def _detect_usb_cameras_once():
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except FileNotFoundError:
        logger.warning("v4l2-ctl not found; no USB cameras detected")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("v4l2-ctl timed out; no USB cameras detected")
        return []
    except Exception:
        logger.exception("USB camera detection failed")
        return []

    output = result.stdout
    cameras = []
    blocks = output.strip().split("\n\n") if output.strip() else []

    for block in blocks:
        lines = block.split("\n")
        if not lines:
            continue

        header = lines[0]
        if any(keyword in header for keyword in ("bcm2835", "codec", "isp", "hevc")):
            continue

        if "usb-" not in header:
            continue

        camera_name = header.split("(")[0].strip()
        video_devices = []

        for line in lines[1:]:
            line = line.strip()
            if "/dev/video" in line:
                video_devices.append(line)

        if not video_devices:
            continue

        existing_video_devices = [device for device in video_devices if Path(device).exists()]
        if not existing_video_devices:
            logger.warning("Skipping camera %s because none of its video nodes exist: %s", camera_name, video_devices)
            continue

        main_video = existing_video_devices[0]
        usb_match = re.search(r"\(usb-[^)]+\)", header)
        usb_path = usb_match.group(0)[1:-1] if usb_match else "unknown"

        cameras.append(
            {
                "camera_name": camera_name,
                "usb_path": usb_path,
                "video_device": main_video,
            }
        )

    logger.info("Detected %s USB camera(s)", len(cameras))
    return cameras


if __name__ == "__main__":
    cameras = detect_usb_cameras()
    for idx, cam in enumerate(cameras, start=1):
        logger.info(
            "Camera %s detected: name=%s usb_path=%s video_device=%s",
            idx,
            cam["camera_name"],
            cam["usb_path"],
            cam["video_device"],
        )
