"""
Stream URL Generation Utilities
Build HLS and RTSP URLs for device streams.
Ensures consistency across device and backend integrations.
"""

from __future__ import annotations

import socket
from typing import Optional

from .constants import (
    HLS_URL_TEMPLATE,
    RTSP_URL_TEMPLATE,
    MEDIAMTX_HOST,
    MEDIAMTX_HLS_PORT,
    MEDIAMTX_RTSP_PORT,
    STREAM_TYPE_INTERNAL,
    STREAM_TYPE_EXTERNAL,
)


def get_lan_ip_address() -> str:
    """
    Get the device's primary LAN IP address.
    Returns 127.0.0.1 if no network is available.
    """
    try:
        # Trick: connect to a public DNS and get the local address used
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        pass
    
    # Fallback: try hostname
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    
    return "127.0.0.1"


def build_hls_url(
    stream_type: str,
    *,
    device_id: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> str:
    """
    Build HLS playback URL for a stream.
    
    Args:
        stream_type: "internal" or "external"
        device_id: device identifier (reads from config if None)
        host: server host (auto-detected if None)
        port: HLS port (default from config)
    
    Returns:
        Full HLS URL, e.g. http://192.168.1.10:8888/hls/QBOX-001/internal/index.m3u8
    """
    if device_id is None:
        from .device_config import load_device_id
        device_id = load_device_id()
    
    if host is None:
        host = get_lan_ip_address()
    
    if port is None:
        port = MEDIAMTX_HLS_PORT
    
    return HLS_URL_TEMPLATE.format(
        host=host,
        port=port,
        device_id=device_id,
        stream_type=stream_type,
    )


def build_rtsp_url(
    stream_type: str,
    *,
    device_id: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> str:
    """
    Build RTSP publishing URL (used by FFmpeg).
    
    Args:
        stream_type: "internal" or "external"
        device_id: device identifier
        host: MediaMTX host (default localhost)
        port: RTSP port (default 8554)
    
    Returns:
        RTSP URL, e.g. rtsp://127.0.0.1:8554/QBOX-001/internal
    """
    if device_id is None:
        from .device_config import load_device_id
        device_id = load_device_id()
    
    if host is None:
        host = MEDIAMTX_HOST  # default 127.0.0.1
    
    if port is None:
        port = MEDIAMTX_RTSP_PORT
    
    return RTSP_URL_TEMPLATE.format(
        host=host,
        port=port,
        device_id=device_id,
        stream_type=stream_type,
    )


def get_all_stream_urls(
    device_id: Optional[str] = None,
    host: Optional[str] = None,
) -> dict[str, dict[str, str]]:
    """
    Get all stream URLs (HLS and RTSP) for both camera types.
    
    Returns:
        {
            "internal": {"hls": "...", "rtsp": "..."},
            "external": {"hls": "...", "rtsp": "..."},
        }
    """
    if device_id is None:
        from .device_config import load_device_id
        device_id = load_device_id()
    
    if host is None:
        host = get_lan_ip_address()
    
    result = {}
    for st in [STREAM_TYPE_INTERNAL, STREAM_TYPE_EXTERNAL]:
        result[st] = {
            "hls": build_hls_url(st, device_id=device_id, host=host),
            "rtsp": build_rtsp_url(st, device_id=device_id, host=host),
        }
    return result
