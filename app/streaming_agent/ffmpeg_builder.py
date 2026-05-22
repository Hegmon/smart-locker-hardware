import os

from app.streaming_agent.config_loader import get_device_id


MEDIAMTX_HOST = "69.62.125.223"
MEDIAMTX_RTSP_PORT = 8554
QR_FRAME_WIDTH = int(os.getenv("QR_FRAME_WIDTH", "960"))
QR_FRAME_HEIGHT = int(os.getenv("QR_FRAME_HEIGHT", "540"))
QR_FRAME_CHANNELS = int(os.getenv("QR_FRAME_CHANNELS", "3"))
QR_FRAME_FPS = max(1, int(os.getenv("QR_FRAME_FPS", "10")))
STREAM_VIDEO_ENCODER = os.getenv("STREAM_VIDEO_ENCODER", "libx264").strip() or "libx264"
STREAM_INPUT_FORMAT = os.getenv("STREAM_INPUT_FORMAT", "mjpeg").strip() or "mjpeg"
STREAM_INPUT_SIZE = os.getenv("STREAM_INPUT_SIZE", "1280x720").strip() or "1280x720"
STREAM_INPUT_FPS = max(1, int(os.getenv("STREAM_INPUT_FPS", "20")))
STREAM_RTSP_TRANSPORT = os.getenv("STREAM_RTSP_TRANSPORT", "tcp").strip() or "tcp"
STREAM_BITRATE = os.getenv("STREAM_VIDEO_BITRATE", "1200k").strip() or "1200k"
STREAM_MAXRATE = os.getenv("STREAM_VIDEO_MAXRATE", STREAM_BITRATE).strip() or STREAM_BITRATE
STREAM_BUFSIZE = os.getenv("STREAM_VIDEO_BUFSIZE", "600k").strip() or "600k"
STREAM_GOP = max(1, int(os.getenv("STREAM_GOP", str(STREAM_INPUT_FPS))))


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
            *_global_low_latency_args(),
            *_input_low_latency_args(),
            "-i", video_device,
            "-an",
            "-filter_complex",
            "[0:v]split=2[rtsp][detect];"
            f"[detect]fps={QR_FRAME_FPS},"
            f"scale={frame_width}:{frame_height}:force_original_aspect_ratio=decrease,"
            f"pad={frame_width}:{frame_height}:(ow-iw)/2:(oh-ih)/2,format=bgr24[raw]",
            "-map", "[rtsp]",
            *encoder_args,
            *_rtsp_low_latency_args(),
            "-f", "rtsp",
            rtsp_url,
            "-map", "[raw]",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "pipe:1",
        ]

    return  [
    "ffmpeg",
    *_global_low_latency_args(),
    *_input_low_latency_args(),
    "-i", video_device,
    "-an",
    *encoder_args,
    *_rtsp_low_latency_args(),
    "-f", "rtsp",
    rtsp_url,
]


def _global_low_latency_args():
    return [
        "-hide_banner",
        "-loglevel", os.getenv("STREAM_FFMPEG_LOGLEVEL", "warning"),
        "-nostdin",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-avioflags", "direct",
        "-probesize", os.getenv("STREAM_PROBESIZE", "32"),
        "-analyzeduration", os.getenv("STREAM_ANALYZEDURATION", "0"),
    ]


def _input_low_latency_args():
    return [
        "-thread_queue_size", os.getenv("STREAM_THREAD_QUEUE_SIZE", "1"),
        "-rtbufsize", os.getenv("STREAM_RTBUF_SIZE", "256k"),
        "-use_wallclock_as_timestamps", "1",
        "-f", "v4l2",
        "-input_format", STREAM_INPUT_FORMAT,
        "-video_size", STREAM_INPUT_SIZE,
        "-framerate", str(STREAM_INPUT_FPS),
    ]


def _rtsp_low_latency_args():
    return [
        "-rtsp_transport", STREAM_RTSP_TRANSPORT,
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-flush_packets", "1",
        "-max_delay", "0",
    ]


def _encoder_args():
    common = [
        "-pix_fmt", "yuv420p",
        "-b:v", STREAM_BITRATE,
        "-maxrate", STREAM_MAXRATE,
        "-bufsize", STREAM_BUFSIZE,
        "-g", str(STREAM_GOP),
        "-bf", "0",
    ]
    if STREAM_VIDEO_ENCODER == "libx264":
        return [
            "-c:v", "libx264",
            "-preset", os.getenv("STREAM_X264_PRESET", "ultrafast"),
            "-tune", "zerolatency",
            "-x264-params", f"keyint={STREAM_GOP}:min-keyint={STREAM_GOP}:scenecut=0",
            *common,
        ]
    return ["-c:v", STREAM_VIDEO_ENCODER, *common]
