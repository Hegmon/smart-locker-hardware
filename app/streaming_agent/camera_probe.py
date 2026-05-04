#!/usr/bin/env python3
"""
Camera probing utility for streaming agent.
Probes V4L2 devices to determine if they're valid cameras and what formats they support.
"""

import subprocess
import sys
from pathlib import Path

def probe_device(dev_path: str) -> dict:
    """Probe a V4L2 device and return its capabilities."""
    result = {
        "device": dev_path,
        "is_camera": False,
        "formats": [],
        "bus": "unknown",
        "name": "",
        "error": None,
    }

    # Get device name from sysfs
    try:
        name_file = Path(f"/sys/class/video4linux/{Path(dev_path).name}/name")
        if name_file.exists():
            result["name"] = name_file.read_text().strip()
    except Exception:
        pass

    # Get bus info
    try:
        video_dir = Path(f"/sys/class/video4linux/{Path(dev_path).name}")
        if video_dir.exists():
            device_link = video_dir / "device"
            if device_link.exists() and device_link.is_symlink():
                target = device_link.readlink()
                target_str = str(target)
                if "/usb" in target_str:
                    result["bus"] = "usb"
                elif "/platform" in target_str:
                    result["bus"] = "platform"
                elif "/pci" in target_str:
                    result["bus"] = "pci"
    except Exception:
        pass

    # Skip known non-camera devices
    skip_keywords = ["codec", "isp", "hevc", "h264", "h265", "encoder", "decoder"]
    name_lower = result["name"].lower()
    if any(kw in name_lower for kw in skip_keywords):
        result["error"] = f"Non-camera device: {result['name']}"
        return result

    # Probe formats using v4l2-ctl if available
    try:
        proc = subprocess.run(
            ["v4l2-ctl", "--device", dev_path, "--list-formats"],
            capture_output=True, text=True, timeout=3
        )
        if proc.returncode == 0:
            # Parse format list
            for line in proc.stdout.splitlines():
                line = line.strip()
                if line.startswith("'") and "'" in line:
                    fmt = line.split("'")[1]
                    result["formats"].append(fmt)
            if result["formats"]:
                result["is_camera"] = True
        else:
            # Device exists but may not be a camera
            result["error"] = f"v4l2-ctl error: {proc.stderr.strip()}"
    except FileNotFoundError:
        # v4l2-ctl not available; fall back to heuristics
        result["is_camera"] = True  # Assume it's a camera if we can't probe
    except Exception as e:
        result["error"] = f"Probe failed: {e}"

    return result

def main():
    import json
    devices = sorted(Path("/dev").glob("video*"))

    if not devices:
        print(json.dumps({"error": "No /dev/video* devices found"}))
        sys.exit(1)

    results = []
    for dev in devices:
        dev_path = str(dev)
        info = probe_device(dev_path)
        results.append(info)

    # Print JSON summary
    print(json.dumps({
        "devices": results,
        "count": len(results),
        "cameras": [d for d in results if d["is_camera"]]
    }, indent=2))

if __name__ == "__main__":
    main()
