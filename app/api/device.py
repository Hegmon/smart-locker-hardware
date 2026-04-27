from fastapi import APIRouter

router = APIRouter(prefix="/device")


@router.get("/heartbeat")
def heartbeat():
    return {"status": "alive"}