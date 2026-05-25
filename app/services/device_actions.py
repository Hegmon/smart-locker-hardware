from __future__ import annotations

import subprocess
import threading
from typing import Any, Callable

from app.core.mqtt_manager import MQTTManager
from app.services.factory_reset import FactoryResetService
from app.utils.logger import get_logger
from app.utils.system_info import utc_timestamp


logger = get_logger(__name__)
ShutdownCallback = Callable[[], None]


class DeviceActionService:
    """Handles remote device-management commands delivered over MQTT."""

    SUPPORTED_ACTIONS = {"reboot", "factory_reset"}

    def __init__(self, mqtt_manager: MQTTManager, *, shutdown_callback: ShutdownCallback | None = None):
        self.mqtt = mqtt_manager
        self.shutdown_callback = shutdown_callback
        self.actions_topic = f"{self.mqtt.topic_prefix}/{self.mqtt.device_id}/actions"
        self.status_topic = f"{self.mqtt.topic_prefix}/{self.mqtt.device_id}/action_status"
        self._started = False
        self._action_lock = threading.Lock()

    def start(self) -> None:
        if self._started:
            return
        self.mqtt.subscribe(self.actions_topic, self._handle_message, qos=1)
        self._started = True
        logger.info("Device action service subscribed to %s", self.actions_topic)

    def stop(self) -> None:
        self._started = False

    def _handle_message(self, topic: str, payload: bytes) -> None:
        try:
            command = self.mqtt.loads(payload)
            action = str(command.get("action") or "").strip().lower()
        except Exception:
            logger.exception("Malformed device action payload on %s", topic)
            self._publish_status("unknown", "rejected", detail="malformed_payload")
            return

        if action not in self.SUPPORTED_ACTIONS:
            logger.warning("Unsupported remote action received: %s", action)
            self._publish_status(action or "unknown", "rejected", detail="unsupported_action")
            return

        if not self._action_lock.acquire(blocking=False):
            logger.warning("Remote action rejected because another action is already running: %s", action)
            self._publish_status(action, "rejected", detail="action_in_progress")
            return

        logger.warning("Remote action accepted action=%s", action)
        worker = threading.Thread(
            target=self._execute_action,
            args=(action,),
            daemon=True,
            name=f"device-action-{action}",
        )
        worker.start()

    def _execute_action(self, action: str) -> None:
        try:
            if action == "reboot":
                self._execute_reboot()
            elif action == "factory_reset":
                self._execute_factory_reset()
        finally:
            self._action_lock.release()

    def _execute_reboot(self) -> None:
        logger.warning("Executing remote reboot request")
        self._publish_status("reboot", "executing")
        self._graceful_shutdown()
        self.mqtt.stop(publish_offline=True)
        self._run_reboot()

    def _execute_factory_reset(self) -> None:
        logger.warning("Executing remote factory reset request")
        self._publish_status("factory_reset", "executing", detail="started")

        def progress(step: str, status: str) -> None:
            self._publish_status("factory_reset", status, detail=step)

        result = FactoryResetService(progress=progress).run()
        if not result.success:
            self._publish_status("factory_reset", "error", detail="; ".join(result.errors[:3]))
            logger.error("Factory reset completed with errors: %s", result.errors)
            return

        self._publish_status("factory_reset", "executing", detail="rebooting")
        self._graceful_shutdown()
        self.mqtt.stop(publish_offline=True)
        self._run_reboot()

    def _graceful_shutdown(self) -> None:
        if not self.shutdown_callback:
            return
        try:
            self.shutdown_callback()
        except Exception:
            logger.exception("Graceful shutdown callback failed before remote action")

    def _run_reboot(self) -> None:
        try:
            subprocess.Popen(["sudo", "reboot"])
        except Exception:
            logger.exception("Failed to execute sudo reboot")
            self._publish_status("reboot", "error", detail="sudo_reboot_failed")

    def _publish_status(self, action: str, status: str, *, detail: str | None = None) -> bool:
        payload: dict[str, Any] = {
            "action": action,
            "status": status,
            "timestamp": utc_timestamp(),
        }
        if detail:
            payload["detail"] = detail
        return self.mqtt.publish_json(self.status_topic, payload, qos=1)
