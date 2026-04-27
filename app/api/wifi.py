from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.wifi_manager import (
    WifiCommandError,
    connect_wifi,
    disconnect_wifi,
    get_wifi_status,
    scan_wifi,
    start_hotspot,
)


router = APIRouter(prefix="/wifi", tags=["wifi"])


class WifiRequest(BaseModel):
    ssid: str = Field(..., min_length=1, max_length=128)
    password: str = Field(default="", max_length=128)


@router.get("/scan")
def scan() -> dict:
    try:
        return {"networks": scan_wifi()}
    except WifiCommandError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/status")
def status() -> dict:
    try:
        return get_wifi_status()
    except WifiCommandError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/connect")
def connect(data: WifiRequest) -> dict:
    try:
        return connect_wifi(data.ssid, data.password)
    except WifiCommandError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/disconnect")
def disconnect() -> dict:
    try:
        return disconnect_wifi()
    except WifiCommandError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/hotspot/start")
def hotspot_start() -> dict:
    try:
        return start_hotspot()
    except WifiCommandError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
