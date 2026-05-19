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
                clipLimit=self._config_value("clahe_clip_limit", 2.5),
                tileGridSize=self._config_value("clahe_tile_grid_size", (8, 8)),
            )

    def candidates(self, frame, attempt_index: int = 0) -> Iterable[PreprocessedFrame]:
        if cv2 is None or np is None:
            return

        working = self._center_roi(frame)
        gray_source = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY) if len(working.shape) == 3 else working

        for width in self._scan_widths(gray_source):
            small, scale = self._resize_for_detection(gray_source, target_width=width)
            gray = self._safe_gray_image(self._with_quiet_zone(small))
            yield PreprocessedFrame(f"gray_{width}", gray, scale)

            if not self._config_value("preprocessing_enabled", True):
                continue

            clahe = self._apply_clahe(gray)
            yield PreprocessedFrame(f"clahe_{width}", clahe, scale)

            if not self._should_try_expensive(attempt_index):
                continue

            adaptive = self._adaptive_threshold(clahe)
            yield PreprocessedFrame(f"adaptive_threshold_{width}", adaptive, scale)

            if self._config_value("invert_candidate_enabled", True):
                yield PreprocessedFrame(f"adaptive_threshold_inverted_{width}", cv2.bitwise_not(adaptive), scale)

    def opencv_candidates(self, frame, attempt_index: int = 0) -> Iterable[PreprocessedFrame]:
        if cv2 is None or np is None:
            return

        working = self._center_roi(frame)
        small, scale = self._resize_for_detection(
            working,
            target_width=min(int(self._config_value("detection_width", 640)), 640),
        )
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if len(small.shape) == 3 else small
        gray = self._safe_gray_image(self._with_quiet_zone(gray))
        yield PreprocessedFrame("opencv_gray", gray, scale)

        if not self._config_value("preprocessing_enabled", True):
            return

        clahe = self._apply_clahe(gray)
        yield PreprocessedFrame("opencv_clahe", clahe, scale)
        if not self._should_try_expensive(attempt_index):
            return

        adaptive = self._adaptive_threshold(clahe)
        yield PreprocessedFrame("opencv_adaptive_threshold", adaptive, scale)

    def _should_try_expensive(self, attempt_index: int) -> bool:
        expensive_every_n = max(1, int(self._config_value("expensive_preprocess_every_n", 1)))
        return attempt_index % expensive_every_n == 0

    def quality_metrics(self, frame) -> FrameQualityMetrics:
        if cv2 is None or np is None:
            return FrameQualityMetrics.empty()
        try:
            small, _ = self._resize_for_detection(
                self._center_roi(frame),
                target_width=min(self._config_value("detection_width", 640), 640),
            )
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if len(small.shape) == 3 else small
            return FrameQualityMetrics(
                brightness=float(np.mean(gray)),
                contrast=float(np.std(gray)),
                blur=float(cv2.Laplacian(gray, cv2.CV_64F).var()),
            )
        except Exception:
            return FrameQualityMetrics.empty()

    def _resize_for_detection(self, frame, target_width: int | None = None) -> Tuple[object, float]:
        target = target_width or int(self._config_value("detection_width", 640))
        width = frame.shape[1]
        if width <= target:
            return frame, 1.0
        scale = target / float(width)
        return cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA), scale

    def _with_quiet_zone(self, image):
        ratio = float(self._config_value("quiet_zone_border_ratio", 0.08))
        if ratio <= 0:
            return image
        side = min(image.shape[:2])
        border = max(8, int(side * ratio))
        return cv2.copyMakeBorder(
            image,
            border,
            border,
            border,
            border,
            cv2.BORDER_CONSTANT,
            value=255,
        )

    def _apply_clahe(self, gray):
        gray = self._safe_gray_image(gray)
        if self._clahe is None:
            return gray
        try:
            return self._safe_gray_image(self._clahe.apply(gray))
        except Exception:
            return gray

    def _adaptive_threshold(self, gray):
        gray = self._safe_gray_image(gray)
        block_size = int(self._config_value("adaptive_block_size", 31))
        if block_size % 2 == 0:
            block_size += 1
        block_size = max(3, min(block_size, min(gray.shape[:2]) | 1))
        return cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            block_size,
            self._config_value("adaptive_c", 4),
        )

    def _safe_gray_image(self, image):
        if len(image.shape) == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if image.dtype != np.uint8:
            image = image.astype(np.uint8)
        return np.ascontiguousarray(image)

    def _center_roi(self, frame):
        if not self._config_value("roi_enabled", True):
            return frame

        height, width = frame.shape[:2]
        roi_width_ratio = min(float(self._config_value("roi_width_ratio", 1.0)), 1.0)
        roi_height_ratio = min(float(self._config_value("roi_height_ratio", 1.0)), 1.0)
        roi_width = min(width, max(1, int(width * roi_width_ratio)))
        roi_height = min(height, max(1, int(height * roi_height_ratio)))
        x0 = max(0, (width - roi_width) // 2)
        y0 = max(0, (height - roi_height) // 2)
        return frame[y0 : y0 + roi_height, x0 : x0 + roi_width]

    def _config_value(self, name, default):
        return getattr(self.config, name, default)

    def _scan_widths(self, image):
        widths = getattr(self.config, "pyzbar_scan_widths", (960, 640, 320))
        seen = set()
        for width in widths:
            width = min(int(width), image.shape[1])
            if width in seen:
                continue
            seen.add(width)
            yield width
