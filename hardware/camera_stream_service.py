#!/usr/bin/env python3
"""
Raspberry Pi 4 RTSP Streamer to MediaMTX

This script streams both cameras via RTSP/H264 to MediaMTX server.

Requirements:
    sudo apt-get update
    sudo apt-get install -y ffmpeg libcamera-tools v4l-utils
    
Or use libcamera-vid for better Pi camera support:
    sudo apt-get install -y libcamera-apps

Configuration:
    - MediaMTX server IP/hostname (set via MEDIAMTX_HOST env var or edit below)
    - Camera device paths
    - Resolution and FPS

Usage:
    python3 pi_streamer.py start               # Start both cameras
    python3 pi_streamer.py start pi_cam_external   # Start specific camera
    python3 pi_streamer.py stop               # Stop all streams
    python3 pi_streamer.py status             # Check status
    
Environment Variables:
    MEDIAMTX_HOST - MediaMTX server hostname/IP (default: 192.168.1.100)
    MEDIAMTX_PORT - MediaMTX RTSP port (default: 8554)
"""

import subprocess
import os
import sys
import time
import signal
import threading
import re
from pathlib import Path

# ============== CONFIG ==============
# MediaMTX Server Configuration
# RTSP streaming can use domain name or IP address
MEDIAMTX_HOST = os.environ.get("MEDIAMTX_HOST", "69.62.125.223")  # Server IP
MEDIAMTX_PORT = 8554

# Camera Configuration
# Use V4L2 device paths or libcamera
CAMERAS = {
    "pi_cam_external": {
        "device": "/dev/video0",
        "resolution": "640x480",
        "fps": 25,
    },
    "pi_cam_internal": {
        "device": "/dev/video2", 
        "resolution": "640x480",
        "fps": 25,
    }
}

# FFmpeg/Streaming settings
STREAM_KEY = os.environ.get("STREAM_KEY", "secret")  # Optional stream key
HW_ACCEL = True       # Use hardware acceleration
AUDIO = False         # No audio for now

# Process management
FFMPEG_PROCESSES = {}
# ==================================


def get_rtsp_url(camera_id):
    """Get RTSP URL for camera"""
    if STREAM_KEY:
        return f"rtsp://{MEDIAMTX_HOST}:{MEDIAMTX_PORT}/{camera_id}?key={STREAM_KEY}"
    return f"rtsp://{MEDIAMTX_HOST}:{MEDIAMTX_PORT}/{camera_id}"


def check_camera_available(device):
    """Check if camera device is available"""
    return os.path.exists(device)


def get_v4l2_format(device):
    """Get available format for V4L2 device"""
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--device", device, "--list-formats"],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.stdout
    except Exception as e:
        print(f"Error checking camera format: {e}")
        return None


def start_stream_ffmpeg(camera_id, config):
    """Start FFmpeg streaming to MediaMTX"""
    
    if camera_id in FFMPEG_PROCESSES:
        proc = FFMPEG_PROCESSES[camera_id]
        if proc.poll() is None:
            print(f"[{camera_id}] Already streaming")
            return True
    
    device = config["device"]
    resolution = config.get("resolution", "640x480")
    fps = config.get("fps", 25)
    rtsp_url = get_rtsp_url(camera_id)
    
    # Check camera
    if not check_camera_available(device):
        print(f"[{camera_id}] Camera device {device} not found!")
        return False
    
    print(f"[{camera_id}] Starting stream to {rtsp_url}")
    print(f"[{camera_id}] Device: {device}, Resolution: {resolution}, FPS: {fps}")
    
    # FFmpeg command for hardware-accelerated H264 streaming
    cmd = [
        "ffmpeg",
        "-f", "v4l2",
        "-thread_queue_size", "4096",
        "-framerate", str(fps),
        "-video_size", resolution,
        "-i", device,
    ]
    
    # Add hardware acceleration if available
    if HW_ACCEL:
        cmd.extend([
            "-c:v", "h264_v4l2m2m",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
        ])
    else:
        cmd.extend([
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
        ])
    
    # Add h264 encoding options for low latency
    cmd.extend([
        "-b:v", "1000k",
        "-maxrate", "1500k",
        "-bufsize", "2000k",
        "-g", str(fps * 2),  # Keyframe interval
        "-keyint_min", str(fps),
        "-flush_packets", "1",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        rtsp_url
    ])
    
    try:
        # Start FFmpeg process
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid
        )
        FFMPEG_PROCESSES[camera_id] = process
        print(f"[{camera_id}] FFmpeg started (PID: {process.pid})")
        return True
    except Exception as e:
        print(f"[{camera_id}] Failed to start: {e}")
        return False


def start_stream_libcamera(camera_id, config):
    """Start streaming using libcamera (for Pi HQ camera)"""
    
    if camera_id in FFMPEG_PROCESSES:
        proc = FFMPEG_PROCESSES[camera_id]
        if proc.poll() is None:
            print(f"[{camera_id}] Already streaming")
            return True
    
    device = config.get("device", "0")
    resolution = config.get("resolution", "640x480")
    fps = config.get("fps", 25)
    rtsp_url = get_rtsp_url(camera_id)
    
    # Parse resolution
    width, height = map(int, resolution.split('x'))
    
    print(f"[{camera_id}] Starting libcamera stream to {rtsp_url}")
    
    # Use libcamera-vid with FFmpeg for streaming
    cmd = [
        "libcamera-vid",
        "--width", str(width),
        "--height", str(height),
        "--framerate", str(fps),
        "-t", "0",  # Infinite streaming
        "--inline",
        "--segment", "0",  # No segmentation for RTSP
        "-o", "-"
    ]
    
    # Pipe to FFmpeg for RTSP
    ffmpeg_cmd = [
        "ffmpeg",
        "-i", "-",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-b:v", "1000k",
        "-maxrate", "1500k",
        "-g", str(fps * 2),
        "-flush_packets", "1",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        rtsp_url
    ]
    
    try:
        # Create pipe between libcamera and FFmpeg
        proc1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc2 = subprocess.Popen(ffmpeg_cmd, stdin=proc1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc1.stdout.close()  # Allow proc1 to receive SIGTERM
        
        FFMPEG_PROCESSES[camera_id] = proc2
        print(f"[{camera_id}] Libcamera + FFmpeg started")
        return True
    except Exception as e:
        print(f"[{camera_id}] Failed to start: {e}")
        return False


def stop_stream(camera_id):
    """Stop streaming for a camera"""
    if camera_id not in FFMPEG_PROCESSES:
        print(f"[{camera_id}] No stream to stop")
        return
    
    proc = FFMPEG_PROCESSES[camera_id]
    
    try:
        # Kill process group
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        print(f"[{camera_id}] Stopped")
    except Exception as e:
        print(f"[{camera_id}] Error stopping: {e}")
    
    del FFMPEG_PROCESSES[camera_id]


def stop_all():
    """Stop all streams"""
    for camera_id in list(FFMPEG_PROCESSES.keys()):
        stop_stream(camera_id)


def status_check():
    """Show streaming status"""
    print("\n" + "=" * 50)
    print("Streaming Status")
    print("=" * 50)
    print(f"MediaMTX Server: {MEDIAMTX_HOST}:{MEDIAMTX_PORT}")
    print()
    
    for camera_id, config in CAMERAS.items():
        device = config["device"]
        resolution = config.get("resolution", "640x480")
        fps = config.get("fps", 25)
        
        # Check if streaming
        if camera_id in FFMPEG_PROCESSES:
            proc = FFMPEG_PROCESSES[camera_id]
            if proc.poll() is None:
                status = "✅ Streaming"
            else:
                status = "❌ Crashed"
        else:
            status = "⏹️ Stopped"
        
        # Check camera
        cam_available = check_camera_available(device)
        
        print(f"{camera_id}:")
        print(f"  Status: {status}")
        print(f"  Device: {device} ({'✅' if cam_available else '❌'})")
        print(f"  Resolution: {resolution}")
        print(f"  FPS: {fps}")
        print(f"  RTSP: rtsp://{MEDIAMTX_HOST}:{MEDIAMTX_PORT}/{camera_id}")
        print()
    
    print("=" * 50)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Pi Camera RTSP Streamer")
    parser.add_argument("action", choices=["start", "stop", "status", "restart"],
                        help="Action to perform")
    parser.add_argument("camera_id", nargs="?", help="Specific camera ID")
    
    args = parser.parse_args()
    
    if args.action == "start":
        if args.camera_id:
            # Start specific camera
            if args.camera_id in CAMERAS:
                start_stream_ffmpeg(args.camera_id, CAMERAS[args.camera_id])
            else:
                print(f"Camera {args.camera_id} not found")
                sys.exit(1)
        else:
            # Start all cameras
            print("Starting all cameras...")
            for camera_id, config in CAMERAS.items():
                start_stream_ffmpeg(camera_id, config)
                time.sleep(1)
    
    elif args.action == "stop":
        if args.camera_id:
            stop_stream(args.camera_id)
        else:
            stop_all()
    
    elif args.action == "restart":
        if args.camera_id:
            stop_stream(args.camera_id)
            time.sleep(1)
            start_stream_ffmpeg(args.camera_id, CAMERAS[args.camera_id])
        else:
            stop_all()
            time.sleep(1)
            for camera_id, config in CAMERAS.items():
                start_stream_ffmpeg(camera_id, config)
                time.sleep(1)
    
    elif args.action == "status":
        status_check()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted, stopping streams...")
        stop_all()
