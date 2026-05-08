import subprocess
import os
import re

def get_usb_cameras():
    """Detect USB cameras using v412-ctl"""
    cameras=[]
    by_path="/dev/v4l/by-path/"
    if not os.path.exists(by_path):
        return cameras
    for device in os.listdir(by_path):
        if "index0" not in device:
            continue
        full_path=os.path.join(by_path,device)
        try:
            real_path=os.path.relpath(full_path)
        except Exception as e:
            continue
        video_device="/dev/"+real_path
        cameras.append({
            "usb_path":device,
             "video_device":video_device
        })
        return cameras
    
    

if __name__=="__main__":
     cameras=get_usb_cameras()
     print("\nDetected USB Cameras:\n")

     for idx,cam in enumerate(cameras):
            print(f"Camera {idx}:")
            print(f"  USB Path: {cam['usb_path']}")
            print(f"  Video Device: {cam['video_device']}")
            print()

