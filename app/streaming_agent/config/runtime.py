from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    return value


@dataclass(frozen=True)
class RelayConfig:
    timeout_seconds: float
    detection_debounce_seconds: float
    retry_count: int
    retry_delay_seconds: float
    poll_interval_seconds: float
    stale_on_failsafe_seconds: float
    state_log_interval_seconds: float

    @classmethod
    def from_env(cls) -> "RelayConfig":
        timeout = _env_float(
            "SECURITY_HOLD_SECONDS",
            _env_float("DETECTION_HOLD_SECONDS", 5.0, minimum=0.0),
            minimum=0.0,
        )
        return cls(
            timeout_seconds=timeout,
            detection_debounce_seconds=_env_float("DETECTION_EVENT_DEBOUNCE_SECONDS", 1.0, minimum=0.0),
            retry_count=_env_int("SECURITY_RELAY_RETRY_COUNT", 3, minimum=1),
            retry_delay_seconds=_env_float("SECURITY_RELAY_RETRY_DELAY_SECONDS", 0.2, minimum=0.0),
            poll_interval_seconds=_env_float("SECURITY_RELAY_POLL_INTERVAL_SECONDS", 0.1, minimum=0.01),
            stale_on_failsafe_seconds=_env_float("SECURITY_RELAY_FAILSAFE_SECONDS", 10.0, minimum=1.0),
            state_log_interval_seconds=_env_float("SECURITY_RELAY_STATE_LOG_INTERVAL_SECONDS", 1.0, minimum=0.1),
        )


@dataclass(frozen=True)
class DetectionEventConfig:
    cooldown_seconds: float

    @classmethod
    def from_env(cls) -> "DetectionEventConfig":
        return cls(
            cooldown_seconds=_env_float("DETECTION_EVENT_COOLDOWN_SECONDS", 0.25, minimum=0.0),
        )


@dataclass(frozen=True)
class PersonDetectionConfig:
    confidence_threshold: float

    @classmethod
    def from_env(cls) -> "PersonDetectionConfig":
        return cls(
            confidence_threshold=_env_float(
                "PERSON_CONFIDENCE_THRESHOLD",
                _env_float("PERSON_DETECTION_CONFIDENCE", 0.6, minimum=0.05, maximum=0.95),
                minimum=0.05,
                maximum=0.95,
            ),
        )


@dataclass(frozen=True)
class TamperDetectionConfig:
    confirm_seconds: float
    clear_seconds: float
    dark_brightness_threshold: float
    bright_brightness_threshold: float
    blur_threshold: float
    edge_density_threshold: float
    large_change_threshold: float
    scene_change_enabled: bool

    @classmethod
    def from_env(cls) -> "TamperDetectionConfig":
        return cls(
            confirm_seconds=_env_float("TAMPER_CONFIRM_SECONDS", 1.5, minimum=0.0),
            clear_seconds=_env_float(
                "TAMPER_HOLD_SECONDS",
                _env_float("TAMPER_CLEAR_SECONDS", 3.0, minimum=0.0),
                minimum=0.0,
            ),
            dark_brightness_threshold=_env_float("TAMPER_DARK_BRIGHTNESS_THRESHOLD", 28.0, minimum=0.0, maximum=255.0),
            bright_brightness_threshold=_env_float("TAMPER_BRIGHT_BRIGHTNESS_THRESHOLD", 242.0, minimum=0.0, maximum=255.0),
            blur_threshold=_env_float("TAMPER_BLUR_THRESHOLD", 12.0, minimum=0.0),
            edge_density_threshold=_env_float("TAMPER_EDGE_DENSITY_THRESHOLD", 0.005, minimum=0.0, maximum=1.0),
            large_change_threshold=_env_float("TAMPER_LARGE_CHANGE_THRESHOLD", 0.58, minimum=0.0, maximum=1.0),
            scene_change_enabled=_env_bool("TAMPER_SCENE_CHANGE_ENABLED", True),
        )


@dataclass(frozen=True)
class StreamingAgentRuntimeConfig:
    relay: RelayConfig
    event: DetectionEventConfig
    person: PersonDetectionConfig
    tamper: TamperDetectionConfig

    @classmethod
    def from_env(cls) -> "StreamingAgentRuntimeConfig":
        return cls(
            relay=RelayConfig.from_env(),
            event=DetectionEventConfig.from_env(),
            person=PersonDetectionConfig.from_env(),
            tamper=TamperDetectionConfig.from_env(),
        )
