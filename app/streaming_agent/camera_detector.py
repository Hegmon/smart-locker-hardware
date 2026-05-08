import subprocess
import re
def detect_usb_cameras():

    result = subprocess.run(
        ["v4l2-ctl", "--list-devices"],
        capture_output=True,
        text=True
    )

    output = result.stdout

    cameras = []

    blocks = output.strip().split("\n\n")

    for block in blocks:

        lines = block.split("\n")

        if not lines:
            continue

        header = lines[0]
        if (
            "bcm2835" in header
            or "codec" in header
            or "isp" in header
            or "hevc" in header
        ):
            continue

        # Only USB cameras
        if "usb-" not in header:
            continue

        camera_name = header.split("(")[0].strip()

        video_devices = []

        for line in lines[1:]:

            line = line.strip()

            if "/dev/video" in line:

                # Ignore metadata devices later
                video_devices.append(line)

        if not video_devices:
            continue

        # Usually first video device is actual stream
        main_video = video_devices[0]

        usb_match = re.search(r"\(usb-[^)]+\)", header)

        usb_path = usb_match.group(0)[1:-1] if usb_match else "unknown"

        cameras.append({
            "camera_name": camera_name,
            "usb_path": usb_path,
            "video_device": main_video
        })

    return cameras


if __name__ == "__main__":

    cameras = detect_usb_cameras()

    print("\nDetected USB Cameras:\n")

    for idx, cam in enumerate(cameras):

        print(f"Camera {idx}:")
        print(f"  Name         : {cam['camera_name']}")
        print(f"  USB Path     : {cam['usb_path']}")
        print(f"  Video Device : {cam['video_device']}")
        print()