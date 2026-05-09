import subprocess
from .camera_detector import detect_usb_cameras

def test_usb_camera_detection(video_device):
    cmd=[
        "ffmeg",
        "-f","v4l2",
        "input_format","mjpeg",
        "video_size","1280x720",
        "-i",video_device,
        "framerate","30",
        "-t","3",
        "-f","null",
        "-"
    ]
    try:
        result=subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10
        )
        if result.returncode==0:
            return True,print(f"Camera {video_device} is working correctly.")
        return result.stderr,print(f"Camera {video_device} is not working correctly.")
    except subprocess.TimeoutExpired:
        return "Timeout",print(f"Camera {video_device} test timed out.")
    except Exception as e:
        return str(e),print(f"An error occurred while testing camera {video_device}: {e}")
    
def main():
    cameras=detect_usb_cameras()
    if not cameras:
        print("No USB cameras detected.")
        return
    print("\nTesting USB Cameras:\n")
    for idx,camera in enumerate(cameras):
        print(f"Testing Camera {idx+1}:{camera["name"]} at {camera["usb_path"]} with device {camera["device"]}")
        video_device=camera["device"]
        success,message=test_usb_camera_detection(video_device)
        if success:
            print(f"Camera {camera['name']} passed the test.\n")
        else:
            print(f"Camera {camera['name']} failed the test: {message}\n")

if __name__ == "__main__":
    main()

