from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - Raspberry Pi runtime dependency
    cv2 = None
    np = None

from app.streaming_agent.detection.scanner_config import QRScannerConfig


@dataclass(frozen=True)
class PreprocessedFrame:
    name: str
    image: object
    scale: float


@dataclass(frozen=True)
class FrameQualityMetrics:
    brightness: float
    contrast: float
    blur: float

    @classmethod
    def empty(cls) -> "FrameQualityMetrics":
        return cls(brightness=0.0, contrast=0.0, blur=0.0)

    def as_dict(self) -> dict:
        return {
            "brightness": self.brightness,
            "contrast": self.contrast,
            "blur": self.blur,
        }


class QRPreprocessor:
    """CPU-bounded preprocessing tuned for phone-screen QR codes in bright sun."""

    def __init__(self, config: QRScannerConfig):
        self.config = config
        self._clahe = None
        if cv2 is not None:
            self._clahe = cv2.createCLAHE(
                clipLimit=config.clahe_clip_limit,
                tileGridSize=config.clahe_tile_grid_size,
            )

    def candidates(self, frame) -> Iterable[PreprocessedFrame]:
        if cv2 is None or np is None:
            return

        small, scale = self._resize_for_detection(frame)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if len(small.shape) == 3 else small

        yield PreprocessedFrame("gray", gray, scale)
        if not self.config.preprocessing_enabled:
            return

        clahe = self._clahe.apply(gray) if self._clahe is not None else gray
        yield PreprocessedFrame("clahe", clahe, scale)

        adaptive = cv2.adaptiveThreshold(
            clahe,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            self.config.adaptive_block_size,
            self.config.adaptive_c,
        )
        yield PreprocessedFrame("adaptive_threshold", adaptive, scale)

        if self.config.sharpening_enabled:
            blurred = cv2.GaussianBlur(clahe, (0, 0), 1.0)
            sharpened = cv2.addWeighted(clahe, 1.7, blurred, -0.7, 0)
            yield PreprocessedFrame("sharpened", sharpened, scale)

    def quality_metrics(self, frame) -> FrameQualityMetrics:
        if cv2 is None or np is None:
            return FrameQualityMetrics.empty()
        try:
            small, _ = self._resize_for_detection(frame, target_width=min(self.config.detection_width, 320))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if len(small.shape) == 3 else small
            return FrameQualityMetrics(
                brightness=float(np.mean(gray)),
                contrast=float(np.std(gray)),
                blur=float(cv2.Laplacian(gray, cv2.CV_64F).var()),
            )
        except Exception:
            return FrameQualityMetrics.empty()

    def _resize_for_detection(self, frame, target_width: int | None = None) -> Tuple[object, float]:
        target = target_width or self.config.detection_width
        width = frame.shape[1]
        if width <= target:
            return frame, 1.0
        scale = target / float(width)
        return cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA), scale
