from __future__ import annotations

from typing import Any


_streaming_agent: Any | None = None


def set_streaming_agent(agent: Any | None) -> None:
    global _streaming_agent
    _streaming_agent = agent


def get_streaming_agent() -> Any | None:
    return _streaming_agent
