"""
MQTT Handler for Streaming Agent
Handles stream control commands via MQTT.
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from typing import Any, Callable, Optional

import paho.mqtt.client as mqtt

from .constants import CAMERA_EXTERNAL, CAMERA_INTERNAL, STREAM_TYPE_EXTERNAL, STREAM_TYPE_INTERNAL

logger = logging.getLogger(__name__)


class StreamingMQTTClient:
    """MQTT client specialized for streaming control commands"""
    
    # Exponential backoff configuration for reconnect
    RECONNECT_BACKOFF_BASE = 1.0  # seconds
    RECONNECT_BACKOFF_MAX = 60.0  # seconds
    RECONNECT_BACKOFF_MULTIPLIER = 2.0
    
    def __init__(
        self,
        host: str,
        port: int,
        device_uuid: str,
        device_id: str,
        keepalive: int = 60,
        username: Optional[str] = None,
        password: Optional[str] = None,
        command_handler: Optional[Callable[[dict, str], dict]] = None,
    ):
        self.host = host
        self.port = port
        self.device_uuid = device_uuid
        self.device_id = device_id
        self.keepalive = keepalive
        self.username = username
        self.password = password
        
        self._command_handler = command_handler
        self._connected = False
        self._client: Optional[mqtt.Client] = None
        
        # Reconnect state
        self._reconnect_delay = self.RECONNECT_BACKOFF_BASE
        self._reconnect_attempts = 0
        self._manual_disconnect = False  # Track intentional disconnects
        
        from threading import Lock
        self._lock = Lock()
        
        # Generate unique client_id to avoid session conflicts (rc=7)
        # Include device_id, device_uuid, and random suffix for uniqueness
        random_suffix = uuid.uuid4().hex[:8]
        self._client_id = f"qbox-stream-{self.device_id}-{self.device_uuid}-{random_suffix}"
        logger.info("Generated unique MQTT client_id: %s", self._client_id)
    
    def connect(self) -> None:
        """Start MQTT connection with unique client_id and exponential backoff"""
        self._manual_disconnect = False
        self._reconnect_delay = self.RECONNECT_BACKOFF_BASE
        self._reconnect_attempts = 0
        
        self._client = mqtt.Client(client_id=self._client_id, clean_session=True)
        
        if self.username and self.password:
            self._client.username_pw_set(self.username, self.password)
        
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        
        logger.info("Connecting MQTT to %s:%d as %s", self.host, self.port, self._client_id)
        self._client.connect_async(self.host, self.port, self.keepalive)
        self._client.loop_start()
    
    def _reconnect_with_backoff(self) -> None:
        """Attempt to reconnect with exponential backoff"""
        if self._manual_disconnect:
            logger.debug("Skipping reconnect: manual disconnect requested")
            return
        
        self._reconnect_attempts += 1
        delay = min(self._reconnect_delay, self.RECONNECT_BACKOFF_MAX)
        
        logger.info(
            "MQTT reconnect attempt %d in %.1f seconds (delay base: %.1f)",
            self._reconnect_attempts, delay, self._reconnect_delay
        )
        
        import time
        time.sleep(delay)
        
        # Increase delay for next time (exponential backoff)
        self._reconnect_delay *= self.RECONNECT_BACKOFF_MULTIPLIER
        
        try:
            if self._client:
                self._client.reconnect()
        except Exception as e:
            logger.error("MQTT reconnect failed: %s", e)
            # Schedule another reconnect attempt
            import threading
            timer = threading.Timer(self._reconnect_delay, self._reconnect_with_backoff)
            timer.daemon = True
            timer.start()
    
    def disconnect(self) -> None:
        """Stop MQTT connection"""
        self._manual_disconnect = True
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
    
    def is_connected(self) -> bool:
        return self._connected
    
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            with self._lock:
                self._connected = True
                self._reconnect_delay = self.RECONNECT_BACKOFF_BASE
                self._reconnect_attempts = 0
            logger.info("MQTT connected successfully")
            # Subscribe to stream command topic
            topic = f"devices/{self.device_uuid}/services/stream/request"
            client.subscribe(topic, qos=1)
            logger.info("Subscribed to %s", topic)
        else:
            # Log full error reason for rc=7 and other codes
            error_msg = self._mqtt_error_message(rc)
            logger.error(
                "MQTT connection failed with rc=%d: %s",
                rc, error_msg
            )
            with self._lock:
                self._connected = False
    
    def _on_disconnect(self, client, userdata, rc):
        with self._lock:
            self._connected = False
        
        # Log full error reason including rc=7 details
        error_msg = self._mqtt_error_message(rc)
        
        if rc != 0:
            logger.warning(
                "MQTT unexpected disconnect, rc=%d: %s",
                rc, error_msg
            )
            # Trigger reconnection with exponential backoff
            if not self._manual_disconnect:
                import threading
                timer = threading.Timer(
                    self.RECONNECT_BACKOFF_BASE,
                    self._reconnect_with_backoff
                )
                timer.daemon = True
                timer.start()
        else:
            logger.info("MQTT disconnected cleanly")
    
    @staticmethod
    def _mqtt_error_message(rc: int) -> str:
        """Get human-readable error message for MQTT return codes"""
        errors = {
            0: "Connection accepted",
            1: "Connection refused - unacceptable protocol version",
            2: "Connection refused - invalid client identifier",
            3: "Connection refused - server unavailable",
            4: "Connection refused - bad username or password",
            5: "Connection refused - not authorized",
            6: "Reserved for future use",
            7: "Connection refused - session conflict (client_id in use) or not authorized",
        }
        return errors.get(rc, f"Unknown error code {rc}")
    
    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            logger.debug("Received MQTT message on %s: %s", msg.topic, payload)
            
            command_id = payload.get("command_id")
            if not command_id:
                logger.warning("Ignoring message without command_id")
                return
            
            if self._command_handler:
                response = self._command_handler(payload, msg.topic)
                if response is not None:
                    self._send_response(command_id, response)
            else:
                logger.warning("No command handler registered")
        
        except json.JSONDecodeError:
            logger.exception("Failed to decode MQTT payload")
        except Exception:
            logger.exception("Error processing MQTT message")
    
    def _send_response(self, command_id: str, result: dict) -> None:
        """Send command response to MQTT"""
        if not self._client or not self._connected:
            logger.warning("Cannot send response: MQTT not connected")
            return
        
        response_topic = f"devices/{self.device_uuid}/services/stream/response"
        response_payload = {
            "command_id": command_id,
            "service": "stream",
            "result": result,
        }
        
        try:
            self._client.publish(response_topic, json.dumps(response_payload), qos=1)
            logger.debug("Sent response to %s", response_topic)
        except Exception:
            logger.exception("Failed to send MQTT response")
    
    def publish_status_event(self, status: dict) -> None:
        """Publish streaming status event"""
        if not self._client or not self._connected:
            return
        
        topic = f"devices/{self.device_uuid}/events/stream"
        payload = {
            "device_id": self.device_id,
            "timestamp": self._utc_iso(),
            **status,
        }
        
        try:
            self._client.publish(topic, json.dumps(payload), qos=1)
        except Exception:
            logger.exception("Failed to publish status event")
    
    @staticmethod
    def _utc_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
