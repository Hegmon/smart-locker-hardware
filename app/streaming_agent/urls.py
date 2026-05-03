"""
Stream URL Generation Utilities
Build HLS and RTSP URLs for device streams.
Ensures consistency across device and backend integrations.
"""

from __future__ import annotations

import socket
from typing import Optional

from .device_config import get_optional_config
from .constants import (
    RTSP_URL_TEMPLATE,
    MEDIAMTX_HOST,
    MEDIAMTX_HLS_PORT,
    MEDIAMTX_RTSP_PORT,
    STREAM_TYPE_INTERNAL,
    STREAM_TYPE_EXTERNAL,
)


def _normalize_base_path(base_path: str) -> str:
    base_path = (base_path or "").strip()
    if not base_path:
        return ""
    return "/" + base_path.strip("/")


def _strip_trailing_slash(value: str) -> str:
    return value.rstrip("/")


def _resolve_public_base_url() -> str:
    """
    Resolve the public playback base URL used by mobile/web clients.
    """
    explicit_base_url = get_optional_config("STREAM_PUBLIC_BASE_URL")
    if explicit_base_url:
        return _strip_trailing_slash(explicit_base_url)

    scheme = get_optional_config("STREAM_PUBLIC_SCHEME", "http") or "http"
    host = get_optional_config("STREAM_PUBLIC_HOST") or get_lan_ip_address()
    port_value = get_optional_config("STREAM_PUBLIC_PORT")
    base_path = _normalize_base_path(get_optional_config("STREAM_PUBLIC_BASE_PATH"))

    port: Optional[int]
    if port_value:
        try:
            port = int(port_value)
        except ValueError:
            port = MEDIAMTX_HLS_PORT
    else:
        port = MEDIAMTX_HLS_PORT

    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None

    return _build_http_base_url(scheme, host, port, base_path)


def _build_http_base_url(scheme: str, host: str, port: Optional[int], base_path: str = "") -> str:
    base = f"{scheme}://{host}"
    if port is not None:
        base = f"{base}:{port}"
    return f"{base}{base_path}"


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
    
    if host is None and port is None:
        base_url = _resolve_public_base_url()
        return f"{base_url}/hls/{device_id}/{stream_type}/index.m3u8"
    else:
        if host is None:
            host = get_lan_ip_address()
        if port is None:
            port = MEDIAMTX_HLS_PORT

    base_url = _build_http_base_url("http", host, port)
    return f"{base_url}/hls/{device_id}/{stream_type}/index.m3u8"


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
        host = get_optional_config("MEDIAMTX_HOST") or MEDIAMTX_HOST
    
    if port is None:
        port_value = get_optional_config("MEDIAMTX_RTSP_PORT")
        if port_value:
            try:
                port = int(port_value)
            except ValueError:
                port = MEDIAMTX_RTSP_PORT
        else:
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
    
    result = {}
    for st in [STREAM_TYPE_INTERNAL, STREAM_TYPE_EXTERNAL]:
        result[st] = {
            "hls": build_hls_url(st, device_id=device_id, host=host),
            "rtsp": build_rtsp_url(st, device_id=device_id, host=host),
        }
    return result
