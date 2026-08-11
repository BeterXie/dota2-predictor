"""Stable production adapters for the existing RayBet vision watcher.

This module deliberately wraps the existing readers instead of replacing the
current production path in-place.  ``scripts/watch_raybet_stream_stable.py``
installs these adapters into the existing watcher so the stabilized path can be
shadow-tested before it becomes the default.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

from contracts.live_observation import DraftPlayerNames
from vision.clock_reader import ClockReader, ClockReading
from vision.frame_quality import FrameQuality, FrameQualityTracker
from vision.hero_recognizer import (
    DEFAULT_FEATURE_PATH,
    DraftReading,
    DraftTracker,
    HeroRecognizer,
    HeroSlotDiagnostic,
    SlotCandidateEvidence,
    _TrackedSlot,
)
from vision.hud_reader import HudFrameReading, HudReader, _ProfileReaders
from vision.layout_selector import (
    LayoutSelection,
    draft_lineup_complete,
    epl_masters_draft_layout_confidence,
    epl_masters_layout_confidence,
    epl_s39_layout_confidence,
    standard_dota_hud_layout_confidence,
    wxc_gotf_2026_layout_confidence,
)
from vision.layout_tracker import LayoutTracker, StableLayoutState
from vision.layouts import (
    BroadcastLayout,
    EPL_MASTERS_DRAFT,
    EPL_MASTERS_LIVE,
    EPL_S39_LIVE,
    STANDARD_DOTA_HUD,
    WXC_GOTF_2026_LIVE,
)
from vision.scoreboard_reader import (
    NetWorthAdvantageReading,
    ReplayGateReading,
    ScoreboardReader,
    ScoreboardReading,
)
from vision.profile_features import promoted_profile_feature_path
from vision.player_name_reader import DraftPlayerNameReader
from vision.screen_state import classify_screen_state
from vision.stream_capture import HLSStreamCapture, StreamFrame
from vision.vision_debug import VisionDebugSink


_LAYOUTS: dict[str, BroadcastLayout] = {
    layout.name: layout
    for layout in (
        EPL_MASTERS_DRAFT,
        EPL_MASTERS_LIVE,
        EPL_S39_LIVE,
        WXC_GOTF_2026_LIVE,
        STANDARD_DOTA_HUD,
    )
}


def broadcast_layout_scores(image: np.ndarray) -> dict[str, float]:
    """Return all supported layout scores rather than a single frame winner."""
    return {
        EPL_MASTERS_DRAFT.name: epl_masters_draft_layout_confidence(image),
        EPL_MASTERS_LIVE.name: epl_masters_layout_confidence(image),
        EPL_S39_LIVE.name: epl_s39_layout_confidence(image),
        WXC_GOTF_2026_LIVE.name: wxc_gotf_2026_layout_confidence(image),
        STANDARD_DOTA_HUD.name: standard_dota_hud_layout_confidence(image),
    }


class StableHLSStreamCapture(HLSStreamCapture):
    """Add content identity without replacing the stream-source identity."""

    @staticmethod
    def _frame_identity(image: np.ndarray, stream_identity: str) -> str:
        contiguous = np.ascontiguousarray(image)
        digest = hashlib.blake2b(digest_size=16)
        digest.update(stream_identity.encode())
        digest.update(str((contiguous.shape, contiguous.dtype)).encode())
        digest.update(contiguous.data)
        return digest.hexdigest()

    def read(self, *, timeout: float = 20.0) -> StreamFrame:
        frame = super().read(timeout=timeout)
        return replace(
            frame,
            frame_hash=self._frame_identity(frame.image, frame.source_hash),
        )


class StableDraftTracker(DraftTracker):
    """Treat progressing video/game evidence as independent even on one stream."""

    def _effective_support(self, evidence: list[SlotCandidateEvidence]) -> int:
        # The parent tracker caps one perceptual crop cluster at three votes.
        # That is unsafe for a static hero portrait: a perfectly stable pHash
        # can otherwise never reach the default provisional_support=5.  Items
        # reaching this deque have already passed the temporal/frame/clock
        # independence gate, so counting them directly is the correct support
        # measure for the stabilized runtime.
        return len(evidence)

    def _is_independent(
        self, slot: _TrackedSlot, evidence: SlotCandidateEvidence
    ) -> bool:
        previous = slot.last_independent
        if previous is None:
            return True
        if evidence.observed_at - previous.observed_at < self.minimum_evidence_interval:
            return False

        if (
            previous.source_frame_hash is not None
            and evidence.source_frame_hash is not None
        ):
            return previous.source_frame_hash != evidence.source_frame_hash

        if (
            previous.game_clock_seconds is not None
            and evidence.game_clock_seconds is not None
        ):
            if evidence.game_clock_seconds > previous.game_clock_seconds:
                return True
            if evidence.game_clock_seconds < previous.game_clock_seconds:
                return False

        if previous.crop_hash is None or evidence.crop_hash is None:
            return True
        return (
            self._hash_distance(previous.crop_hash, evidence.crop_hash)
            > self.maximum_similar_hash_distance
        )

    def _apply_independent_evidence(
        self, slot: _TrackedSlot, evidence: SlotCandidateEvidence
    ) -> None:
        if slot.state != "locked":
            super()._apply_independent_evidence(slot, evidence)
            return

        # A Dota lineup cannot change during one map.  Once a slot is locked,
        # conflicting OCR observations are diagnostics, not a state transition.
        if evidence.hero_id == slot.hero_id:
            slot.evidence.append(evidence)
            if self._is_high_quality(evidence):
                slot.strong_conflict_count = 0
        elif self._is_high_quality(evidence):
            slot.strong_conflict_count += 1


class StableHeroRecognizer(HeroRecognizer):
    """Resolve the ten slots jointly so duplicate local winners do not poison a draft."""

    global_reassignment_tolerance = 0.04

    def read(self, image: np.ndarray) -> DraftReading:
        regions = self.layout.radiant_heroes + self.layout.dire_heroes
        scored_rows = [self._score_crop(region.crop(image)) for region in regions]
        if len(scored_rows) != 10:
            return DraftReading((), (), 0.0)

        hero_count = len(self.ids)
        score_matrix = np.full((10, hero_count), -1.0, dtype=np.float64)
        for row_index, scored in enumerate(scored_rows):
            if scored is not None:
                score_matrix[row_index] = scored.combined

        row_indices, column_indices = linear_sum_assignment(-score_matrix)
        assignment = {int(row): int(column) for row, column in zip(row_indices, column_indices, strict=True)}

        diagnostics: list[HeroSlotDiagnostic] = []
        for index, scored in enumerate(scored_rows):
            side = "radiant" if index < 5 else "dire"
            slot = index % 5 + 1
            if scored is None or len(scored.combined) < 2 or index not in assignment:
                diagnostics.append(
                    HeroSlotDiagnostic(
                        side, slot, None, 0.0, 0.0, 0.0, False, "low_signal"
                    )
                )
                continue

            assigned = assignment[index]
            scores = scored.combined
            local_order = np.argsort(scores)[::-1]
            local_best = int(local_order[0])
            assigned_score = float(scores[assigned])
            local_best_score = float(scores[local_best])

            alternatives = [
                int(candidate)
                for candidate in local_order
                if int(candidate) != assigned
            ]
            second = alternatives[0]
            second_score = float(scores[second])

            if assigned == local_best:
                margin = assigned_score - second_score
                globally_plausible = True
            else:
                # A duplicate local winner may be owned by another slot.  Accept a
                # reassignment only when the assigned candidate was nearly tied
                # locally; otherwise fail closed instead of forcing a unique hero.
                assigned_columns = set(assignment.values())
                feasible_alternatives = [
                    int(candidate)
                    for candidate in local_order
                    if int(candidate) not in assigned_columns
                ]
                if feasible_alternatives:
                    second = feasible_alternatives[0]
                    second_score = float(scores[second])
                else:
                    second_score = 0.0
                margin = assigned_score - second_score
                globally_plausible = (
                    local_best_score - assigned_score
                    <= self.global_reassignment_tolerance
                )

            if assigned_score < 0.62:
                accepted = False
                reason = "low_score"
            elif not globally_plausible or margin < 0.025:
                accepted = False
                reason = "ambiguous_match"
            else:
                accepted = True
                reason = "accepted"

            diagnostics.append(
                HeroSlotDiagnostic(
                    side,
                    slot,
                    int(self.ids[assigned]),
                    assigned_score,
                    second_score,
                    margin,
                    accepted,
                    reason,
                    second_hero_id=int(self.ids[second]),
                    crop_hash=scored.crop_hash,
                    best_channels=self._channels(scored, assigned),
                    second_channels=self._channels(scored, second),
                    best_variant=str(scored.variants[assigned]),
                    hero_variant_count=int(scored.variant_counts[assigned]),
                    second_variant=str(scored.variants[second]),
                    second_hero_variant_count=int(scored.variant_counts[second]),
                )
            )

        accepted = [item for item in diagnostics if item.accepted]
        confidence = min((item.best_score for item in accepted), default=0.0)
        if len(accepted) != 10:
            return DraftReading((), (), confidence, tuple(diagnostics))
        ids = [item.best_hero_id for item in diagnostics]
        if any(hero_id is None for hero_id in ids):
            return DraftReading((), (), confidence, tuple(diagnostics))
        complete = [int(hero_id) for hero_id in ids if hero_id is not None]
        if len(set(complete)) != 10:
            return DraftReading((), (), confidence, tuple(diagnostics))
        return DraftReading(
            tuple(complete[:5]),
            tuple(complete[5:]),
            confidence,
            tuple(diagnostics),
        )


class StableHudReader(HudReader):
    """Sticky layout selection and perception that is independent of trust gates."""

    def __init__(
        self,
        feature_path: str | Path = DEFAULT_FEATURE_PATH,
        *,
        use_ocr: bool = True,
    ) -> None:
        super().__init__(feature_path, use_ocr=use_ocr)
        self.layout_tracker = LayoutTracker()
        self.frame_quality_tracker = FrameQualityTracker()
        debug_root = os.environ.get("VISION_DEBUG_DIR")
        self.debug_sink = VisionDebugSink(debug_root) if debug_root else None
        forced_layout = os.environ.get("VISION_LAYOUT_PROFILE")
        self.forced_layout_name = (
            None if forced_layout in {None, "", "auto"} else forced_layout
        )
        if self.forced_layout_name is not None and self.forced_layout_name not in _LAYOUTS:
            raise ValueError(
                f"unsupported VISION_LAYOUT_PROFILE: {self.forced_layout_name}"
            )
        self.last_layout_state = StableLayoutState(None, 0.0, "unsupported")
        self.last_frame_quality: FrameQuality | None = None
        self.debug_context: dict[str, object] | None = None

    def set_debug_context(
        self,
        *,
        raybet_match_id: str,
        map_number: int,
        captured_at_utc: datetime,
        source_frame_ref: str,
    ) -> None:
        self.debug_context = {
            "raybet_match_id": raybet_match_id,
            "map_number": map_number,
            "captured_at_utc": captured_at_utc,
            "source_frame_ref": source_frame_ref,
        }

    def _profile(self, layout: BroadcastLayout) -> _ProfileReaders:
        profile = self._profiles.get(layout.name)
        if profile is not None:
            return profile
        scoreboard = ScoreboardReader(layout, use_ocr=self.use_ocr)
        clock = ClockReader(layout, use_ocr=False)
        clock.ocr = scoreboard.ocr
        feature_path = self.feature_path
        hero_features_ready = True
        if Path(feature_path).resolve() == DEFAULT_FEATURE_PATH.resolve():
            promoted = promoted_profile_feature_path(layout.name)
            feature_path = promoted or feature_path
            hero_features_ready = (
                promoted is not None or not layout.draft_completion_cyan_regions
            )
        profile = _ProfileReaders(
            scoreboard=scoreboard,
            clock=clock,
            heroes=StableHeroRecognizer(feature_path, layout),
            player_names=DraftPlayerNameReader(layout, ocr=scoreboard.ocr),
            hero_features_ready=hero_features_ready,
        )
        self._profiles[layout.name] = profile
        return profile

    def _selection(self, image: np.ndarray) -> LayoutSelection:
        if self.forced_layout_name is not None:
            layout = _LAYOUTS[self.forced_layout_name]
            self.last_layout_state = StableLayoutState(
                layout.name, 1.0, "locked", consecutive_support=1
            )
            return LayoutSelection(layout, 1.0, True)
        state = self.layout_tracker.update(broadcast_layout_scores(image))
        self.last_layout_state = state
        layout = _LAYOUTS.get(state.layout_name or "")
        return LayoutSelection(
            layout,
            state.confidence,
            layout is not None,
            None if layout is not None else "unsupported_layout",
        )

    @staticmethod
    def _unavailable(
        selection: LayoutSelection,
        screen_state: str,
        screen_confidence: float,
        *,
        draft: DraftReading | None = None,
        draft_player_names: DraftPlayerNames | None = None,
        replay_gate: ReplayGateReading | None = None,
    ) -> HudFrameReading:
        return HudFrameReading(
            selection,
            screen_state,
            screen_confidence,
            replay_gate or ReplayGateReading("untrusted", 0.0),
            ClockReading(None, 0.0, None),
            ScoreboardReading(None, None, 0.0),
            NetWorthAdvantageReading(None, None, None, 0.0),
            draft or DraftReading((), (), 0.0),
            draft_player_names or DraftPlayerNames.unavailable(),
        )

    def _debug(self, image: np.ndarray, reading: HudFrameReading) -> None:
        if self.debug_sink is None:
            return
        diagnostics = reading.diagnostics
        quality_reason = (
            self.last_frame_quality.reason
            if self.last_frame_quality is not None and not self.last_frame_quality.usable
            else None
        )
        layout_reason = (
            f"layout_{self.last_layout_state.state}"
            if self.last_layout_state.state in {"degraded", "switching"}
            else None
        )
        if (
            quality_reason is None
            and layout_reason is None
            and diagnostics.blocker_code == "ready"
            and not diagnostics.draft_failed_slots
        ):
            return
        layout = reading.selection.layout
        hero_regions = () if layout is None else layout.radiant_heroes + layout.dire_heroes
        context = getattr(self, "debug_context", None) or {}
        self.debug_sink.record(
            image,
            reason=quality_reason or layout_reason or diagnostics.blocker_code,
            layout_name=reading.selection.layout_name,
            diagnostics={
                "hud": diagnostics,
                "layout_tracker": self.last_layout_state,
                "frame_quality": self.last_frame_quality,
            },
            hero_regions=hero_regions,
            raybet_match_id=context.get("raybet_match_id"),
            map_number=context.get("map_number"),
            captured_at_utc=context.get("captured_at_utc"),
            source_frame_ref=context.get("source_frame_ref"),
        )

    def read(self, image: np.ndarray) -> HudFrameReading:
        self.last_frame_quality = self.frame_quality_tracker.assess(image)
        selection = self._selection(image)
        if selection.layout is None:
            reading = self._unavailable(selection, "unknown", 0.0)
            self._debug(image, reading)
            return reading

        profile = self._profile(selection.layout)
        screen_state, screen_confidence = classify_screen_state(image, selection.layout)

        if (
            self.last_frame_quality is not None
            and not self.last_frame_quality.usable
            and screen_state == "game"
        ):
            reading = self._unavailable(
                selection,
                "game",
                screen_confidence,
                replay_gate=ReplayGateReading(
                    "untrusted", 0.0, self.last_frame_quality.reason
                ),
            )
            self._debug(image, reading)
            return reading

        if screen_state == "draft":
            # Draft-frame hero evidence remains observational.  The existing
            # watcher still decides whether/when it is promoted into live state.
            lineup_complete = draft_lineup_complete(image, selection.layout)
            draft = (
                profile.heroes.read(image)
                if profile.hero_features_ready
                and lineup_complete
                else DraftReading((), (), 0.0)
            )
            player_names = (
                profile.player_names.read(image)
                if lineup_complete
                else DraftPlayerNames.unavailable("draft_lineup_incomplete")
            )
            player_names = profile.player_names.bind_heroes(
                player_names,
                draft.radiant_hero_ids,
                draft.dire_hero_ids,
            )
            reading = self._unavailable(
                selection,
                screen_state,
                screen_confidence,
                draft=draft,
                draft_player_names=player_names,
            )
            self._debug(image, reading)
            return reading

        if screen_state != "game":
            reading = self._unavailable(selection, screen_state, screen_confidence)
            self._debug(image, reading)
            return reading

        replay_gate = profile.scoreboard.read_replay_gate(image)
        # Crucial difference from the legacy HudReader: perception continues on
        # untrusted/replay frames.  The watcher may freeze publication, but a
        # single OCR gate failure no longer prevents us from observing the HUD.
        clock = profile.scoreboard.read_positioned_clock(image) or profile.clock.read(image)
        reading = HudFrameReading(
            selection=selection,
            screen_state=screen_state,
            screen_confidence=screen_confidence,
            replay_gate=replay_gate,
            clock=clock,
            scoreboard=profile.scoreboard.read(image),
            net_worth_advantage=profile.scoreboard.read_net_worth_advantage(image),
            draft=profile.heroes.read(image),
        )
        self._debug(image, reading)
        return reading


def freeze_untrusted_live_hud_tracking(
    replay_gate: ReplayGateReading,
    **_: object,
) -> bool:
    """Fail closed for publication without destroying accumulated tracker state."""
    return replay_gate.status == "live"


def freeze_untrusted_draft_tracking(
    draft_tracker: DraftTracker,
    last_draft: DraftReading | None,
) -> DraftReading | None:
    """Keep a locked map lineup while live HUD publication is frozen."""
    return draft_tracker.current_draft or last_draft


def require_target_team_confirmation(*, radiant_team_side: str | None) -> bool:
    """Do not let a preceding or unrelated broadcast poison an immutable lineup."""
    return radiant_team_side is not None


def install_stable_runtime(watcher_module: object) -> None:
    """Install stable adapters into ``scripts.watch_raybet_stream`` in-process."""
    setattr(watcher_module, "HudReader", StableHudReader)
    setattr(watcher_module, "DraftTracker", StableDraftTracker)
    setattr(watcher_module, "HLSStreamCapture", StableHLSStreamCapture)
    setattr(
        watcher_module,
        "allow_live_hud_tracking",
        freeze_untrusted_live_hud_tracking,
    )
    setattr(
        watcher_module,
        "draft_during_untrusted",
        freeze_untrusted_draft_tracking,
    )
    setattr(
        watcher_module,
        "allow_target_draft_tracking",
        require_target_team_confirmation,
    )
