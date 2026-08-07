"""Cheap frame-quality signals used by the stable vision runtime."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class FrameQuality:
    usable: bool
    visible_ratio: float
    sharpness: float
    frozen: bool
    fingerprint: str
    reason: str | None = None


class FrameQualityTracker:
    def __init__(
        self,
        *,
        minimum_visible_ratio: float = 0.08,
        minimum_sharpness: float = 8.0,
        freeze_confirmations: int = 3,
    ) -> None:
        if not 0.0 <= minimum_visible_ratio <= 1.0:
            raise ValueError("minimum_visible_ratio must be between zero and one")
        if minimum_sharpness < 0.0:
            raise ValueError("minimum_sharpness must not be negative")
        if freeze_confirmations < 2:
            raise ValueError("freeze_confirmations must be at least two")
        self.minimum_visible_ratio = minimum_visible_ratio
        self.minimum_sharpness = minimum_sharpness
        self.freeze_confirmations = freeze_confirmations
        self._last_fingerprint: str | None = None
        self._same_fingerprint_count = 0

    @staticmethod
    def fingerprint(image: np.ndarray) -> str:
        if not isinstance(image, np.ndarray) or image.ndim not in {2, 3} or image.size == 0:
            return "invalid"
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        thumb = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
        return hashlib.blake2b(thumb.tobytes(), digest_size=8).hexdigest()

    def assess(self, image: np.ndarray) -> FrameQuality:
        if not isinstance(image, np.ndarray) or image.ndim != 3 or image.size == 0:
            return FrameQuality(False, 0.0, 0.0, False, "invalid", "invalid_frame")

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        visible_ratio = float((gray > 20).mean())
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        fingerprint = self.fingerprint(image)

        if fingerprint == self._last_fingerprint:
            self._same_fingerprint_count += 1
        else:
            self._last_fingerprint = fingerprint
            self._same_fingerprint_count = 1
        frozen = self._same_fingerprint_count >= self.freeze_confirmations

        if visible_ratio < self.minimum_visible_ratio:
            return FrameQuality(
                False, visible_ratio, sharpness, frozen, fingerprint, "mostly_black"
            )
        if sharpness < self.minimum_sharpness:
            return FrameQuality(
                False, visible_ratio, sharpness, frozen, fingerprint, "low_detail"
            )
        if frozen:
            return FrameQuality(
                False, visible_ratio, sharpness, True, fingerprint, "frozen_frame"
            )
        return FrameQuality(True, visible_ratio, sharpness, False, fingerprint)
