from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Callable


EventHandler = Callable[[dict[str, Any]], None]


class EventBus:
    def __init__(self):
        self._lock = threading.RLock()
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        with self._lock:
            self._subscribers[event_name].append(handler)

    def publish(self, event_name: str, payload: dict[str, Any]) -> None:
        with self._lock:
            handlers = list(self._subscribers.get(event_name, ()))
        for handler in handlers:
            handler(payload)
