#!/usr/bin/env python3
"""
Stream Verification Script
Checks that RTSP and HLS streams are healthy.
Can be run manually or as a systemd timer/cron job.

Exit codes:
  0 - all streams OK
  1 - one or more streams failed
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.streaming_agent.device_config import load_device_id
from app.streaming_agent.stream_verifier import StreamVerifier


def main():
    # Load device_id
    try:
        device_id = load_device_id()
    except Exception as e:
        print(f"❌ Failed to load device_id: {e}")
        return 1
    
    print(f"🔍 Verifying streams for device: {device_id}")
    
    verifier = StreamVerifier(device_id=device_id)
    
    # Check both stream types
    stream_types = ["internal", "external"]
    
    results = {}
    all_ok = True
    
    for stream_type in stream_types:
        ok, error, details = verifier.verify_stream(stream_type)
        results[stream_type] = {"ok": ok, "error": error, "details": details}
        
        status_icon = "✅" if ok else "❌"
        print(f"{status_icon} {stream_type}: ", end="")
        if ok:
            print("OK")
            print(f"   RTSP: {details['rtsp_url']}")
            print(f"   HLS:  {details['hls_url']}")
        else:
            print(f"FAILED - {error}")
            all_ok = False
    
    # Print summary
    print()
    if all_ok:
        print("✅ All streams verified successfully")
        return 0
    else:
        print("❌ Some streams are not healthy")
        for st, r in results.items():
            if not r["ok"]:
                print(f"   {st}: {r['error']}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
