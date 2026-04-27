from fastapi import APIRouter

from app.services.backend_sync import get_backend_sync_status, register_device, send_telemetry


router = APIRouter(prefix="/device")


@router.get("/heartbeat")
def heartbeat():
    return {"status": "alive"}


@router.get("/backend/status")
def backend_status() -> dict:
    return get_backend_sync_status()


@router.post("/backend/register")
def backend_register() -> dict:
    return register_device(force=False)


@router.post("/backend/telemetry")
def backend_telemetry() -> dict:
    return send_telemetry()
