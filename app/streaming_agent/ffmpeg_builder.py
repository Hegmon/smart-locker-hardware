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
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-video_size", "1280x720",
        "-framerate", "20",
        "-i", video_device,
        "-an",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-profile:v", "baseline",
        "-b:v", "700k",
        "-maxrate", "700k",
        "-bufsize", "350k",
        "-g", "20",
        "-keyint_min", "20",
        "-sc_threshold", "0",
        "-rtsp_transport", "tcp",
        "-f", "rtsp",
        rtsp_url,
    ]
