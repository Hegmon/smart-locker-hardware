from fastapi import FastAPI

from app.api import device, system, wifi


app = FastAPI(title="Smart Locker Device API", version="1.0.0")

app.include_router(wifi.router)
app.include_router(device.router)
app.include_router(system.router)


@app.get("/")
def root() -> dict:
    return {"status": "online", "service": "smart-locker-device-api"}
