"""
Stream Verification
Checks that RTSP and HLS streams are accessible and valid.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Optional, Tuple
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

import requests

from .constants import VERIFY_TIMEOUT, VERIFY_RETRY_COUNT, MEDIAMTX_HOST, MEDIAMTX_HLS_PORT
from .urls import build_hls_url, build_rtsp_url

logger = logging.getLogger(__name__)


def check_rtsp_stream(rtsp_url: str, timeout: int = VERIFY_TIMEOUT) -> Tuple[bool, Optional[str]]:
    """
    Check if RTSP stream is accessible using ffprobe.
    Returns (is_accessible, error_message)
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-rtsp_transport", "tcp",
        "-timeout", str(timeout * 1000000),  # microseconds
        "-i", rtsp_url,
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.returncode == 0:
            logger.debug("RTSP stream verified: %s", rtsp_url)
            return True, None
        else:
            error = result.stderr.strip() or f"ffprobe returned {result.returncode}"
            logger.debug("RTSP verification failed: %s - %s", rtsp_url, error)
            return False, error
    except subprocess.TimeoutExpired:
        return False, "Timeout probing RTSP stream"
    except FileNotFoundError:
        logger.error("ffprobe not found - install FFmpeg")
        return False, "ffprobe not available"
    except Exception as exc:
        logger.exception("RTSP verification error")
        return False, str(exc)


def check_hls_playlist(hls_url: str, timeout: int = VERIFY_TIMEOUT) -> Tuple[bool, Optional[str]]:
    """
    Check if HLS playlist exists and is valid.
    Returns (is_valid, error_message)
    """
    session = requests.Session()
    session.timeout = timeout
    
    try:
        response = session.get(hls_url, timeout=timeout)
        response.raise_for_status()
        
        content = response.text
        lines = content.strip().split('\n')
        
        # Basic HLS validation
        if not lines:
            return False, "Empty playlist"
        
        if not lines[0].startswith("#EXTM3U"):
            return False, "Missing #EXTM3U header"
        
        # Check for at least one media segment or variant
        has_segment = any(not line.startswith('#') and line.strip() for line in lines[1:])
        has_variant = any(line.startswith("#EXT-X-STREAM-INF") for line in lines)
        
        if has_segment or has_variant:
            logger.debug("HLS playlist verified: %s", hls_url)
            return True, None
        else:
            return False, "No media segments or variants found"
    
    except HTTPError as e:
        return False, f"HTTP {e.response.status_code}"
    except URLError as e:
        return False, f"URL error: {e.reason}"
    except requests.RequestException as exc:
        logger.exception("HLS verification error")
        return False, str(exc)
    except Exception as exc:
        logger.exception("HLS verification error")
        return False, str(exc)


class StreamVerifier:
    """Manages verification of multiple streams"""
    
    def __init__(
        self,
        device_id: str,
        mediamtx_host: str = MEDIAMTX_HOST,
        rtsp_port: int = 8554,
        hls_port: int = MEDIAMTX_HLS_PORT,
    ):
        self.device_id = device_id
        self.mediamtx_host = mediamtx_host
        self.rtsp_port = rtsp_port
        self.hls_port = hls_port
    
    def build_hls_url(self, stream_type: str) -> str:
        """Build HLS URL for a stream type"""
        return build_hls_url(stream_type, device_id=self.device_id)
    
    def build_rtsp_url(self, stream_type: str) -> str:
        """Build RTSP URL for a stream type"""
        return build_rtsp_url(
            stream_type,
            device_id=self.device_id,
            host=self.mediamtx_host,
            port=self.rtsp_port,
        )
    
    def verify_stream(self, stream_type: str) -> Tuple[bool, Optional[str], dict]:
        """
        Verify both RTSP and HLS for a stream type.
        Returns (all_ok, error_message, details_dict)
        """
        rtsp_url = self.build_rtsp_url(stream_type)
        hls_url = self.build_hls_url(stream_type)
        
        logger.info("Verifying stream %s: RTSP=%s, HLS=%s", stream_type, rtsp_url, hls_url)
        
        details = {
            "stream_type": stream_type,
            "rtsp_url": rtsp_url,
            "hls_url": hls_url,
            "rtsp_ok": False,
            "hls_ok": False,
            "rtsp_error": None,
            "hls_error": None,
        }
        
        # Check RTSP
        rtsp_ok, rtsp_error = check_rtsp_stream(rtsp_url)
        details["rtsp_ok"] = rtsp_ok
        details["rtsp_error"] = rtsp_error
        
        # Check HLS
        hls_ok, hls_error = check_hls_playlist(hls_url)
        details["hls_ok"] = hls_ok
        details["hls_error"] = hls_error
        
        all_ok = rtsp_ok and hls_ok
        error_msg = None
        if not all_ok:
            errors = []
            if not rtsp_ok:
                errors.append(f"RTSP: {rtsp_error or 'unavailable'}")
            if not hls_ok:
                errors.append(f"HLS: {hls_error or 'unavailable'}")
            error_msg = "; ".join(errors)
        
        return all_ok, error_msg, details
    
    def verify_all_streams(self, stream_types: list[str]) -> dict[str, dict]:
        """
        Verify multiple streams and return summary.
        """
        results = {}
        for st in stream_types:
            ok, error, details = self.verify_stream(st)
            results[st] = {
                "ok": ok,
                "error": error,
                "details": details,
            }
        return results
