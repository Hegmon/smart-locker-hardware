import os

from app.streaming_agent.config_loader import get_device_id


MEDIAMTX_HOST = "69.62.125.223"
MEDIAMTX_RTSP_PORT = 8554
QR_FRAME_WIDTH = int(os.getenv("QR_FRAME_WIDTH", "960"))
QR_FRAME_HEIGHT = int(os.getenv("QR_FRAME_HEIGHT", "540"))
QR_FRAME_CHANNELS = int(os.getenv("QR_FRAME_CHANNELS", "3"))
QR_FRAME_FPS = max(1, int(os.getenv("QR_FRAME_FPS", "10")))
STREAM_VIDEO_ENCODER = os.getenv("STREAM_VIDEO_ENCODER", "libx264").strip() or "libx264"


def build_rtsp_url(camera_role):
    device_id = get_device_id()
    return f"rtsp://{MEDIAMTX_HOST}:{MEDIAMTX_RTSP_PORT}/{device_id}/{camera_role}"


def build_ffmpeg_command(
    video_device,
    camera_role,
    *,
    frame_pipe=False,
    frame_width=QR_FRAME_WIDTH,
    frame_height=QR_FRAME_HEIGHT,
):
    rtsp_url = build_rtsp_url(camera_role)
    encoder_args = _encoder_args()
    if frame_pipe:
        return [
            "ffmpeg",
            "-loglevel", "warning",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-thread_queue_size", "2",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-video_size", "1280x720",
            "-framerate", "20",
            "-i", video_device,
            "-an",
            "-filter_complex",
            "[0:v]split=2[rtsp][detect];"
            f"[detect]fps={QR_FRAME_FPS},"
            f"scale={frame_width}:{frame_height}:force_original_aspect_ratio=decrease,"
            f"pad={frame_width}:{frame_height}:(ow-iw)/2:(oh-ih)/2,format=bgr24[raw]",
            "-map", "[rtsp]",
            *encoder_args,
            "-rtsp_transport", "tcp",
            "-muxdelay", "0",
            "-muxpreload", "0",
            "-f", "rtsp",
            rtsp_url,
            "-map", "[raw]",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "pipe:1",
        ]

    return  [
    "ffmpeg",
    "-loglevel", "warning",
 
    # Low latency
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-thread_queue_size", "2",
 
    # Camera input
    "-f", "v4l2",
    "-input_format", "mjpeg",
    "-video_size", "1280x720",
    "-framerate", "20",
    "-i", video_device,
 
    "-an",
 
    *encoder_args,
 
    # RTSP output
    "-rtsp_transport", "tcp",
    "-muxdelay", "0",
    "-muxpreload", "0",
    "-f", "rtsp",
    rtsp_url,
]


def _encoder_args():
    common = [
        "-pix_fmt", "yuv420p",
        "-b:v", "1200k",
        "-maxrate", "1200k",
        "-bufsize", "2400k",
        "-g", "40",
        "-bf", "0",
    ]
    if STREAM_VIDEO_ENCODER == "libx264":
        return [
            "-c:v", "libx264",
            "-preset", os.getenv("STREAM_X264_PRESET", "veryfast"),
            "-tune", "zerolatency",
            *common,
        ]
    return ["-c:v", STREAM_VIDEO_ENCODER, *common]
