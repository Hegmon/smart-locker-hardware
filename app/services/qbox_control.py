from __future__ import annotations

import os
import subprocess
import threading
from typing import Any

from app.core.mqtt_manager import MQTTManager
from app.services.qbox_runtime import get_qbox_runtime_state
from app.streaming_agent.gpio.relay_controller import RelayController
from app.utils.logger import get_logger
from app.utils.system_info import utc_timestamp


logger = get_logger(__name__)

QBOX_TOPIC_PREFIX = "qbox"
ALLOWED_SERVICE_NAME = "qbox-device.service"


class QBoxControlService:
    def __init__(
        self,
        mqtt_manager: MQTTManager,
        *,
        relay_controller: RelayController | None = None,
    ) -> None:
        self.mqtt = mqtt_manager
        self.relay = relay_controller or RelayController()
        self.runtime_state = get_qbox_runtime_state()
        self.alarm_control_topic = f"{QBOX_TOPIC_PREFIX}/{self.mqtt.device_id}/alarm/control"
        self.alarm_status_topic = f"{QBOX_TOPIC_PREFIX}/{self.mqtt.device_id}/alarm/status"
        self.mqtt_reconnect_topic = f"{QBOX_TOPIC_PREFIX}/{self.mqtt.device_id}/mqtt/reconnect"
        self.mqtt_status_topic = f"{QBOX_TOPIC_PREFIX}/{self.mqtt.device_id}/mqtt/status"
        self.service_restart_topic = f"{QBOX_TOPIC_PREFIX}/{self.mqtt.device_id}/service/restart"
        self.service_status_topic = f"{QBOX_TOPIC_PREFIX}/{self.mqtt.device_id}/service/status"
        self._started = False
        self._lock = threading.RLock()

    def start(self) -> None:
        if self._started:
            return
        try:
            self.relay.start()
        except Exception:
            logger.exception("Relay controller initialization failed for QBox control service")
        self.mqtt.subscribe(self.alarm_control_topic, self._handle_alarm_message, qos=1)
        self.mqtt.subscribe(self.mqtt_reconnect_topic, self._handle_mqtt_reconnect_message, qos=1)
        self.mqtt.subscribe(self.service_restart_topic, self._handle_service_restart_message, qos=1)
        self._started = True
        logger.info(
            "QBox control service subscribed alarm=%s mqtt_reconnect=%s service_restart=%s",
            self.alarm_control_topic,
            self.mqtt_reconnect_topic,
            self.service_restart_topic,
        )

    def stop(self) -> None:
        self._started = False

    def handle_alarm_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"start", "stop"}:
            logger.warning("Invalid alarm control action received: %s", action)
            return self._alarm_response(False, self.runtime_state.alarm_active, detail="invalid_action")

        alarm_active = action == "start"
        logger.info("Alarm control request action=%s alarm_active=%s", action, alarm_active)
        try:
            with self._lock:
                if alarm_active:
                    self.relay.red_led_on()
                    self.relay.buzzer_on()
                else:
                    self.relay.red_led_off()
                    self.relay.buzzer_off()
                self.runtime_state.set_alarm_active(alarm_active)
        except Exception:
            logger.exception("Failed to apply alarm control action=%s", action)
            return self._alarm_response(False, self.runtime_state.alarm_active, detail="hardware_error")

        return self._alarm_response(True, alarm_active)

    def handle_mqtt_reconnect(self, payload: dict[str, Any]) -> dict[str, Any]:
        logger.info("MQTT reconnect request received payload=%s", payload)
        try:
            connected = self.mqtt.restart_connection(timeout_seconds=15.0)
        except Exception:
            logger.exception("MQTT reconnect request failed")
            connected = False

        last_reconnect = self.runtime_state.last_mqtt_reconnect
        if connected:
            last_reconnect = utc_timestamp()
            self.runtime_state.set_last_mqtt_reconnect(last_reconnect)
            logger.info("MQTT reconnect completed successfully timestamp=%s", last_reconnect)
        else:
            logger.warning("MQTT reconnect did not complete successfully")

        return {
            "connected": connected,
            "last_reconnect": last_reconnect,
        }

    def handle_service_restart(self, payload: dict[str, Any]) -> dict[str, Any]:
        service_name = str(payload.get("service") or "").strip()
        logger.info("Service restart request received service=%s payload=%s", service_name, payload)
        if service_name != ALLOWED_SERVICE_NAME:
            logger.warning("Rejected restart request for invalid service=%s", service_name)
            return {
                "service": service_name or ALLOWED_SERVICE_NAME,
                "success": False,
                "timestamp": utc_timestamp(),
                "detail": "invalid_service",
            }

        success = self._restart_service(service_name)
        return {
            "service": service_name,
            "success": success,
            "timestamp": utc_timestamp(),
        }

    def _handle_alarm_message(self, topic: str, payload: bytes) -> None:
        command = self.mqtt.loads(payload)
        response = self.handle_alarm_control(command)
        self.mqtt.publish_json(self.alarm_status_topic, response, qos=1, retain=True)

    def _handle_mqtt_reconnect_message(self, topic: str, payload: bytes) -> None:
        command = self.mqtt.loads(payload)
        response = self.handle_mqtt_reconnect(command)
        self.mqtt.publish_json(self.mqtt_status_topic, response, qos=1, retain=True)

    def _handle_service_restart_message(self, topic: str, payload: bytes) -> None:
        command = self.mqtt.loads(payload)
        response = self.handle_service_restart(command)
        self.mqtt.publish_json(self.service_status_topic, response, qos=1, retain=True)

    def _alarm_response(self, success: bool, alarm_active: bool, *, detail: str | None = None) -> dict[str, Any]:
        response: dict[str, Any] = {
            "success": success,
            "alarm_active": alarm_active,
            "timestamp": utc_timestamp(),
        }
        if detail:
            response["detail"] = detail
        return response

    def _restart_service(self, service_name: str) -> bool:
        command = ["systemctl", "restart", service_name]
        if os.geteuid() != 0:
            command = ["sudo", "-n", *command]

        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=15.0)
        except Exception:
            logger.exception("systemctl restart failed for %s", service_name)
            return False

        if result.returncode != 0:
            logger.warning(
                "systemctl restart returned rc=%s for %s stdout=%s stderr=%s",
                result.returncode,
                service_name,
                (result.stdout or "").strip(),
                (result.stderr or "").strip(),
            )
            return False

        logger.info("systemctl restart succeeded for %s", service_name)
        return True
