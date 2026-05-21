from contextlib import asynccontextmanager
import signal
import threading
import time

from fastapi import FastAPI

from app.agents.control_agent import ControlAgent
from app.agents.telemetry_agent import HeartbeatAgent, TelemetryAgent
from app.api import device, hardware, system, wifi
from app.core.mqtt_manager import get_shared_mqtt_manager
from app.deployment.bootstrap import bootstrap_device
from app.deployment.device_identity import ensure_device_id
from app.deployment.validation import validate_runtime_configuration
from app.services.backend_sync import register_device_if_needed
from app.utils.logger import get_logger


logger = get_logger(__name__)


class DeviceApplication:
    def __init__(self):
        self.mqtt = get_shared_mqtt_manager()
        self.telemetry = TelemetryAgent(self.mqtt, interval_seconds=30)
        self.heartbeat = HeartbeatAgent(self.mqtt, interval_seconds=60)
        self.control = ControlAgent(self.mqtt)
        self.wifi_agent = None
        self.streaming_agent = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        logger.info("Starting Smart Locker device runtime")
        bootstrap_device()
        validate_runtime_configuration()
        register_device_if_needed()

        self.mqtt.start()
        self.control.start()
        self.telemetry.start()
        self.heartbeat.start()

        self._start_wifi_agent()
        self._start_streaming_agent()
        logger.info("Smart Locker device runtime started")

    def _start_wifi_agent(self) -> None:
        try:
            from app.agents.wifi_agent import WifiAgent

            self.wifi_agent = WifiAgent()
            self.wifi_agent.start()
        except Exception:
            self.wifi_agent = None
            logger.exception("WiFi agent failed to start; continuing device runtime")

    def _start_streaming_agent(self) -> None:
        try:
            from app.agents.streaming_agent import StreamingAgent

            self.streaming_agent = StreamingAgent()
            self.streaming_agent.start()
        except Exception:
            self.streaming_agent = None
            logger.exception("Streaming agent failed to start; continuing device runtime")

    def stop(self) -> None:
        logger.info("Stopping Smart Locker device runtime")
        self._stop_event.set()
        for agent in (self.streaming_agent, self.wifi_agent, self.control, self.heartbeat, self.telemetry):
            if agent is None:
                continue
            try:
                agent.stop()
            except Exception:
                logger.exception("Agent shutdown failed: %s", agent.__class__.__name__)
        self.mqtt.stop(publish_offline=True)
        logger.info("Smart Locker device runtime stopped")

    def run_forever(self) -> None:
        self.start()
        while not self._stop_event.is_set():
            time.sleep(1)


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


def main() -> None:
    runtime = DeviceApplication()

    def _handle_signal(signum, frame):
        logger.info("Signal received: %s", signum)
        runtime.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        runtime.run_forever()
    except KeyboardInterrupt:
        runtime.stop()
    except Exception:
        logger.exception("Device runtime failed")
        runtime.stop()
        raise


if __name__ == "__main__":
    main()
