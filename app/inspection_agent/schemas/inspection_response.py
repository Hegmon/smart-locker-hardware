from __future__ import annotations

"""Inspection result and summary schemas."""

from dataclasses import asdict, dataclass, field
from typing import Any

from app.utils.system_info import utc_timestamp


@dataclass(frozen=True)
class InspectionResult:
    """Structured per-test MQTT response."""

    request_id: str
    device_id: str
    module: str
    status: str
    message: str
    timestamp: str = field(default_factory=utc_timestamp)
    type: str = "result"
    details: dict[str, Any] | None = None

    @classmethod
    def success(
        cls,
        *,
        request_id: str,
        device_id: str,
        module: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> "InspectionResult":
        return cls(
            request_id=request_id,
            device_id=device_id,
            module=module,
            status="PASS",
            message=message,
            details=details,
        )

    @classmethod
    def failure(
        cls,
        *,
        request_id: str,
        device_id: str,
        module: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> "InspectionResult":
        return cls(
            request_id=request_id,
            device_id=device_id,
            module=module,
            status="FAIL",
            message=message,
            details=details,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if payload.get("details") is None:
            payload.pop("details", None)
        return payload


@dataclass(frozen=True)
class InspectionSummary:
    """Summary payload published after a run-all inspection request."""

    request_id: str
    type: str = "summary"
    passed: int = 0
    failed: int = 0
    status: str = "PASS"
    timestamp: str = field(default_factory=utc_timestamp)

    @classmethod
    def from_counts(cls, *, request_id: str, passed: int, failed: int) -> "InspectionSummary":
        return cls(
            request_id=request_id,
            passed=passed,
            failed=failed,
            status="PASS" if failed == 0 else "FAIL",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
