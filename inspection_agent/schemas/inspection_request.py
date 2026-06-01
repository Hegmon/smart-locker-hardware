from __future__ import annotations

"""Inspection command request schema."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InspectionRequest:
    """Validated request payload received over MQTT."""

    request_id: str
    action: str
    module: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "InspectionRequest":
        if not isinstance(payload, dict):
            raise ValueError("Inspection request payload must be a JSON object")

        request_id = str(payload.get("request_id", "")).strip()
        action = str(payload.get("action", "")).strip().lower()
        module_value = payload.get("module")
        module = str(module_value).strip().lower() if isinstance(module_value, str) and module_value.strip() else None

        if not request_id:
            raise ValueError("Inspection request is missing request_id")
        if not action:
            raise ValueError("Inspection request is missing action")

        return cls(request_id=request_id, action=action, module=module)

    def is_run_test(self) -> bool:
        return self.action == "run_test"

    def is_run_all(self) -> bool:
        return self.action == "run_all"
