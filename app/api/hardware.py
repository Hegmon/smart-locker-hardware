from fastapi import APIRouter

from app.services.hardware_manager import get_camera_inventory, get_light_status, get_system_hardware_status


router = APIRouter(prefix="/hardware", tags=["hardware"])


@router.get("/status")
def hardware_status() -> dict:
    return get_system_hardware_status()


@router.get("/lights/status")
def lights_status() -> dict:
    return get_light_status()


@router.get("/cameras/status")
def cameras_status() -> dict:
    return get_camera_inventory()
