from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Callable

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)
EventHandler = Callable[[Any], None]


class EventBus:
    """Small synchronous event bus with safe fan-out logging."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        with self._lock:
            self._subscribers[event_name].append(handler)

    def publish(self, event_name: str, payload: Any) -> None:
        with self._lock:
            handlers = list(self._subscribers.get(event_name, ()))
            wildcard_handlers = list(self._subscribers.get("*", ()))
        for handler in [*handlers, *wildcard_handlers]:
            try:
                handler(payload)
            except Exception:
                logger.exception("Event handler failed for event=%s handler=%s", event_name, getattr(handler, "__name__", repr(handler)))
