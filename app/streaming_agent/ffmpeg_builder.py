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
    "-framerate", "20",
    "-i", video_device,
 
    "-an",
 
    # Hardware encoder (IMPORTANT)
    "-c:v", "h264_v4l2m2m",
 
    # Low latency tuning
    "-pix_fmt", "yuv420p",
 
    # Bitrate
    "-b:v", "1200k",
    "-maxrate", "1200k",
    "-bufsize", "2400k",
 
    # GOP
    "-g", "40",
    "-bf", "0",
 
    # RTSP output
    "-rtsp_transport", "tcp",
    "-f", "rtsp",
    rtsp_url,
]
