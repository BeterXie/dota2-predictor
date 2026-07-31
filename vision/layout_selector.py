"""Fail-closed selection of supported Dota broadcast HUD profiles."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .layouts import (
    BroadcastLayout,
    EPL_MASTERS_LIVE,
    EPL_S39_LIVE,
    NormalizedRegion,
    STANDARD_DOTA_HUD,
)


_EPL_LEFT_DIVIDER = NormalizedRegion(0.235, 0.000, 0.250, 0.065)
_EPL_RIGHT_DIVIDER = NormalizedRegion(0.750, 0.000, 0.765, 0.065)
_EPL_LOWER_ACCENT = NormalizedRegion(0.800, 0.860, 0.840, 1.000)
_EPL_LOWER_PLATE = NormalizedRegion(0.680, 0.850, 0.860, 1.000)
_EPL_SELECTION_THRESHOLD = 0.90
_EPL_S39_SELECTION_THRESHOLD = 0.90
_STANDARD_SELECTION_THRESHOLD = 0.90
_EPL_S39_BRAND_PLATE = NormalizedRegion(0.735, 0.940, 0.860, 1.000)


@dataclass(frozen=True)
class LayoutSelection:
    layout: BroadcastLayout | None
    confidence: float
    supported: bool
    reason: str | None = None

    @property
    def layout_name(self) -> str | None:
        return self.layout.name if self.layout is not None else None


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


def _bright_ratio(image: np.ndarray, region: NormalizedRegion) -> float:
    gray = cv2.cvtColor(region.crop(image), cv2.COLOR_BGR2GRAY)
    return float((gray > 150).mean())


def _detail(image: np.ndarray, regions: tuple[NormalizedRegion, ...]) -> float:
    if not regions:
        return 0.0
    values = [
        float(cv2.cvtColor(region.crop(image), cv2.COLOR_BGR2GRAY).std())
        for region in regions
    ]
    return float(np.median(values))


def standard_dota_hud_layout_confidence(image: np.ndarray) -> float:
    """Score standard HUD geometry without treating every non-EPL frame as standard."""
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.size == 0:
        return 0.0
    logo_regions = tuple(
        region
        for region in (
            STANDARD_DOTA_HUD.radiant_team_logo,
            STANDARD_DOTA_HUD.dire_team_logo,
        )
        if region is not None
    )
    signals = (
        min(
            1.0,
            _dark_ratio(image, NormalizedRegion(0.22, 0.0, 0.78, 0.060)) / 0.45,
        ),
        min(1.0, _bright_ratio(image, STANDARD_DOTA_HUD.clock) / 0.035),
        min(1.0, _bright_ratio(image, STANDARD_DOTA_HUD.radiant_kills) / 0.06),
        min(1.0, _bright_ratio(image, STANDARD_DOTA_HUD.dire_kills) / 0.06),
        min(
            1.0,
            _detail(
                image,
                STANDARD_DOTA_HUD.radiant_heroes + STANDARD_DOTA_HUD.dire_heroes,
            )
            / 18.0,
        ),
        min(1.0, _detail(image, logo_regions) / 12.0),
    )
    return min(signals)


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


def epl_s39_layout_confidence(image: np.ndarray) -> float:
    """Score the EPL S39 live overlay from its HUD and tournament plate geometry."""
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.size == 0:
        return 0.0
    logo_regions = tuple(
        region
        for region in (
            EPL_S39_LIVE.radiant_team_logo,
            EPL_S39_LIVE.dire_team_logo,
        )
        if region is not None
    )
    signals = (
        min(1.0, _bright_ratio(image, EPL_S39_LIVE.clock) / 0.035),
        min(1.0, _bright_ratio(image, EPL_S39_LIVE.radiant_kills) / 0.045),
        min(1.0, _bright_ratio(image, EPL_S39_LIVE.dire_kills) / 0.025),
        min(
            1.0,
            _detail(
                image,
                EPL_S39_LIVE.radiant_heroes + EPL_S39_LIVE.dire_heroes,
            )
            / 18.0,
        ),
        min(1.0, _detail(image, logo_regions) / 12.0),
        min(1.0, _dark_ratio(image, _EPL_S39_BRAND_PLATE) / 0.50),
        min(1.0, _cyan_ratio(image, _EPL_S39_BRAND_PLATE) / 0.06),
    )
    return min(signals)


def layout_match_confidence(image: np.ndarray, layout: BroadcastLayout) -> float:
    if layout.name == EPL_MASTERS_LIVE.name:
        return epl_masters_layout_confidence(image)
    if layout.name == STANDARD_DOTA_HUD.name:
        return standard_dota_hud_layout_confidence(image)
    if layout.name == EPL_S39_LIVE.name:
        return epl_s39_layout_confidence(image)
    return 0.0


def select_broadcast_layout(image: np.ndarray) -> LayoutSelection:
    epl_confidence = epl_masters_layout_confidence(image)
    if epl_confidence >= _EPL_SELECTION_THRESHOLD:
        return LayoutSelection(EPL_MASTERS_LIVE, epl_confidence, True)
    epl_s39_confidence = epl_s39_layout_confidence(image)
    if epl_s39_confidence >= _EPL_S39_SELECTION_THRESHOLD:
        return LayoutSelection(EPL_S39_LIVE, epl_s39_confidence, True)
    standard_confidence = standard_dota_hud_layout_confidence(image)
    if standard_confidence >= _STANDARD_SELECTION_THRESHOLD:
        return LayoutSelection(STANDARD_DOTA_HUD, standard_confidence, True)
    return LayoutSelection(
        None,
        max(epl_confidence, epl_s39_confidence, standard_confidence),
        False,
        "unsupported_layout",
    )
