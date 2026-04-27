from fastapi import APIRouter, HTTPException

from app.services.backend_sync import BackendSyncError, get_backend_sync_status, register_device, send_telemetry


router = APIRouter(prefix="/device")


@router.get("/heartbeat")
def heartbeat():
    return {"status": "alive"}


@router.get("/backend/status")
def backend_status() -> dict:
    return get_backend_sync_status()


@router.post("/backend/register")
def backend_register() -> dict:
    try:
        return register_device(force=False)
    except BackendSyncError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/backend/telemetry")
def backend_telemetry() -> dict:
    return send_telemetry()
