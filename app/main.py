from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import device, hardware, system, wifi
from app.services.backend_sync import register_device_if_needed
from app.utils.logger import get_logger


logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        register_device_if_needed()
    except Exception as exc:  # pragma: no cover
        logger.warning("Backend device registration skipped: %s", exc)
    yield


app = FastAPI(title="Smart Locker Device API", version="1.0.0", lifespan=lifespan)

app.include_router(wifi.router)
app.include_router(device.router)
app.include_router(hardware.router)
app.include_router(system.router)


@app.get("/")
def root() -> dict:
    return {"status": "online", "service": "smart-locker-device-api"}
