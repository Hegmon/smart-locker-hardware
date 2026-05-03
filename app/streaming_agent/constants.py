# Streaming Agent Constants
# ==========================

# MediaMTX ports and host
MEDIAMTX_RTSP_PORT = 8554
MEDIAMTX_HLS_PORT = 8888
MEDIAMTX_HOST = "backend.qbox.sa"  # Default production RTSP publish target

# Stream URL templates
HLS_URL_TEMPLATE = "http://{host}:{port}/hls/{device_id}/{stream_type}/index.m3u8"
RTSP_URL_TEMPLATE = "rtsp://{host}:{port}/{device_id}/{stream_type}"

# Camera types
CAMERA_INTERNAL = "internal"
CAMERA_EXTERNAL = "external"

# Stream type mapping (for MQTT and URLs)
STREAM_TYPE_INTERNAL = "internal"
STREAM_TYPE_EXTERNAL = "external"

# FFmpeg encoding options optimized for Raspberry Pi 4 (ARM64)
FFMPEG_INPUT_OPTIONS = [
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-framerate", "30",
    "-probesize", "32",
    "-analyzeduration", "0",
]

FFMPEG_ENCODE_VIDEO = [
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-tune", "zerolatency",
    "-profile:v", "baseline",
    "-level", "3.1",
    "-vf", "scale=1280:720",
    "-b:v", "800k",
    "-maxrate", "1000k",
    "-bufsize", "1200k",
    "-r", "30",
]

FFMPEG_OUTPUT_OPTIONS = [
    "-f", "rtsp",
    "-rtsp_transport", "tcp",
]

# Process monitoring
PROCESS_CHECK_INTERVAL = 5  # seconds
RESTART_MAX_ATTEMPTS = 3
RESTART_BACKOFF = [1, 5, 10]  # seconds between retries

# Stream verification
VERIFY_TIMEOUT = 10  # seconds
VERIFY_RETRY_COUNT = 3

# Logging
LOG_LEVEL = "INFO"
