"""Coarse broadcast-state classification used before HUD extraction."""

from __future__ import annotations

import cv2
import numpy as np

from .layouts import BroadcastLayout, STANDARD_DOTA_HUD
from .stream_capture import nonblack_ratio


def classify_screen_state(
    image: np.ndarray, layout: BroadcastLayout = STANDARD_DOTA_HUD
) -> tuple[str, float]:
    visible = nonblack_ratio(image)
    if visible < 0.08:
        return "transition", min(1.0, (0.08 - visible) / 0.08 + 0.5)

    banner = layout.draft_banner.crop(image)
    hsv = cv2.cvtColor(banner, cv2.COLOR_BGR2HSV)
    cyan = (
        (hsv[:, :, 0] >= 75)
        & (hsv[:, :, 0] <= 105)
        & (hsv[:, :, 1] >= 80)
        & (hsv[:, :, 2] >= 100)
    )
    cyan_ratio = float(cyan.mean())
    if cyan_ratio > 0.25:
        return "draft", min(1.0, 0.6 + cyan_ratio)

    clock = layout.clock.crop(image)
    clock_gray = cv2.cvtColor(clock, cv2.COLOR_BGR2GRAY)
    bright = cv2.threshold(clock_gray, 90, 255, cv2.THRESH_BINARY)[1]
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    glyphs = 0
    for contour in contours:
        _, _, width, height = cv2.boundingRect(contour)
        if height >= bright.shape[0] * 0.22 and 2 <= width <= bright.shape[1] * 0.35:
            glyphs += 1

    hero_regions = layout.radiant_heroes + layout.dire_heroes
    hero_detail = 0.0
    if hero_regions:
        details = [
            float(cv2.cvtColor(region.crop(image), cv2.COLOR_BGR2GRAY).std())
            for region in hero_regions
        ]
        hero_detail = float(np.median(details))
    if glyphs >= 3 and hero_detail > 18:
        confidence = min(0.98, 0.55 + glyphs * 0.05 + hero_detail / 250)
        return "game", confidence
    return "unknown", 0.3
