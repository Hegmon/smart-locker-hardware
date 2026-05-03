from __future__ import annotations

import socket
from datetime import datetime, timezone
from json import JSONDecodeError
from typing import Any
from urllib.parse import urlsplit

import requests

from app.core.config import (
    APP_VERSION,
    LOCKER_DEFAULT_STATUS,
    QBOX_AUTO_REGISTER,
    QBOX_BACKEND_TIMEOUT_SECONDS,
    QBOX_DEVICE_NAME,
    QBOX_DEVICE_REGISTRATION_URL,
    QBOX_TELEMETRY_URL,
)
from app.streaming_agent.device_config import get_optional_config
from app.services.backend_state import load_backend_state, save_backend_state
from app.services.hardware_manager import get_camera_inventory, get_system_metrics
from app.utils.logger import get_logger


logger = get_logger(__name__)


class BackendSyncError(Exception):
    def __init__(self, message: str, *, status_code: int = 500, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail or {"message": message}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_ip_address() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _parse_response_body(response: requests.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except JSONDecodeError:
        return response.text


def _build_registration_payload() -> dict[str, Any]:
    version = APP_VERSION
    if not version.startswith('v'):
        version = f"v{version}"

    # Allow the device to advertise a public stream hostname instead of its
    # private LAN address when the backend generates playback URLs.
    public_base_url = get_optional_config("STREAM_PUBLIC_BASE_URL")
    public_host = get_optional_config("STREAM_PUBLIC_HOST")
    if public_base_url and not public_host:
        public_host = urlsplit(public_base_url).hostname or ""

    return {
        "name": QBOX_DEVICE_NAME,
        "ip_address": public_host or _get_ip_address(),
        "version": version,
        "is_active": True,
        "status": "Online",
        "last_seen": _utc_now_iso(),
    }


def _normalize_registration_status(status: str) -> str:
    if status in {"Online", "Offline", "Error"}:
        return status
    return "Online"


def _normalize_locker_status(status: str) -> str:
    normalized = status.strip().upper()
    if normalized in {"LOCKED", "UNLOCKED"}:
        return normalized
    return "LOCKED"


def _camera_status(connected: bool) -> str:
    return "ONLINE" if connected else "OFFLINE"


def get_backend_sync_status() -> dict[str, Any]:
    state = load_backend_state()
    return {
        "auto_register_enabled": QBOX_AUTO_REGISTER,
        "registered": bool(state.get("device_uuid")),
        "device_uuid": state.get("device_uuid"),
        "device_id": state.get("device_id"),
        "name": state.get("name", QBOX_DEVICE_NAME),
        "registration_url": QBOX_DEVICE_REGISTRATION_URL,
        "telemetry_url": QBOX_TELEMETRY_URL,
        "last_registration_at": state.get("registered_at"),
        "last_telemetry_at": state.get("last_telemetry_at"),
    }


def register_device(force: bool = False) -> dict[str, Any]:
    state = load_backend_state()
    if state.get("device_uuid") and not force:
        return {
            "registered": True,
            "skipped": True,
            "device_uuid": state["device_uuid"],
            "device_id": state.get("device_id"),
            "backend_response": state,
        }

    payload = _build_registration_payload()
    print(f"[backend-register] POST {QBOX_DEVICE_REGISTRATION_URL}", flush=True)
    print(f"[backend-register] payload={payload}", flush=True)
    logger.info("Registering Qbox device with payload: %s", payload)

    try:
        response = requests.post(
            QBOX_DEVICE_REGISTRATION_URL,
            json=payload,
            timeout=QBOX_BACKEND_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        detail = {
            "message": "Failed to call Django registration API.",
            "url": QBOX_DEVICE_REGISTRATION_URL,
            "payload": payload,
            "error": str(exc),
        }
        print(f"[backend-register] request error={detail}", flush=True)
        logger.exception("Django registration API request failed")
        raise BackendSyncError("Registration request failed", status_code=502, detail=detail) from exc

    response_body = _parse_response_body(response)
    print(
        f"[backend-register] response_status={response.status_code} response_body={response_body}",
        flush=True,
    )
    logger.info("Registration response status=%s body=%s", response.status_code, response_body)
    if response.status_code >= 400:
        raise BackendSyncError(
            "Registration API returned an error",
            status_code=response.status_code,
            detail={
                "message": "Django registration API returned an error.",
                "url": QBOX_DEVICE_REGISTRATION_URL,
                "payload": payload,
                "backend_status_code": response.status_code,
                "backend_response": response_body,
            },
        )

    if not isinstance(response_body, dict):
        raise BackendSyncError(
            "Registration API returned a non-object response",
            status_code=502,
            detail={
                "message": "Django registration API returned an unexpected response.",
                "url": QBOX_DEVICE_REGISTRATION_URL,
                "payload": payload,
                "backend_status_code": response.status_code,
                "backend_response": response_body,
            },
        )

    body = response_body
    device_uuid = body.get("id")
    if not device_uuid:
        raise BackendSyncError(
            "Registration response did not include device UUID",
            status_code=502,
            detail={
                "message": "Django registration API did not return `id`.",
                "url": QBOX_DEVICE_REGISTRATION_URL,
                "payload": payload,
                "backend_status_code": response.status_code,
                "backend_response": body,
            },
        )

    updated_state = {
        "device_uuid": device_uuid,
        "device_id": body.get("device_id"),
        "name": body.get("name", QBOX_DEVICE_NAME),
        "ip_address": body.get("ip_address") or payload["ip_address"],
        "version": body.get("version", APP_VERSION),
        "status": _normalize_registration_status(body.get("status", "Online")),
        "registered_at": _utc_now_iso(),
    }
    save_backend_state(updated_state)
    logger.info("Registered Qbox device with backend: %s", updated_state["device_uuid"])

    return {
        "registered": True,
        "skipped": False,
        "device_uuid": updated_state["device_uuid"],
        "device_id": updated_state.get("device_id"),
        "request_payload": payload,
        "backend_status_code": response.status_code,
        "backend_response": body,
    }


def register_device_if_needed() -> dict[str, Any]:
    if not QBOX_AUTO_REGISTER:
        return {"registered": False, "skipped": True, "reason": "auto registration disabled"}
    return register_device(force=False)


def build_telemetry_payload() -> dict[str, Any]:
    state = load_backend_state()
    device_uuid = state.get("device_uuid")
    if not device_uuid:
        raise RuntimeError("Device is not registered yet. No device UUID is stored.")

    system_metrics = get_system_metrics()
    cameras = get_camera_inventory()

    return {
        "device": device_uuid,
        "cpu_usage": system_metrics["cpu_usage"],
        "ram_usage": system_metrics["ram_usage"],
        "external_camera_status": _camera_status(cameras["external_camera"]["connected"]),
        "internal_camera_status": _camera_status(cameras["internal_camera"]["connected"]),
        "locker_status": _normalize_locker_status(LOCKER_DEFAULT_STATUS),
    }


def send_telemetry() -> dict[str, Any]:
    payload = build_telemetry_payload()
    response = requests.post(
        QBOX_TELEMETRY_URL,
        json=payload,
        timeout=QBOX_BACKEND_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    state = load_backend_state()
    state["last_telemetry_at"] = _utc_now_iso()
    save_backend_state(state)
    logger.info("Telemetry sent for device %s", payload["device"])

    return {
        "sent": True,
        "device_uuid": payload["device"],
        "payload": payload,
        "response": response.json() if response.content else None,
    }
