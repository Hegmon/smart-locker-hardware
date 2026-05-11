from app.streaming_agent.config_loader import get_device_id


MEDIAMTX_HOST = "69.62.125.223"
MEDIAMTX_RTSP_PORT = 8554


def build_rtsp_url(camera_role):
    device_id = get_device_id()
    return f"rtsp://{MEDIAMTX_HOST}:{MEDIAMTX_RTSP_PORT}/{device_id}/{camera_role}"


def build_ffmpeg_command(video_device, camera_role):
    rtsp_url = build_rtsp_url(camera_role)
    return  [
    "ffmpeg",
    "-loglevel", "warning",

    # Low latency
    "-fflags", "nobuffer",
    "-flags", "low_delay",

    # Camera input
    "-f", "v4l2",
    "-input_format", "mjpeg",
    "-video_size", "1280x720",
    "-framerate", "50",
    "-i", video_device,

    "-an",

    # Encoder
    "-c:v", "libx264",
    "-preset", "veryfast",          # better quality than ultrafast
    "-tune", "zerolatency",

    "-pix_fmt", "yuv420p",
    "-profile:v", "main",

    # Quality settings
    "-b:v", "2500k",
    "-maxrate", "3000k",
    "-bufsize", "6000k",

    # GOP
    "-g", "30",
    "-keyint_min", "30",
    "-sc_threshold", "0",
    "-bf", "0",

    # RTSP output
    "-rtsp_transport", "tcp",
    "-f", "rtsp",
    rtsp_url,
]
