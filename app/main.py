from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import device, hardware, system, wifi
from app.deployment.bootstrap import bootstrap_device
from app.deployment.device_identity import ensure_device_id
from app.deployment.validation import validate_runtime_configuration
from app.services.backend_sync import register_device_if_needed
from app.utils.logger import get_logger


logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        bootstrap_device()
        validate_runtime_configuration()
        register_device_if_needed()
    except Exception as exc:  # pragma: no cover
        logger.warning("Backend device registration skipped: %s", exc)
    yield


app = FastAPI(title="Smart Locker Device API", version="v1.0.0", lifespan=lifespan)

app.include_router(wifi.router)
app.include_router(device.router)
app.include_router(hardware.router)
app.include_router(system.router)


@app.get("/")
def root() -> dict:
    return {
        "status": "online",
        "service": "smart-locker-device-api",
        "device_id": ensure_device_id(),
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "device-registry", "device_id": ensure_device_id()}
