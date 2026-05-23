from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CameraType(str, Enum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class DetectionType(str, Enum):
    PERSON_DETECTED = "PERSON_DETECTED"
    MOTION_DETECTED = "MOTION_DETECTED"
    TAMPER_DETECTED = "TAMPER_DETECTED"


@dataclass(frozen=True)
class DetectionEvent:
    camera_type: str
    detection_type: str
    confidence: float
    timestamp: float = field(default_factory=time.monotonic)
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def event_name(self) -> str:
        return f"detection.{self.detection_type.lower()}"
