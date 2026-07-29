"""Fail-closed selection of supported Dota broadcast HUD profiles."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .layouts import (
    BroadcastLayout,
    EPL_MASTERS_LIVE,
    NormalizedRegion,
    STANDARD_DOTA_HUD,
)


_EPL_LEFT_DIVIDER = NormalizedRegion(0.235, 0.000, 0.250, 0.065)
_EPL_RIGHT_DIVIDER = NormalizedRegion(0.750, 0.000, 0.765, 0.065)
_EPL_LOWER_ACCENT = NormalizedRegion(0.800, 0.860, 0.840, 1.000)
_EPL_LOWER_PLATE = NormalizedRegion(0.680, 0.850, 0.860, 1.000)
_EPL_SELECTION_THRESHOLD = 0.90


@dataclass(frozen=True)
class LayoutSelection:
    layout: BroadcastLayout
    confidence: float


def _cyan_ratio(image: np.ndarray, region: NormalizedRegion) -> float:
    hsv = cv2.cvtColor(region.crop(image), cv2.COLOR_BGR2HSV)
    cyan = (
        (hsv[:, :, 0] >= 75)
        & (hsv[:, :, 0] <= 105)
        & (hsv[:, :, 1] >= 80)
        & (hsv[:, :, 2] >= 100)
    )
    return float(cyan.mean())


def _dark_ratio(image: np.ndarray, region: NormalizedRegion) -> float:
    gray = cv2.cvtColor(region.crop(image), cv2.COLOR_BGR2GRAY)
    return float((gray < 45).mean())


def epl_masters_layout_confidence(image: np.ndarray) -> float:
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.size == 0:
        return 0.0
    signals = (
        min(1.0, _cyan_ratio(image, _EPL_LEFT_DIVIDER) / 0.18),
        min(1.0, _cyan_ratio(image, _EPL_RIGHT_DIVIDER) / 0.18),
        min(1.0, _cyan_ratio(image, _EPL_LOWER_ACCENT) / 0.08),
        min(1.0, _dark_ratio(image, _EPL_LOWER_PLATE) / 0.55),
    )
    return min(signals)


def layout_match_confidence(image: np.ndarray, layout: BroadcastLayout) -> float:
    if layout.name == EPL_MASTERS_LIVE.name:
        return epl_masters_layout_confidence(image)
    return 1.0 if layout.name == STANDARD_DOTA_HUD.name else 0.0


def select_broadcast_layout(image: np.ndarray) -> LayoutSelection:
    epl_confidence = epl_masters_layout_confidence(image)
    if epl_confidence >= _EPL_SELECTION_THRESHOLD:
        return LayoutSelection(EPL_MASTERS_LIVE, epl_confidence)
    return LayoutSelection(STANDARD_DOTA_HUD, max(0.5, 1.0 - epl_confidence))
