from app.streaming_agent.config_loader import get_device_id
import shlex
MEDIAMTX_HOST="69.62.125.223"

MEDIAMTX_RTSP_PORT = 8554

def build_rtsp_url(camera_role):
    device_id=get_device_id()
    return (
        f"rtsp://"
        f"{MEDIAMTX_HOST}:{MEDIAMTX_RTSP_PORT}/"
        f"{device_id}/"
        f"{camera_role}"
    )
def build_ffmpeg_command(video_device,camera_role):
    rtsp_url=build_rtsp_url(camera_role)
    cmd=[
        "ffmpeg",
        "-loglevel","warning",
        "-fflags","nobuffer",
        "-flags","low_delay",
        "-f","v4l2",
        "-input_format","mjpeg",
        "-video_size","1280x720",
        "-framerate","30",
        "-i",video_device,
        "-c:v","copy",
        "-f","rtsp",
        rtsp_url
    ]
    print(f"command:",cmd)
    return cmd
if __name__=="__main__":
    video_device="/dev/video0"
    camera_role="internal"
    cmd=build_ffmpeg_command(video_device,camera_role)
    print("FFmpeg command:",shlex.join(cmd))