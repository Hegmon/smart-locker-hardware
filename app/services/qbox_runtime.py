from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class QBoxRuntimeState:
    alarm_active: bool = False
    last_mqtt_reconnect: str = ""
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def set_alarm_active(self, active: bool) -> None:
        with self._lock:
            self.alarm_active = bool(active)

    def set_last_mqtt_reconnect(self, timestamp: str) -> None:
        with self._lock:
            self.last_mqtt_reconnect = str(timestamp or "").strip()

    def snapshot(self) -> dict[str, str | bool]:
        with self._lock:
            return {
                "alarm_active": self.alarm_active,
                "last_mqtt_reconnect": self.last_mqtt_reconnect,
            }


_runtime_state = QBoxRuntimeState()


def get_qbox_runtime_state() -> QBoxRuntimeState:
    return _runtime_state
