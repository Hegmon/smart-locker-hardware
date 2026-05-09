from app.streaming_agent.camera_detector import detect_usb_cameras

INTERNAL_CAMERA_KEYWORDS="1.2"

EXTERNAL_CAMERA_KEYWORDS="1.4"

def assign_camera_roles():
    cameras=detect_usb_cameras()
    assigned_roles={
        "internal":None,
        "external":None
    }
    for camera in cameras:
        usb_path=camera["usb_path"].lower()
        if INTERNAL_CAMERA_KEYWORDS in usb_path:
            assigned_roles["internal"]=camera
        elif EXTERNAL_CAMERA_KEYWORDS in usb_path:
            assigned_roles["external"]=camera
        print(f"assigned roles:",assigned_roles)
    return assigned_roles

if __name__=="__main__":
    assign_camera_roles()