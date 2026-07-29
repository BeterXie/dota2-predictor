"""Layout-aware, fail-closed reading of one Dota broadcast HUD frame."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from contracts.live_observation import COMEBACK_STATE_MIN_CONFIDENCE

from .clock_reader import ClockReader, ClockReading
from .hero_recognizer import DEFAULT_FEATURE_PATH, DraftReading, HeroRecognizer
from .layout_selector import LayoutSelection, select_broadcast_layout
from .layouts import BroadcastLayout
from .screen_state import classify_screen_state
from .scoreboard_reader import (
    NetWorthAdvantageReading,
    ReplayGateReading,
    ScoreboardReader,
    ScoreboardReading,
)


@dataclass(frozen=True)
class HudFrameReading:
    selection: LayoutSelection
    screen_state: str
    screen_confidence: float
    replay_gate: ReplayGateReading
    clock: ClockReading
    scoreboard: ScoreboardReading
    net_worth_advantage: NetWorthAdvantageReading
    draft: DraftReading

    @property
    def is_hud_available(self) -> bool:
        """Return whether the frame has complete trusted live HUD facts."""
        return (
            self.screen_state == "game"
            and self.replay_gate.status == "live"
            and self.clock.seconds is not None
            and self.clock.confidence >= COMEBACK_STATE_MIN_CONFIDENCE
            and self.scoreboard.radiant_kills is not None
            and self.scoreboard.dire_kills is not None
            and self.scoreboard.confidence >= COMEBACK_STATE_MIN_CONFIDENCE
            and self.net_worth_advantage.side is not None
            and self.net_worth_advantage.minimum is not None
            and self.net_worth_advantage.maximum is not None
            and self.net_worth_advantage.confidence >= COMEBACK_STATE_MIN_CONFIDENCE
        )


@dataclass
class _ProfileReaders:
    scoreboard: ScoreboardReader
    clock: ClockReader
    heroes: HeroRecognizer


class HudReader:
    """Select a supported overlay profile and read independent HUD components."""

    def __init__(
        self,
        feature_path: str | Path = DEFAULT_FEATURE_PATH,
        *,
        use_ocr: bool = True,
    ) -> None:
        self.feature_path = Path(feature_path)
        self.use_ocr = use_ocr
        self._profiles: dict[str, _ProfileReaders] = {}

    def _profile(self, layout: BroadcastLayout) -> _ProfileReaders:
        profile = self._profiles.get(layout.name)
        if profile is not None:
            return profile
        scoreboard = ScoreboardReader(layout, use_ocr=self.use_ocr)
        clock = ClockReader(layout, use_ocr=False)
        # Reuse one OCR runtime for the positioned strip and small-region fallback.
        clock.ocr = scoreboard.ocr
        profile = _ProfileReaders(
            scoreboard=scoreboard,
            clock=clock,
            heroes=HeroRecognizer(self.feature_path, layout),
        )
        self._profiles[layout.name] = profile
        return profile

    def read(self, image: np.ndarray) -> HudFrameReading:
        selection = select_broadcast_layout(image)
        profile = self._profile(selection.layout)
        screen_state, screen_confidence = classify_screen_state(image, selection.layout)
        unavailable_gate = ReplayGateReading("untrusted", 0.0)
        unavailable_clock = ClockReading(None, 0.0, None)
        unavailable_scoreboard = ScoreboardReading(None, None, 0.0)
        unavailable_advantage = NetWorthAdvantageReading(None, None, None, 0.0)
        unavailable_draft = DraftReading((), (), 0.0)
        if screen_state != "game":
            return HudFrameReading(
                selection,
                screen_state,
                screen_confidence,
                unavailable_gate,
                unavailable_clock,
                unavailable_scoreboard,
                unavailable_advantage,
                unavailable_draft,
            )

        replay_gate = profile.scoreboard.read_replay_gate(image)
        if replay_gate.status != "live":
            return HudFrameReading(
                selection,
                screen_state,
                screen_confidence,
                replay_gate,
                unavailable_clock,
                unavailable_scoreboard,
                unavailable_advantage,
                unavailable_draft,
            )

        clock = profile.scoreboard.read_positioned_clock(image) or profile.clock.read(
            image
        )
        return HudFrameReading(
            selection=selection,
            screen_state=screen_state,
            screen_confidence=screen_confidence,
            replay_gate=replay_gate,
            clock=clock,
            scoreboard=profile.scoreboard.read(image),
            net_worth_advantage=profile.scoreboard.read_net_worth_advantage(image),
            draft=profile.heroes.read(image),
        )
