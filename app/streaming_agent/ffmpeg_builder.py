from app.streaming_agent.config_loader import get_device_id


MEDIAMTX_HOST = "69.62.125.223"
MEDIAMTX_RTSP_PORT = 8554


def build_rtsp_url(camera_role):
    device_id = get_device_id()
    return f"rtsp://{MEDIAMTX_HOST}:{MEDIAMTX_RTSP_PORT}/{device_id}/{camera_role}"


def build_ffmpeg_command(video_device, camera_role):
    rtsp_url = build_rtsp_url(camera_role)
    return [
    "ffmpeg",
    "-loglevel", "warning",
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-f", "v4l2",
    "-input_format", "mjpeg",
    "-video_size", "1280x720",
    "-framerate", "30",
    "-i", video_device,
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-tune", "zerolatency",
    "-b:v", "800k",
    "-g", "30",
    "-keyint_min", "30",
    "-rtsp_transport", "udp", 
    "-f", "rtsp",
    rtsp_url,
]
