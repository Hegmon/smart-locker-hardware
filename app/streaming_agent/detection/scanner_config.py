from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _env_resolution(name: str, default: Tuple[int, int]) -> Tuple[int, int]:
    raw = os.getenv(name, f"{default[0]}x{default[1]}").strip().lower()
    separator = "x" if "x" in raw else ","
    try:
        width_text, height_text = raw.split(separator, 1)
        return max(160, int(width_text)), max(120, int(height_text))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class QRScannerConfig:
    """Runtime settings for the external QR scanner service."""

    scan_interval_ms: int = 250
    cooldown_seconds: float = 5.0
    camera_resolution: Tuple[int, int] = (960, 540)
    autofocus_enabled: bool = True
    preprocessing_enabled: bool = True
    detection_width: int = 480
    adaptive_block_size: int = 31
    adaptive_c: int = 4
    clahe_clip_limit: float = 2.5
    clahe_tile_grid_size: Tuple[int, int] = (8, 8)
    sharpening_enabled: bool = True
    qr_detector_eps_x: float = 0.35
    qr_detector_eps_y: float = 0.35
    scan_timeout_seconds: float = 30.0
    camera_watchdog_seconds: float = 8.0
    camera_reconnect_backoff_seconds: float = 2.0
    duplicate_cache_seconds: float = 180.0
    debug_preview_enabled: bool = False
    debug_frame_dir: Path = Path("logs/qr_debug_frames")
    debug_save_interval_seconds: float = 5.0
    metrics_log_interval_seconds: float = 10.0
    backend_verify_url: str = "https://backend.qbox.sa/shipments/qr/verify/"
    backend_timeout_seconds: float = 10.0
    success_gpio_pin: int = 15
    failure_gpio_pin: int = 14
    default_unlock_seconds: int = 5
    failure_signal_seconds: float = 2.0
    attention_hold_seconds: float = 2.5
    require_jwt_shape: bool = False

    @property
    def scan_interval_seconds(self) -> float:
        return self.scan_interval_ms / 1000.0

    @classmethod
    def from_env(cls) -> "QRScannerConfig":
        return cls(
            scan_interval_ms=_env_int("QR_SCAN_INTERVAL_MS", 250, minimum=100),
            cooldown_seconds=_env_float(
                "QR_COOLDOWN_SECONDS",
                _env_float("QR_DEBOUNCE_SECONDS", 5.0, minimum=0.0),
                minimum=0.0,
            ),
            camera_resolution=_env_resolution(
                "QR_CAMERA_RESOLUTION",
                (
                    _env_int("QR_FRAME_WIDTH", 960, minimum=160),
                    _env_int("QR_FRAME_HEIGHT", 540, minimum=120),
                ),
            ),
            autofocus_enabled=_env_bool("QR_AUTOFOCUS_ENABLED", True),
            preprocessing_enabled=_env_bool("QR_PREPROCESSING_ENABLED", True),
            detection_width=_env_int("QR_DETECTION_WIDTH", _env_int("QR_DETECT_WIDTH", 480), minimum=240),
            adaptive_block_size=_make_odd(_env_int("QR_ADAPTIVE_BLOCK_SIZE", 31, minimum=3)),
            adaptive_c=_env_int("QR_ADAPTIVE_C", 4),
            clahe_clip_limit=_env_float("QR_CLAHE_CLIP_LIMIT", 2.5, minimum=0.1),
            sharpening_enabled=_env_bool("QR_SHARPENING_ENABLED", _env_bool("QR_SHARPEN_ENABLED", True)),
            qr_detector_eps_x=_env_float("QR_DETECTOR_EPS_X", 0.35, minimum=0.01),
            qr_detector_eps_y=_env_float("QR_DETECTOR_EPS_Y", 0.35, minimum=0.01),
            scan_timeout_seconds=_env_float("QR_SCAN_TIMEOUT_SECONDS", 30.0, minimum=1.0),
            camera_watchdog_seconds=_env_float("QR_CAMERA_WATCHDOG_SECONDS", 8.0, minimum=2.0),
            camera_reconnect_backoff_seconds=_env_float("QR_CAMERA_RECONNECT_BACKOFF_SECONDS", 2.0, minimum=0.5),
            duplicate_cache_seconds=_env_float("QR_DUPLICATE_CACHE_SECONDS", 180.0, minimum=1.0),
            debug_preview_enabled=_env_bool("QR_DEBUG_PREVIEW", _env_bool("QR_SCAN_DEBUG", False)),
            debug_frame_dir=Path(os.getenv("QR_DEBUG_FRAME_DIR", "logs/qr_debug_frames")),
            debug_save_interval_seconds=_env_float("QR_DEBUG_SAVE_INTERVAL_SECONDS", 5.0, minimum=0.5),
            metrics_log_interval_seconds=_env_float("QR_METRICS_LOG_INTERVAL_SECONDS", 10.0, minimum=1.0),
            backend_verify_url=os.getenv("BACKEND_QR_VERIFY_URL", cls.backend_verify_url),
            backend_timeout_seconds=_env_float("QR_VERIFY_TIMEOUT_SECONDS", 10.0, minimum=0.5),
            success_gpio_pin=_env_int("QR_SUCCESS_GPIO_PIN", 15),
            failure_gpio_pin=_env_int("QR_FAILURE_GPIO_PIN", 14),
            default_unlock_seconds=_env_int("QR_DEFAULT_UNLOCK_SECONDS", 5, minimum=1),
            failure_signal_seconds=_env_float("QR_FAILURE_SIGNAL_SECONDS", 2.0, minimum=0.1),
            attention_hold_seconds=_env_float("QR_ATTENTION_HOLD_SECONDS", 2.5, minimum=0.1),
            require_jwt_shape=_env_bool("QR_REQUIRE_JWT_SHAPE", False),
        )


def _make_odd(value: int) -> int:
    return value if value % 2 == 1 else value + 1
