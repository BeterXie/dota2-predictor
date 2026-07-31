"""Hero portrait recognition for the ten standard spectator HUD slots."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from vision.image_features import (
    ALLOWED_HERO_VARIANT_NAMES,
    MAX_VARIANTS_PER_HERO,
    color_histogram,
    compute_phash,
)
from vision.layouts import BroadcastLayout, STANDARD_DOTA_HUD


DEFAULT_FEATURE_PATH = (
    Path(__file__).resolve().parent / "templates" / "hero_features.npz"
)


@dataclass(frozen=True)
class HeroReading:
    hero_id: int | None
    confidence: float
    margin: float


@dataclass(frozen=True)
class HeroFeatureChannelScores:
    phash: float
    histogram: float
    pixel: float


@dataclass(frozen=True)
class HeroSlotDiagnostic:
    side: Literal["radiant", "dire"]
    slot: int
    best_hero_id: int | None
    best_score: float
    second_score: float
    margin: float
    accepted: bool
    reason: Literal[
        "accepted",
        "low_signal",
        "low_score",
        "ambiguous_match",
        "duplicate_hero",
    ]
    second_hero_id: int | None = None
    crop_hash: str | None = None
    best_channels: HeroFeatureChannelScores | None = None
    second_channels: HeroFeatureChannelScores | None = None
    best_variant: str | None = None
    hero_variant_count: int = 0
    second_variant: str | None = None
    second_hero_variant_count: int = 0


@dataclass(frozen=True)
class DraftReading:
    radiant_hero_ids: tuple[int, ...]
    dire_hero_ids: tuple[int, ...]
    confidence: float
    slot_diagnostics: tuple[HeroSlotDiagnostic, ...] = ()

    @property
    def recognized_slot_count(self) -> int:
        return sum(item.accepted for item in self.slot_diagnostics)


@dataclass(frozen=True)
class SlotCandidateEvidence:
    hero_id: int
    observed_at: float
    score: float
    margin: float
    crop_hash: str | None
    source_frame_hash: str | None = None
    game_clock_seconds: int | None = None


@dataclass(frozen=True)
class DraftSlotStatus:
    side: Literal["radiant", "dire"]
    slot: int
    state: Literal["observing", "provisional", "locked"]
    hero_id: int | None
    independent_evidence_count: int
    unique_crop_cluster_count: int
    high_quality_evidence_count: int
    strong_conflict_count: int
    duplicate_evidence_count: int
    last_observed_at: float | None
    evidence: tuple[SlotCandidateEvidence, ...]


@dataclass(frozen=True)
class _CropScores:
    combined: np.ndarray
    phash: np.ndarray
    histogram: np.ndarray
    pixel: np.ndarray
    variants: np.ndarray
    variant_counts: np.ndarray
    crop_hash: str


class HeroRecognizer:
    def __init__(
        self,
        feature_path: str | Path = DEFAULT_FEATURE_PATH,
        layout: BroadcastLayout = STANDARD_DOTA_HUD,
    ) -> None:
        with np.load(str(feature_path)) as data:
            variant_ids = np.asarray(data["ids"], dtype=np.int32)
            if "variant_names" in data.files:
                variant_names = np.asarray(data["variant_names"]).astype(str)
            else:
                if len(set(variant_ids.tolist())) != len(variant_ids):
                    raise ValueError(
                        "feature packs with duplicate hero ids require variant_names"
                    )
                variant_names = np.asarray(
                    [str(hero_id) for hero_id in variant_ids]
                )
            self.hashes = np.asarray(data["hashes"])
            self.histograms = np.asarray(data["histograms"])
            self.thumbnails = np.asarray(data["thumbnails"])
        template_count = len(variant_ids)
        arrays = (variant_names, self.hashes, self.histograms, self.thumbnails)
        if any(array.ndim == 0 or array.shape[0] != template_count for array in arrays):
            raise ValueError("hero feature arrays must have matching template counts")
        invalid_names = [
            name
            for hero_id, name in zip(variant_ids, variant_names, strict=True)
            if name != str(hero_id)
            and (
                not name.startswith(f"{hero_id}__")
                or name.removeprefix(f"{hero_id}__")
                not in ALLOWED_HERO_VARIANT_NAMES
            )
        ]
        if invalid_names:
            raise ValueError(f"invalid hero variant names: {sorted(invalid_names)}")
        if len(set(zip(variant_ids.tolist(), variant_names.tolist(), strict=True))) != template_count:
            raise ValueError("hero feature variant names must be unique per hero")
        self.ids = np.asarray(list(dict.fromkeys(variant_ids)), dtype=np.int32)
        groups: list[np.ndarray] = []
        for hero_id in self.ids:
            indices = np.flatnonzero(variant_ids == hero_id)
            base_name = str(hero_id)
            if base_name not in variant_names[indices]:
                raise ValueError(f"hero {hero_id} is missing its base portrait")
            if len(indices) > MAX_VARIANTS_PER_HERO:
                raise ValueError(
                    f"hero {hero_id} exceeds the {MAX_VARIANTS_PER_HERO} template limit"
                )
            ordered = sorted(
                (int(index) for index in indices),
                key=lambda index: (
                    variant_names[index] != base_name,
                    variant_names[index],
                ),
            )
            groups.append(np.asarray(ordered, dtype=np.intp))
        self._variant_groups = tuple(groups)
        self.variant_names = variant_names
        self.layout = layout

    def _score_crop(self, image: np.ndarray) -> _CropScores | None:
        if image.size == 0 or float(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).std()) < 8:
            return None
        hero_hash = compute_phash(image, hash_size=8)
        variant_hash_scores = 1.0 - np.mean(self.hashes != hero_hash, axis=1)
        histogram = color_histogram(image)
        variant_hist_scores = np.asarray(
            [
                (cv2.compareHist(histogram, candidate, cv2.HISTCMP_CORREL) + 1.0) / 2.0
                for candidate in self.histograms
            ]
        )
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        thumbnail = cv2.resize(gray, (48, 32), interpolation=cv2.INTER_AREA)
        variant_pixel_scores = np.asarray(
            [
                (
                    cv2.matchTemplate(thumbnail, candidate, cv2.TM_CCOEFF_NORMED)[0, 0]
                    + 1.0
                )
                / 2.0
                for candidate in self.thumbnails
            ]
        )
        variant_combined = (
            variant_hash_scores * 0.5
            + variant_hist_scores * 0.25
            + variant_pixel_scores * 0.25
        )
        winner_indices = np.asarray(
            [
                group[int(np.argmax(variant_combined[group]))]
                for group in self._variant_groups
            ],
            dtype=np.intp,
        )
        return _CropScores(
            combined=variant_combined[winner_indices],
            phash=variant_hash_scores[winner_indices],
            histogram=variant_hist_scores[winner_indices],
            pixel=variant_pixel_scores[winner_indices],
            variants=self.variant_names[winner_indices],
            variant_counts=np.asarray(
                [len(group) for group in self._variant_groups], dtype=np.int32
            ),
            crop_hash=np.packbits(hero_hash).tobytes().hex(),
        )

    def _scores(self, image: np.ndarray) -> np.ndarray | None:
        scored = self._score_crop(image)
        return None if scored is None else scored.combined

    @staticmethod
    def _channels(scores: _CropScores, index: int) -> HeroFeatureChannelScores:
        return HeroFeatureChannelScores(
            phash=float(scores.phash[index]),
            histogram=float(scores.histogram[index]),
            pixel=float(scores.pixel[index]),
        )

    def recognize_crop(self, image: np.ndarray) -> HeroReading:
        scored = self._score_crop(image)
        if scored is None:
            return HeroReading(None, 0.0, 0.0)
        scores = scored.combined
        order = np.argsort(scores)[::-1]
        best, second = int(order[0]), int(order[1])
        confidence = float(scores[best])
        margin = float(scores[best] - scores[second])
        if confidence < 0.62 or margin < 0.025:
            return HeroReading(None, confidence, margin)
        return HeroReading(int(self.ids[best]), confidence, margin)

    def read(self, image: np.ndarray) -> DraftReading:
        regions = self.layout.radiant_heroes + self.layout.dire_heroes
        score_rows = [self._score_crop(region.crop(image)) for region in regions]
        if len(score_rows) != 10:
            return DraftReading((), (), 0.0)

        diagnostics: list[HeroSlotDiagnostic] = []
        for index, scored in enumerate(score_rows):
            side: Literal["radiant", "dire"] = "radiant" if index < 5 else "dire"
            slot = index % 5 + 1
            if scored is None or len(scored.combined) < 2:
                diagnostics.append(
                    HeroSlotDiagnostic(
                        side, slot, None, 0.0, 0.0, 0.0, False, "low_signal"
                    )
                )
                continue
            scores = scored.combined
            order = np.argsort(scores)[::-1]
            best, second = int(order[0]), int(order[1])
            best_score = float(scores[best])
            second_score = float(scores[second])
            margin = best_score - second_score
            if best_score < 0.62:
                accepted = False
                reason = "low_score"
            elif margin < 0.025:
                accepted = False
                reason = "ambiguous_match"
            else:
                accepted = True
                reason = "accepted"
            diagnostics.append(
                HeroSlotDiagnostic(
                    side,
                    slot,
                    int(self.ids[best]),
                    best_score,
                    second_score,
                    margin,
                    accepted,
                    reason,
                    second_hero_id=int(self.ids[second]),
                    crop_hash=scored.crop_hash,
                    best_channels=self._channels(scored, best),
                    second_channels=self._channels(scored, second),
                    best_variant=str(scored.variants[best]),
                    hero_variant_count=int(scored.variant_counts[best]),
                    second_variant=str(scored.variants[second]),
                    second_hero_variant_count=int(scored.variant_counts[second]),
                )
            )

        accepted_ids = [
            item.best_hero_id
            for item in diagnostics
            if item.accepted and item.best_hero_id is not None
        ]
        duplicate_ids = {
            hero_id for hero_id in accepted_ids if accepted_ids.count(hero_id) > 1
        }
        if duplicate_ids:
            diagnostics = [
                replace(item, accepted=False, reason="duplicate_hero")
                if item.best_hero_id in duplicate_ids
                else item
                for item in diagnostics
            ]

        accepted = [item for item in diagnostics if item.accepted]
        confidence = min((item.best_score for item in accepted), default=0.0)
        if len(accepted) != 10:
            return DraftReading((), (), confidence, tuple(diagnostics))
        ids = [item.best_hero_id for item in accepted]
        assert all(hero_id is not None for hero_id in ids)
        complete_ids = [int(hero_id) for hero_id in ids if hero_id is not None]
        return DraftReading(
            tuple(complete_ids[:5]),
            tuple(complete_ids[5:]),
            confidence,
            tuple(diagnostics),
        )


@dataclass
class _TrackedSlot:
    evidence: deque[SlotCandidateEvidence]
    state: Literal["observing", "provisional", "locked"] = "observing"
    hero_id: int | None = None
    provisional_at: float | None = None
    locked_confidence: float = 0.0
    strong_conflict_count: int = 0
    duplicate_evidence_count: int = 0
    last_independent: SlotCandidateEvidence | None = None
    last_observed_at: float | None = None


class DraftTracker:
    def __init__(
        self,
        confirmations: int | None = None,
        *,
        evidence_window: int = 7,
        provisional_support: int = 5,
        minimum_high_quality: int = 2,
        minimum_evidence_interval: float = 3.0,
        maximum_similar_hash_distance: int = 2,
        maximum_evidence_per_crop_cluster: int = 3,
        minimum_lock_interval: float | None = None,
        high_quality_score: float = 0.67,
        high_quality_margin: float = 0.04,
    ) -> None:
        if confirmations is not None:
            provisional_support = confirmations
            evidence_window = max(evidence_window, confirmations)
        if not 1 <= provisional_support <= evidence_window:
            raise ValueError("provisional_support must fit inside evidence_window")
        if not 1 <= minimum_high_quality <= provisional_support:
            raise ValueError("minimum_high_quality must not exceed provisional_support")
        if maximum_evidence_per_crop_cluster < 1:
            raise ValueError("maximum_evidence_per_crop_cluster must be positive")
        if minimum_lock_interval is None:
            minimum_lock_interval = (
                minimum_evidence_interval if confirmations is not None else 20.0
            )
        if minimum_lock_interval < minimum_evidence_interval:
            raise ValueError(
                "minimum_lock_interval must not be shorter than evidence interval"
            )
        self.evidence_window = evidence_window
        self.provisional_support = provisional_support
        self.minimum_high_quality = minimum_high_quality
        self.minimum_evidence_interval = minimum_evidence_interval
        self.maximum_similar_hash_distance = maximum_similar_hash_distance
        self.maximum_evidence_per_crop_cluster = (
            maximum_evidence_per_crop_cluster
        )
        self.minimum_lock_interval = minimum_lock_interval
        self.high_quality_score = high_quality_score
        self.high_quality_margin = high_quality_margin
        self._slots = [
            _TrackedSlot(deque(maxlen=evidence_window)) for _ in range(10)
        ]

    def reset(self) -> None:
        self._slots = [
            _TrackedSlot(deque(maxlen=self.evidence_window)) for _ in range(10)
        ]

    @staticmethod
    def _diagnostics(reading: DraftReading) -> tuple[HeroSlotDiagnostic, ...]:
        if len(reading.slot_diagnostics) == 10:
            return reading.slot_diagnostics
        heroes = reading.radiant_hero_ids + reading.dire_hero_ids
        if len(heroes) != 10:
            return ()
        return tuple(
            HeroSlotDiagnostic(
                "radiant" if index < 5 else "dire",
                index % 5 + 1,
                hero_id,
                reading.confidence,
                0.0,
                reading.confidence,
                True,
                "accepted",
            )
            for index, hero_id in enumerate(heroes)
        )

    @staticmethod
    def _hash_distance(left: str, right: str) -> int:
        try:
            return (int(left, 16) ^ int(right, 16)).bit_count()
        except ValueError:
            return 64

    def _is_high_quality(self, evidence: SlotCandidateEvidence) -> bool:
        return (
            evidence.score >= self.high_quality_score
            and evidence.margin >= self.high_quality_margin
        )

    def _crop_clusters(
        self, evidence: list[SlotCandidateEvidence]
    ) -> list[list[SlotCandidateEvidence]]:
        clusters: list[list[SlotCandidateEvidence]] = []
        representatives: list[str | None] = []
        for item in evidence:
            if item.crop_hash is None:
                representatives.append(None)
                clusters.append([item])
                continue
            cluster_index = next(
                (
                    index
                    for index, representative in enumerate(representatives)
                    if representative is not None
                    and self._hash_distance(representative, item.crop_hash)
                    <= self.maximum_similar_hash_distance
                ),
                None,
            )
            if cluster_index is None:
                representatives.append(item.crop_hash)
                clusters.append([item])
            else:
                clusters[cluster_index].append(item)
        return clusters

    def _effective_support(self, evidence: list[SlotCandidateEvidence]) -> int:
        return sum(
            min(len(cluster), self.maximum_evidence_per_crop_cluster)
            for cluster in self._crop_clusters(evidence)
        )

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
            if previous.source_frame_hash == evidence.source_frame_hash:
                return False
            if (
                previous.game_clock_seconds is not None
                and evidence.game_clock_seconds is not None
            ):
                return evidence.game_clock_seconds > previous.game_clock_seconds
        if previous.crop_hash is None or evidence.crop_hash is None:
            return True
        return (
            self._hash_distance(previous.crop_hash, evidence.crop_hash)
            > self.maximum_similar_hash_distance
        )

    def _enter_observing(
        self, slot: _TrackedSlot, evidence: SlotCandidateEvidence
    ) -> None:
        slot.state = "observing"
        slot.hero_id = None
        slot.provisional_at = None
        slot.locked_confidence = 0.0
        slot.strong_conflict_count = 0
        slot.evidence.clear()
        slot.evidence.append(evidence)

    def _maybe_enter_provisional(self, slot: _TrackedSlot) -> None:
        counts = Counter(item.hero_id for item in slot.evidence)
        if not counts:
            return
        hero_id, support = counts.most_common(1)[0]
        supporting = [item for item in slot.evidence if item.hero_id == hero_id]
        if (
            support < self.provisional_support
            or self._effective_support(supporting) < self.provisional_support
        ):
            return
        if sum(self._is_high_quality(item) for item in supporting) < self.minimum_high_quality:
            return
        if any(
            item.hero_id != hero_id and self._is_high_quality(item)
            for item in slot.evidence
        ):
            return
        slot.state = "provisional"
        slot.hero_id = hero_id
        slot.provisional_at = slot.evidence[-1].observed_at
        slot.strong_conflict_count = 0

    def _apply_independent_evidence(
        self, slot: _TrackedSlot, evidence: SlotCandidateEvidence
    ) -> None:
        slot.evidence.append(evidence)
        if slot.state == "observing":
            self._maybe_enter_provisional(slot)
            return

        assert slot.hero_id is not None
        high_quality = self._is_high_quality(evidence)
        if slot.state == "provisional":
            if evidence.hero_id != slot.hero_id and high_quality:
                self._enter_observing(slot, evidence)
                return
            if (
                evidence.hero_id == slot.hero_id
                and high_quality
                and slot.provisional_at is not None
                and evidence.observed_at - slot.provisional_at
                >= self.minimum_lock_interval
            ):
                supporting = [
                    item.score for item in slot.evidence if item.hero_id == slot.hero_id
                ]
                slot.state = "locked"
                slot.locked_confidence = min(supporting, default=evidence.score)
            return

        if evidence.hero_id == slot.hero_id:
            if high_quality:
                slot.strong_conflict_count = 0
            return
        if not high_quality:
            return
        slot.strong_conflict_count += 1
        if slot.strong_conflict_count >= 2:
            slot.state = "provisional"
            slot.provisional_at = evidence.observed_at
            slot.locked_confidence = 0.0
            slot.evidence.clear()
            slot.evidence.append(evidence)

    @property
    def slot_statuses(self) -> tuple[DraftSlotStatus, ...]:
        statuses: list[DraftSlotStatus] = []
        for index, slot in enumerate(self._slots):
            supporting = [
                item
                for item in slot.evidence
                if slot.hero_id is not None and item.hero_id == slot.hero_id
            ]
            statuses.append(
                DraftSlotStatus(
                    side="radiant" if index < 5 else "dire",
                    slot=index % 5 + 1,
                    state=slot.state,
                    hero_id=slot.hero_id,
                    independent_evidence_count=len(supporting),
                    unique_crop_cluster_count=len(
                        self._crop_clusters(supporting)
                    ),
                    high_quality_evidence_count=sum(
                        self._is_high_quality(item) for item in supporting
                    ),
                    strong_conflict_count=slot.strong_conflict_count,
                    duplicate_evidence_count=slot.duplicate_evidence_count,
                    last_observed_at=slot.last_observed_at,
                    evidence=tuple(slot.evidence),
                )
            )
        return tuple(statuses)

    @property
    def current_draft(self) -> DraftReading | None:
        if any(slot.state != "locked" or slot.hero_id is None for slot in self._slots):
            return None
        ids = [int(slot.hero_id) for slot in self._slots if slot.hero_id is not None]
        if len(ids) != 10 or len(set(ids)) != 10:
            return None
        return DraftReading(
            tuple(ids[:5]),
            tuple(ids[5:]),
            min(slot.locked_confidence for slot in self._slots),
        )

    def update(
        self,
        reading: DraftReading,
        *,
        observed_at: float,
        source_frame_hash: str | None = None,
        game_clock_seconds: int | None = None,
    ) -> DraftReading | None:
        diagnostics = self._diagnostics(reading)
        if len(diagnostics) != 10:
            return self.current_draft

        for index, diagnostic in enumerate(diagnostics):
            if not diagnostic.accepted or diagnostic.best_hero_id is None:
                continue
            slot = self._slots[index]
            slot.last_observed_at = observed_at
            evidence = SlotCandidateEvidence(
                hero_id=diagnostic.best_hero_id,
                observed_at=observed_at,
                score=diagnostic.best_score,
                margin=diagnostic.margin,
                crop_hash=diagnostic.crop_hash,
                source_frame_hash=source_frame_hash,
                game_clock_seconds=game_clock_seconds,
            )
            if not self._is_independent(slot, evidence):
                slot.duplicate_evidence_count += 1
                continue
            slot.last_independent = evidence
            self._apply_independent_evidence(slot, evidence)

        return self.current_draft
