"""
MQTT Stability Layer

Fixes reconnect issues with exponential backoff and session persistence.

Features:
- Exponential backoff: 1s → 2s → 5s → 10s → 30s max
- Session persistence
- Prevents rc=7 reconnect loops
- Connection state management
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import uuid
from typing import Any, Callable, Optional
from enum import Enum

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class MQTTState(Enum):
    """MQTT connection states."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    BACKOFF = "backoff"
    FAILED = "failed"


class MQTTStabilityLayer:
    """
    Stable MQTT client with exponential backoff reconnection.
    
    Solves:
    - Infinite reconnect loops (rc=7)
    - Reconnect storms
    - Session conflicts
    - Authentication issues
    
    Backoff strategy:
    1s → 2s → 5s → 10s → 30s (max)
    """
    
    # Exponential backoff configuration
    BACKOFF_DELAYS = [1, 2, 5, 10, 30]  # seconds
    
    # Connection settings
    DEFAULT_KEEPALIVE = 60
    
    def __init__(
        self,
        host: str,
        port: int,
        device_id: str,
        device_uuid: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        keepalive: int = DEFAULT_KEEPALIVE,
        client_id: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.device_id = device_id
        self.device_uuid = device_uuid
        self.username = username
        self.password = password
        self.keepalive = keepalive
        
        # Generate unique client ID to prevent session conflicts
        if client_id is None:
            random_suffix = uuid.uuid4().hex[:8]
            self.client_id = f"cctv-{device_id}-{device_uuid[:8]}-{random_suffix}"
        else:
            self.client_id = client_id
        
        logger.info(
            "MQTT client ID: %s (device: %s, uuid: %s)",
            self.client_id, device_id, device_uuid,
        )
        
        # MQTT client
        self._client: Optional[mqtt.Client] = None
        
        # Connection state
        self._state = MQTTState.DISCONNECTED
        self._state_lock = threading.RLock()
        
        # Reconnection management
        self._reconnect_delay_idx = 0
        self._is_reconnecting = False
        self._manual_disconnect = False
        self._reconnect_lock = threading.Lock()
        
        # Connection tracking
        self._connect_start_time: Optional[float] = None
        self._total_reconnects = 0
        
        # Callbacks
        self._on_connect_callback: Optional[Callable[[], None]] = None
        self._on_disconnect_callback: Optional[Callable[[], None]] = None
        self._on_message_callback: Optional[Callable[[str, dict], None]] = None
        self._on_state_change_callback: Optional[Callable[[MQTTState, MQTTState], None]] = None
        
        # Message handlers
        self._topic_handlers: dict[str, Callable[[dict], None]] = {}
        
        logger.info("MQTT Stability Layer initialized")
    
    def connect(self) -> bool:
        """
        Connect to MQTT broker with stability features.
        
        Returns:
            True if connection initiated successfully
        """
        with self._state_lock:
            if self._state in (MQTTState.CONNECTING, MQTTState.CONNECTED):
                logger.debug("MQTT already connecting/connected")
                return True
            
            self._set_state(MQTTState.CONNECTING)
            self._manual_disconnect = False
        
        try:
            # Create MQTT client
            self._client = mqtt.Client(
                client_id=self.client_id,
                clean_session=False,  # Persist session
            )
            
            # Set credentials if provided
            if self.username and self.password:
                self._client.username_pw_set(self.username, self.password)
            
            # Set callbacks
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message
            
            # Set keepalive
            self._client.keepalive_set(self.keepalive)
            
            # Enable automatic reconnection (will be managed by our backoff)
            self._client.reconnect_delay_set(
                delay=1,
                delay_max=30,
                exponential_backoff=True,
            )
            
            # Connect asynchronously
            logger.info(
                "Connecting to MQTT broker %s:%d as %s",
                self.host, self.port, self.client_id,
            )
            
            self._connect_start_time = time.time()
            self._client.connect_async(self.host, self.port, self.keepalive)
            
            # Start network loop
            self._client.loop_start()
            
            return True
            
        except Exception as e:
            logger.exception("Failed to initiate MQTT connection: %s", e)
            self._set_state(MQTTState.FAILED)
            return False
    
    def disconnect(self) -> None:
        """
        Disconnect from MQTT broker.
        Prevents automatic reconnection.
        """
        with self._state_lock:
            self._manual_disconnect = True
            self._is_reconnecting = False
        
        logger.info("Disconnecting from MQTT broker")
        
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as e:
                logger.debug("Error during MQTT disconnect: %s", e)
        
        self._set_state(MQTTState.DISCONNECTED)
        logger.info("Disconnected from MQTT broker")
    
    def is_connected(self) -> bool:
        """Check if currently connected."""
        with self._state_lock:
            return self._state == MQTTState.CONNECTED
    
    def get_state(self) -> MQTTState:
        """Get current connection state."""
        with self._state_lock:
            return self._state
    
    def publish(
        self,
        topic: str,
        payload: dict,
        qos: int = 1,
        retain: bool = False,
    ) -> bool:
        """
        Publish message to MQTT topic.
        
        Args:
            topic: MQTT topic
            payload: Message payload (will be JSON serialized)
            qos: Quality of service (0, 1, or 2)
            retain: Whether to retain message
        
        Returns:
            True if published successfully
        """
        if not self.is_connected():
            logger.warning("Cannot publish: MQTT not connected")
            return False
        
        try:
            result = self._client.publish(topic, json.dumps(payload), qos, retain)
            
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.debug("Published to %s: %s", topic, payload)
                return True
            else:
                logger.warning(
                    "Failed to publish to %s: %s",
                    topic, mqtt.error_string(result.rc),
                )
                return False
                
        except Exception as e:
            logger.exception("Error publishing to %s: %s", topic, e)
            return False
    
    def subscribe(
        self,
        topic: str,
        handler: Callable[[dict], None],
        qos: int = 1,
    ) -> bool:
        """
        Subscribe to MQTT topic.
        
        Args:
            topic: MQTT topic pattern
            handler: Callback function that receives parsed payload
            qos: Quality of service
        
        Returns:
            True if subscribed successfully
        """
        if not self.is_connected():
            logger.warning("Cannot subscribe: MQTT not connected")
            return False
        
        try:
            self._topic_handlers[topic] = handler
            result = self._client.subscribe(topic, qos)
            
            if result[0] == mqtt.MQTT_ERR_SUCCESS:
                logger.info("Subscribed to %s (QoS %d)", topic, qos)
                return True
            else:
                logger.warning(
                    "Failed to subscribe to %s: %s",
                    topic, mqtt.error_string(result[0]),
                )
                return False
                
        except Exception as e:
            logger.exception("Error subscribing to %s: %s", topic, e)
            return False
    
    def set_callbacks(
        self,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        on_message: Optional[Callable[[str, dict], None]] = None,
        on_state_change: Optional[Callable[[MQTTState, MQTTState], None]] = None,
    ) -> None:
        """
        Set callback functions.
        
        Args:
            on_connect: Called when connected
            on_disconnect: Called when disconnected
            on_message: Called when message received (topic, payload)
            on_state_change: Called when state changes (old, new)
        """
        self._on_connect_callback = on_connect
        self._on_disconnect_callback = on_disconnect
        self._on_message_callback = on_message
        self._on_state_change_callback = on_state_change
    
    def _set_state(self, new_state: MQTTState) -> None:
        """
        Update connection state and notify callbacks.
        
        Args:
            new_state: New state
        """
        with self._state_lock:
            old_state = self._state
            
            if old_state != new_state:
                self._state = new_state
                logger.info(
                    "MQTT state changed: %s → %s",
                    old_state.value, new_state.value,
                )
                
                if self._on_state_change_callback:
                    try:
                        self._on_state_change_callback(old_state, new_state)
                    except Exception as e:
                        logger.exception("State change callback failed: %s", e)
    
    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict,
        rc: int,
    ) -> None:
        """Handle MQTT connection result."""
        if rc == 0:
            logger.info(
                "MQTT connected successfully (after %.1fs)",
                time.time() - (self._connect_start_time or time.time()),
            )
            
            with self._state_lock:
                self._state = MQTTState.CONNECTED
                self._reconnect_delay_idx = 0  # Reset backoff
                self._is_reconnecting = False
                self._total_reconnects += 1
            
            # Resubscribe to all topics
            for topic in self._topic_handlers:
                try:
                    client.subscribe(topic, qos=1)
                    logger.debug("Resubscribed to %s", topic)
                except Exception as e:
                    logger.warning("Failed to resubscribe to %s: %s", topic, e)
            
            if self._on_connect_callback:
                try:
                    self._on_connect_callback()
                except Exception as e:
                    logger.exception("Connect callback failed: %s", e)
        
        else:
            error_msg = self._get_error_message(rc)
            logger.error(
                "MQTT connection failed (rc=%d): %s",
                rc, error_msg,
            )
            
            with self._state_lock:
                self._state = MQTTState.FAILED
            
            # Handle rc=7 (session conflict) specially
            if rc == 7:
                logger.warning(
                    "MQTT rc=7 (session conflict). "
                    "Client ID: %s. "
                    "Consider using unique client IDs.",
                    self.client_id,
                )
            
            # Schedule reconnection with backoff
            self._schedule_reconnect()
    
    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        rc: int,
    ) -> None:
        """Handle MQTT disconnection."""
        with self._state_lock:
            was_connected = self._state == MQTTState.CONNECTED
            self._state = MQTTState.DISCONNECTED
        
        if rc == 0:
            logger.info("MQTT disconnected cleanly")
        else:
            error_msg = self._get_error_message(rc)
            logger.warning(
                "MQTT unexpected disconnect (rc=%d): %s",
                rc, error_msg,
            )
            
            # Don't reconnect if manual disconnect
            if self._manual_disconnect:
                logger.debug("Skipping reconnect: manual disconnect requested")
                return
            
            # Schedule reconnection with backoff
            if was_connected:
                self._schedule_reconnect()
        
        if self._on_disconnect_callback:
            try:
                self._on_disconnect_callback()
            except Exception as e:
                logger.exception("Disconnect callback failed: %s", e)
    
    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        """Handle incoming MQTT message."""
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode())
            
            logger.debug("Received MQTT message on %s: %s", topic, payload)
            
            # Call topic-specific handler
            for pattern, handler in self._topic_handlers.items():
                if self._topic_matches(topic, pattern):
                    try:
                        handler(payload)
                    except Exception as e:
                        logger.exception(
                            "Handler for %s failed: %s",
                            pattern, e,
                        )
                    break
            
            # Call general message callback
            if self._on_message_callback:
                self._on_message_callback(topic, payload)
                
        except json.JSONDecodeError:
            logger.exception("Failed to decode MQTT payload")
        except Exception as e:
            logger.exception("Error processing MQTT message: %s", e)
    
    def _schedule_reconnect(self) -> None:
        """
        Schedule a reconnection attempt with exponential backoff.
        """
        with self._reconnect_lock:
            if self._is_reconnecting or self._manual_disconnect:
                return
            
            self._is_reconnecting = True
        
        # Calculate delay
        delay_idx = min(self._reconnect_delay_idx, len(self.BACKOFF_DELAYS) - 1)
        delay = self.BACKOFF_DELAYS[delay_idx]
        
        logger.info(
            "MQTT reconnect attempt %d in %d seconds",
            self._reconnect_delay_idx + 1, delay,
        )
        
        # Schedule reconnect in background
        def do_reconnect():
            time.sleep(delay)
            
            with self._reconnect_lock:
                self._is_reconnecting = False
                self._reconnect_delay_idx = min(
                    self._reconnect_delay_idx + 1,
                    len(self.BACKOFF_DELAYS) - 1,
                )
            
            # Attempt reconnect if not manually disconnected
            with self._state_lock:
                if not self._manual_disconnect and self._state != MQTTState.CONNECTED:
                    logger.info("Attempting MQTT reconnect...")
                    self._set_state(MQTTState.RECONNECTING)
                    
                    try:
                        if self._client:
                            self._client.reconnect()
                    except Exception as e:
                        logger.warning("MQTT reconnect failed: %s", e)
                        self._set_state(MQTTState.FAILED)
                        # Schedule another attempt
                        self._schedule_reconnect()
        
        thread = threading.Thread(target=do_reconnect, daemon=True)
        thread.start()
    
    @staticmethod
    def _topic_matches(topic: str, pattern: str) -> bool:
        """
        Check if topic matches pattern (simple wildcard support).
        
        Args:
            topic: Actual topic
            pattern: Pattern with optional wildcards
        
        Returns:
            True if topic matches pattern
        """
        # Simple implementation - exact match for now
        return topic == pattern
    
    @staticmethod
    def _get_error_message(rc: int) -> str:
        """
        Get human-readable error message for MQTT return code.
        
        Args:
            rc: MQTT return code
        
        Returns:
            Error message string
        """
        errors = {
            0: "Connection accepted",
            1: "Connection refused - unacceptable protocol version",
            2: "Connection refused - invalid client identifier",
            3: "Connection refused - server unavailable",
            4: "Connection refused - bad username or password",
            5: "Connection refused - not authorized",
            6: "Reserved for future use",
            7: "Connection refused - session conflict or not authorized",
        }
        return errors.get(rc, f"Unknown error code {rc}")
    
    def get_stats(self) -> dict:
        """
        Get connection statistics.
        
        Returns:
            Dictionary with connection stats
        """
        with self._state_lock:
            return {
                "state": self._state.value,
                "client_id": self.client_id,
                "device_id": self.device_id,
                "total_reconnects": self._total_reconnects,
                "current_backoff": (
                    self.BACKOFF_DELAYS[
                        min(self._reconnect_delay_idx, len(self.BACKOFF_DELAYS) - 1)
                    ]
                    if self._reconnect_delay_idx > 0
                    else 0
                ),
                "is_reconnecting": self._is_reconnecting,
                "manual_disconnect": self._manual_disconnect,
            }
    
    def reset_backoff(self) -> None:
        """Reset exponential backoff to initial value."""
        with self._reconnect_lock:
            self._reconnect_delay_idx = 0
        logger.debug("MQTT backoff reset")
    
    def cleanup(self) -> None:
        """Clean up all resources."""
        logger.info("Cleaning up MQTT stability layer...")
        self.disconnect()
        logger.info("MQTT stability layer cleaned up")
