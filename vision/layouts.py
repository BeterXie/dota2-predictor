"""Resolution-independent regions for supported Dota broadcast layouts."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class NormalizedRegion:
    left: float
    top: float
    right: float
    bottom: float

    def crop(self, image: np.ndarray) -> np.ndarray:
        height, width = image.shape[:2]
        x1, x2 = int(self.left * width), int(self.right * width)
        y1, y2 = int(self.top * height), int(self.bottom * height)
        if x2 <= x1 or y2 <= y1:
            raise ValueError("normalized region has no area")
        return image[y1:y2, x1:x2]


@dataclass(frozen=True)
class BroadcastLayout:
    name: str
    clock: NormalizedRegion
    draft_banner: NormalizedRegion
    radiant_heroes: tuple[NormalizedRegion, ...] = field(default_factory=tuple)
    dire_heroes: tuple[NormalizedRegion, ...] = field(default_factory=tuple)
    radiant_team_logo: NormalizedRegion | None = None
    dire_team_logo: NormalizedRegion | None = None


STANDARD_DOTA_HUD = BroadcastLayout(
    name="standard_dota_hud_1080p",
    clock=NormalizedRegion(0.470, 0.015, 0.530, 0.047),
    draft_banner=NormalizedRegion(0.04, 0.05, 0.96, 0.15),
    radiant_heroes=tuple(
        NormalizedRegion(0.286 + index * 0.0315, 0.0, 0.3175 + index * 0.0315, 0.045)
        for index in range(5)
    ),
    dire_heroes=tuple(
        NormalizedRegion(0.556 + index * 0.0315, 0.0, 0.5875 + index * 0.0315, 0.045)
        for index in range(5)
    ),
    radiant_team_logo=NormalizedRegion(0.250, 0.0, 0.286, 0.060),
    dire_team_logo=NormalizedRegion(0.714, 0.0, 0.750, 0.060),
)


EPL_POSTGAME = BroadcastLayout(
    name="epl_s39_postgame_1080p",
    clock=NormalizedRegion(0.455, 0.0, 0.545, 0.07),
    draft_banner=NormalizedRegion(0.04, 0.05, 0.96, 0.15),
    radiant_heroes=tuple(
        NormalizedRegion(0.012, 0.757 + index * 0.047, 0.048, 0.804 + index * 0.047)
        for index in range(5)
    ),
    dire_heroes=tuple(
        NormalizedRegion(0.516, 0.757 + index * 0.047, 0.552, 0.804 + index * 0.047)
        for index in range(5)
    ),
)
