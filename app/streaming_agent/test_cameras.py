import subprocess

from app.streaming_agent.camera_detector import detect_usb_cameras
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


def test_usb_camera_detection(video_device):
    cmd = [
        "ffmpeg",
        "-f",
        "v4l2",
        "-input_format",
        "mjpeg",
        "-video_size",
        "1280x720",
        "-framerate",
        "30",
        "-i",
        video_device,
        "-t",
        "3",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            logger.info("Camera %s is working correctly", video_device)
            return True, "Camera stream is working correctly"

        logger.error("Camera %s is not working correctly", video_device)
        return False, result.stderr
    except subprocess.TimeoutExpired:
        logger.error("Camera %s test timed out", video_device)
        return False, "Timeout"
    except Exception as error:
        logger.exception("An error occurred while testing camera %s", video_device)
        return False, str(error)


def main():
    cameras = detect_usb_cameras()
    if not cameras:
        logger.warning("No USB cameras detected")
        return

    logger.info("Testing USB cameras")
    for idx, camera in enumerate(cameras, start=1):
        logger.info(
            "Testing camera %s: %s at %s with device %s",
            idx,
            camera["camera_name"],
            camera["usb_path"],
            camera["video_device"],
        )
        video_device = camera["video_device"]
        success, message = test_usb_camera_detection(video_device)
        if success:
            logger.info("Camera %s passed the test", camera["camera_name"])
        else:
            logger.error("Camera %s failed the test: %s", camera["camera_name"], message)


if __name__ == "__main__":
    main()
