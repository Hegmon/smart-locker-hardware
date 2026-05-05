from fastapi import APIRouter

from app.deployment.device_identity import ensure_device_id
from app.services.system_status import build_system_status

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "device-registry", "device_id": ensure_device_id()}


@router.get("/status")
def system_status() -> dict:
    return build_system_status()
