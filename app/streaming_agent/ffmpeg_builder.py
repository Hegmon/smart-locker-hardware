import os
from urllib.parse import urljoin

from app.streaming_agent.config_loader import get_device_id


def _env_int(name, default, minimum=None):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    return value


MEDIAMTX_HOST = os.getenv("MEDIAMTX_HOST", "69.62.125.223").strip() or "69.62.125.223"
MEDIAMTX_RTSP_PORT = int(os.getenv("MEDIAMTX_RTSP_PORT", "8554"))
STREAM_PUBLIC_BASE_URL = os.getenv("STREAM_PUBLIC_BASE_URL", "").strip().rstrip("/")
QR_FRAME_WIDTH = _env_int("QR_FRAME_WIDTH", 1280, minimum=160)
QR_FRAME_HEIGHT = _env_int("QR_FRAME_HEIGHT", 720, minimum=120)
QR_FRAME_CHANNELS = _env_int("QR_FRAME_CHANNELS", 3, minimum=1)
QR_FRAME_FPS = _env_int("QR_FRAME_FPS", 10, minimum=1)
INTERNAL_FRAME_FPS = _env_int("INTERNAL_FRAME_FPS", 8, minimum=1)
STREAM_VIDEO_ENCODER = os.getenv("STREAM_VIDEO_ENCODER", "libx264").strip() or "libx264"
STREAM_INPUT_FORMAT = os.getenv("STREAM_INPUT_FORMAT", "mjpeg").strip() or "mjpeg"
STREAM_INPUT_SIZE = os.getenv("STREAM_INPUT_SIZE", "1280x720").strip() or "1280x720"
EXTERNAL_STREAM_INPUT_SIZE = os.getenv("EXTERNAL_STREAM_INPUT_SIZE", "1280x720").strip() or "1280x720"
STREAM_INPUT_FPS = _env_int("STREAM_INPUT_FPS", 20, minimum=1)
EXTERNAL_STREAM_INPUT_FPS = _env_int("EXTERNAL_STREAM_INPUT_FPS", 20, minimum=1)
STREAM_OUTPUT_WIDTH = _env_int("STREAM_OUTPUT_WIDTH", 640, minimum=160)
STREAM_OUTPUT_HEIGHT = _env_int("STREAM_OUTPUT_HEIGHT", 480, minimum=120)
EXTERNAL_STREAM_OUTPUT_WIDTH = _env_int("EXTERNAL_STREAM_OUTPUT_WIDTH", 960, minimum=160)
EXTERNAL_STREAM_OUTPUT_HEIGHT = _env_int("EXTERNAL_STREAM_OUTPUT_HEIGHT", 540, minimum=120)
STREAM_OUTPUT_FPS = _env_int("STREAM_OUTPUT_FPS", 15, minimum=1)
STREAM_RTSP_TRANSPORT = os.getenv("STREAM_RTSP_TRANSPORT", "tcp").strip() or "tcp"
STREAM_BITRATE = os.getenv("STREAM_VIDEO_BITRATE", "800k").strip() or "800k"
STREAM_MAXRATE = os.getenv("STREAM_VIDEO_MAXRATE", STREAM_BITRATE).strip() or STREAM_BITRATE
STREAM_BUFSIZE = os.getenv("STREAM_VIDEO_BUFSIZE", "120k").strip() or "120k"
EXTERNAL_STREAM_BITRATE = os.getenv("EXTERNAL_STREAM_VIDEO_BITRATE", "1400k").strip() or "1400k"
EXTERNAL_STREAM_MAXRATE = os.getenv("EXTERNAL_STREAM_VIDEO_MAXRATE", EXTERNAL_STREAM_BITRATE).strip() or EXTERNAL_STREAM_BITRATE
EXTERNAL_STREAM_BUFSIZE = os.getenv("EXTERNAL_STREAM_VIDEO_BUFSIZE", "180k").strip() or "180k"
STREAM_GOP = _env_int("STREAM_GOP", STREAM_OUTPUT_FPS, minimum=1)


def build_rtsp_url(camera_role):
    device_id = get_device_id()
    return f"rtsp://{MEDIAMTX_HOST}:{MEDIAMTX_RTSP_PORT}/{device_id}/{camera_role}"


def build_public_stream_urls(camera_role):
    device_id = get_device_id()
    path = f"{device_id}/{camera_role}"
    urls = {
        "rtsp": build_rtsp_url(camera_role),
    }
    if STREAM_PUBLIC_BASE_URL:
        base = STREAM_PUBLIC_BASE_URL + "/"
        urls["hls"] = urljoin(base, f"{path}/index.m3u8")
        urls["webrtc"] = urljoin(base, path)
    return urls


def build_ffmpeg_command(
    video_device,
    camera_role,
    *,
    frame_pipe=False,
    frame_width=QR_FRAME_WIDTH,
    frame_height=QR_FRAME_HEIGHT,
):
    rtsp_url = build_rtsp_url(camera_role)
    encoder_args = _encoder_args(camera_role)
    detection_fps = QR_FRAME_FPS if camera_role == "external" else INTERNAL_FRAME_FPS
    output_width, output_height = _output_dimensions(camera_role)
    if frame_pipe:
        return [
            "ffmpeg",
            *_global_low_latency_args(),
            *_input_low_latency_args(camera_role),
            "-i", video_device,
            "-an",
            "-filter_complex",
            "[0:v]split=2[rtsp][detect];"
            f"[rtsp]scale={output_width}:{output_height}:force_original_aspect_ratio=decrease,"
            f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p[rtspout];"
            f"[detect]fps={detection_fps},"
            f"scale={frame_width}:{frame_height}:force_original_aspect_ratio=decrease,"
            f"pad={frame_width}:{frame_height}:(ow-iw)/2:(oh-ih)/2,format=bgr24[raw]",
            "-map", "[rtspout]",
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
    *_input_low_latency_args(camera_role),
    "-i", video_device,
    "-an",
    "-vf",
    f"scale={output_width}:{output_height}:force_original_aspect_ratio=decrease,"
    f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
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


def _input_low_latency_args(camera_role=None):
    input_size = EXTERNAL_STREAM_INPUT_SIZE if camera_role == "external" else STREAM_INPUT_SIZE
    input_fps = EXTERNAL_STREAM_INPUT_FPS if camera_role == "external" else STREAM_INPUT_FPS
    return [
        "-thread_queue_size", os.getenv("STREAM_THREAD_QUEUE_SIZE", "1"),
        "-rtbufsize", os.getenv("STREAM_RTBUF_SIZE", "256k"),
        "-f", "v4l2",
        "-input_format", STREAM_INPUT_FORMAT,
        "-video_size", input_size,
        "-framerate", str(input_fps),
    ]


def _rtsp_low_latency_args():
    return [
        "-rtsp_transport", STREAM_RTSP_TRANSPORT,
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-flush_packets", "1",
        "-max_delay", "0",
        "-pkt_size", os.getenv("STREAM_RTP_PACKET_SIZE", "1316"),
    ]


def _output_dimensions(camera_role):
    if camera_role == "external":
        return EXTERNAL_STREAM_OUTPUT_WIDTH, EXTERNAL_STREAM_OUTPUT_HEIGHT
    return STREAM_OUTPUT_WIDTH, STREAM_OUTPUT_HEIGHT


def _encoder_args(camera_role):
    bitrate = EXTERNAL_STREAM_BITRATE if camera_role == "external" else STREAM_BITRATE
    maxrate = EXTERNAL_STREAM_MAXRATE if camera_role == "external" else STREAM_MAXRATE
    bufsize = EXTERNAL_STREAM_BUFSIZE if camera_role == "external" else STREAM_BUFSIZE
    common = [
        "-pix_fmt", "yuv420p",
        "-b:v", bitrate,
        "-maxrate", maxrate,
        "-bufsize", bufsize,
        "-g", str(STREAM_GOP),
        "-bf", "0",
    ]
    if STREAM_VIDEO_ENCODER == "libx264":
        return [
            "-c:v", "libx264",
            "-preset", os.getenv("STREAM_X264_PRESET", "ultrafast"),
            "-tune", "zerolatency",
            "-profile:v", os.getenv("STREAM_X264_PROFILE", "baseline"),
            "-threads", os.getenv("STREAM_X264_THREADS", "1"),
            "-x264-params",
            (
                f"keyint={STREAM_GOP}:min-keyint={STREAM_GOP}:scenecut=0:"
                "rc-lookahead=0:sync-lookahead=0:sliced-threads=1"
            ),
            *common,
        ]
    return ["-c:v", STREAM_VIDEO_ENCODER, *common]
