"""Layout-aware, fail-closed reading of one Dota broadcast HUD frame."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    def diagnostics(self) -> "HudDiagnostics":
        return HudDiagnostics.from_reading(self)

    @property
    def core_hud_ready(self) -> bool:
        return (
            self.selection.supported
            and self.screen_state == "game"
            and self.replay_gate.status == "live"
            and self.clock.seconds is not None
            and self.clock.confidence >= COMEBACK_STATE_MIN_CONFIDENCE
            and self.scoreboard.radiant_kills is not None
            and self.scoreboard.dire_kills is not None
            and self.scoreboard.confidence >= COMEBACK_STATE_MIN_CONFIDENCE
        )

    @property
    def comeback_state_ready(self) -> bool:
        return (
            self.core_hud_ready
            and self.net_worth_advantage.side is not None
            and self.net_worth_advantage.minimum is not None
            and self.net_worth_advantage.maximum is not None
            and self.net_worth_advantage.confidence >= COMEBACK_STATE_MIN_CONFIDENCE
        )

    @property
    def draft_ready(self) -> bool:
        heroes = self.draft.radiant_hero_ids + self.draft.dire_hero_ids
        return (
            len(self.draft.radiant_hero_ids) == 5
            and len(self.draft.dire_hero_ids) == 5
            and len(set(heroes)) == 10
            and self.draft.confidence >= COMEBACK_STATE_MIN_CONFIDENCE
        )

    @property
    def is_hud_available(self) -> bool:
        """Return whether the frame has complete trusted live HUD facts."""
        return self.comeback_state_ready


@dataclass(frozen=True)
class HudDiagnostics:
    blocker_code: str
    layout_name: str | None
    layout_confidence: float
    layout_supported: bool
    screen_state: str
    screen_confidence: float
    replay_gate_status: str
    replay_gate_confidence: float
    clock_seconds: int | None
    clock_confidence: float
    clock_confirmed: bool
    radiant_kills: int | None
    dire_kills: int | None
    scoreboard_confidence: float
    scoreboard_confirmed: bool
    net_worth_side: str | None
    net_worth_minimum: int | None
    net_worth_maximum: int | None
    net_worth_confidence: float
    net_worth_confirmed: bool
    radiant_hero_count: int
    dire_hero_count: int
    draft_confidence: float
    draft_confirmed: bool
    team_side_confirmed: bool

    @classmethod
    def from_reading(cls, reading: HudFrameReading) -> "HudDiagnostics":
        clock_confirmed = (
            reading.clock.seconds is not None
            and reading.clock.confidence >= COMEBACK_STATE_MIN_CONFIDENCE
        )
        scoreboard_confirmed = (
            reading.scoreboard.radiant_kills is not None
            and reading.scoreboard.dire_kills is not None
            and reading.scoreboard.confidence >= COMEBACK_STATE_MIN_CONFIDENCE
        )
        net_worth_confirmed = (
            reading.net_worth_advantage.side is not None
            and reading.net_worth_advantage.minimum is not None
            and reading.net_worth_advantage.maximum is not None
            and reading.net_worth_advantage.confidence >= COMEBACK_STATE_MIN_CONFIDENCE
        )
        draft_confirmed = reading.draft_ready
        diagnostics = cls(
            blocker_code="ready",
            layout_name=reading.selection.layout_name,
            layout_confidence=reading.selection.confidence,
            layout_supported=reading.selection.supported,
            screen_state=reading.screen_state,
            screen_confidence=reading.screen_confidence,
            replay_gate_status=reading.replay_gate.status,
            replay_gate_confidence=reading.replay_gate.confidence,
            clock_seconds=reading.clock.seconds,
            clock_confidence=reading.clock.confidence,
            clock_confirmed=clock_confirmed,
            radiant_kills=reading.scoreboard.radiant_kills,
            dire_kills=reading.scoreboard.dire_kills,
            scoreboard_confidence=reading.scoreboard.confidence,
            scoreboard_confirmed=scoreboard_confirmed,
            net_worth_side=reading.net_worth_advantage.side,
            net_worth_minimum=reading.net_worth_advantage.minimum,
            net_worth_maximum=reading.net_worth_advantage.maximum,
            net_worth_confidence=reading.net_worth_advantage.confidence,
            net_worth_confirmed=net_worth_confirmed,
            radiant_hero_count=len(reading.draft.radiant_hero_ids),
            dire_hero_count=len(reading.draft.dire_hero_ids),
            draft_confidence=reading.draft.confidence,
            draft_confirmed=draft_confirmed,
            team_side_confirmed=False,
        )
        return diagnostics.with_confirmations()

    @property
    def core_hud_ready(self) -> bool:
        return (
            self.layout_supported
            and self.screen_state == "game"
            and self.replay_gate_status == "live"
            and self.clock_confirmed
            and self.scoreboard_confirmed
        )

    @property
    def comeback_state_ready(self) -> bool:
        return self.core_hud_ready and self.net_worth_confirmed

    @property
    def strategy_ready(self) -> bool:
        return (
            self.comeback_state_ready
            and self.draft_confirmed
            and self.team_side_confirmed
        )

    def with_confirmations(self, **updates: bool) -> "HudDiagnostics":
        diagnostics = replace(self, **updates)
        if not diagnostics.layout_supported:
            blocker = "unsupported_layout"
        elif diagnostics.screen_state != "game":
            blocker = "screen_not_game"
        elif diagnostics.replay_gate_status == "replay":
            blocker = "replay_detected"
        elif diagnostics.replay_gate_status != "live":
            blocker = "replay_gate_untrusted"
        elif not diagnostics.clock_confirmed:
            blocker = "clock_unconfirmed"
        elif not diagnostics.scoreboard_confirmed:
            blocker = "kill_score_unconfirmed"
        elif not diagnostics.net_worth_confirmed:
            blocker = "net_worth_advantage_unconfirmed"
        elif not diagnostics.draft_confirmed:
            blocker = "draft_unconfirmed"
        elif not diagnostics.team_side_confirmed:
            blocker = "team_side_unconfirmed"
        else:
            blocker = "ready"
        return replace(diagnostics, blocker_code=blocker)


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
        unavailable_gate = ReplayGateReading("untrusted", 0.0)
        unavailable_clock = ClockReading(None, 0.0, None)
        unavailable_scoreboard = ScoreboardReading(None, None, 0.0)
        unavailable_advantage = NetWorthAdvantageReading(None, None, None, 0.0)
        unavailable_draft = DraftReading((), (), 0.0)
        if selection.layout is None:
            return HudFrameReading(
                selection,
                "unknown",
                0.0,
                unavailable_gate,
                unavailable_clock,
                unavailable_scoreboard,
                unavailable_advantage,
                unavailable_draft,
            )
        profile = self._profile(selection.layout)
        screen_state, screen_confidence = classify_screen_state(image, selection.layout)
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
