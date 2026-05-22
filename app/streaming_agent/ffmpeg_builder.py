import os
from urllib.parse import urljoin

from app.streaming_agent.config_loader import get_device_id


MEDIAMTX_HOST = os.getenv("MEDIAMTX_HOST", "69.62.125.223").strip() or "69.62.125.223"
MEDIAMTX_RTSP_PORT = int(os.getenv("MEDIAMTX_RTSP_PORT", "8554"))
STREAM_PUBLIC_BASE_URL = os.getenv("STREAM_PUBLIC_BASE_URL", "").strip().rstrip("/")
QR_FRAME_WIDTH = int(os.getenv("QR_FRAME_WIDTH", "960"))
QR_FRAME_HEIGHT = int(os.getenv("QR_FRAME_HEIGHT", "540"))
QR_FRAME_CHANNELS = int(os.getenv("QR_FRAME_CHANNELS", "3"))
QR_FRAME_FPS = max(1, int(os.getenv("QR_FRAME_FPS", "10")))
INTERNAL_FRAME_FPS = max(1, int(os.getenv("INTERNAL_FRAME_FPS", "10")))
STREAM_VIDEO_ENCODER = os.getenv("STREAM_VIDEO_ENCODER", "libx264").strip() or "libx264"
STREAM_INPUT_FORMAT = os.getenv("STREAM_INPUT_FORMAT", "mjpeg").strip() or "mjpeg"
STREAM_INPUT_SIZE = os.getenv("STREAM_INPUT_SIZE", "1280x720").strip() or "1280x720"
STREAM_INPUT_FPS = max(1, int(os.getenv("STREAM_INPUT_FPS", "20")))
STREAM_OUTPUT_WIDTH = max(160, int(os.getenv("STREAM_OUTPUT_WIDTH", "640")))
STREAM_OUTPUT_HEIGHT = max(120, int(os.getenv("STREAM_OUTPUT_HEIGHT", "360")))
STREAM_OUTPUT_FPS = max(1, int(os.getenv("STREAM_OUTPUT_FPS", "15")))
STREAM_RTSP_TRANSPORT = os.getenv("STREAM_RTSP_TRANSPORT", "tcp").strip() or "tcp"
STREAM_BITRATE = os.getenv("STREAM_VIDEO_BITRATE", "500k").strip() or "500k"
STREAM_MAXRATE = os.getenv("STREAM_VIDEO_MAXRATE", STREAM_BITRATE).strip() or STREAM_BITRATE
STREAM_BUFSIZE = os.getenv("STREAM_VIDEO_BUFSIZE", "100k").strip() or "100k"
STREAM_GOP = max(1, int(os.getenv("STREAM_GOP", str(STREAM_OUTPUT_FPS))))


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
    encoder_args = _encoder_args()
    detection_fps = QR_FRAME_FPS if camera_role == "external" else INTERNAL_FRAME_FPS
    if frame_pipe:
        return [
            "ffmpeg",
            *_global_low_latency_args(),
            *_input_low_latency_args(),
            "-i", video_device,
            "-an",
            "-filter_complex",
            "[0:v]split=2[rtsp][detect];"
            f"[rtsp]scale={STREAM_OUTPUT_WIDTH}:{STREAM_OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={STREAM_OUTPUT_WIDTH}:{STREAM_OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=yuv420p[rtspout];"
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
    *_input_low_latency_args(),
    "-i", video_device,
    "-an",
    "-vf",
    f"scale={STREAM_OUTPUT_WIDTH}:{STREAM_OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={STREAM_OUTPUT_WIDTH}:{STREAM_OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
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
        "-pkt_size", os.getenv("STREAM_RTP_PACKET_SIZE", "1316"),
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
