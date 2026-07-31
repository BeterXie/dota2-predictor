"""Hero portrait recognition for the ten standard spectator HUD slots."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from vision.image_features import color_histogram, compute_phash
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


@dataclass(frozen=True)
class DraftReading:
    radiant_hero_ids: tuple[int, ...]
    dire_hero_ids: tuple[int, ...]
    confidence: float
    slot_diagnostics: tuple[HeroSlotDiagnostic, ...] = ()

    @property
    def recognized_slot_count(self) -> int:
        return sum(item.accepted for item in self.slot_diagnostics)


class HeroRecognizer:
    def __init__(
        self,
        feature_path: str | Path = DEFAULT_FEATURE_PATH,
        layout: BroadcastLayout = STANDARD_DOTA_HUD,
    ) -> None:
        data = np.load(str(feature_path))
        self.ids = data["ids"]
        self.hashes = data["hashes"]
        self.histograms = data["histograms"]
        self.thumbnails = data["thumbnails"]
        self.layout = layout

    def _scores(self, image: np.ndarray) -> np.ndarray | None:
        if image.size == 0 or float(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).std()) < 8:
            return None
        hero_hash = compute_phash(image, hash_size=8)
        hash_scores = 1.0 - np.mean(self.hashes != hero_hash, axis=1)
        histogram = color_histogram(image)
        hist_scores = np.asarray(
            [
                (cv2.compareHist(histogram, candidate, cv2.HISTCMP_CORREL) + 1.0) / 2.0
                for candidate in self.histograms
            ]
        )
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        thumbnail = cv2.resize(gray, (48, 32), interpolation=cv2.INTER_AREA)
        pixel_scores = np.asarray(
            [
                (
                    cv2.matchTemplate(thumbnail, candidate, cv2.TM_CCOEFF_NORMED)[0, 0]
                    + 1.0
                )
                / 2.0
                for candidate in self.thumbnails
            ]
        )
        return hash_scores * 0.5 + hist_scores * 0.25 + pixel_scores * 0.25

    def recognize_crop(self, image: np.ndarray) -> HeroReading:
        scores = self._scores(image)
        if scores is None:
            return HeroReading(None, 0.0, 0.0)
        order = np.argsort(scores)[::-1]
        best, second = int(order[0]), int(order[1])
        confidence = float(scores[best])
        margin = float(scores[best] - scores[second])
        if confidence < 0.62 or margin < 0.025:
            return HeroReading(None, confidence, margin)
        return HeroReading(int(self.ids[best]), confidence, margin)

    def read(self, image: np.ndarray) -> DraftReading:
        regions = self.layout.radiant_heroes + self.layout.dire_heroes
        score_rows = [self._scores(region.crop(image)) for region in regions]
        if len(score_rows) != 10:
            return DraftReading((), (), 0.0)

        diagnostics: list[HeroSlotDiagnostic] = []
        for index, scores in enumerate(score_rows):
            side: Literal["radiant", "dire"] = "radiant" if index < 5 else "dire"
            slot = index % 5 + 1
            if scores is None or len(scores) < 2:
                diagnostics.append(
                    HeroSlotDiagnostic(
                        side, slot, None, 0.0, 0.0, 0.0, False, "low_signal"
                    )
                )
                continue
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


class DraftTracker:
    def __init__(self, confirmations: int = 3) -> None:
        self.confirmations = confirmations
        self._recent: tuple[deque[tuple[int, float]], ...] = tuple(
            deque(maxlen=confirmations) for _ in range(10)
        )
        self._locked: dict[int, tuple[int, float]] = {}

    def reset(self) -> None:
        for recent in self._recent:
            recent.clear()
        self._locked.clear()

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

    def update(self, reading: DraftReading) -> DraftReading | None:
        diagnostics = self._diagnostics(reading)
        if len(diagnostics) != 10:
            return None

        for index, diagnostic in enumerate(diagnostics):
            if index in self._locked:
                continue
            recent = self._recent[index]
            if not diagnostic.accepted or diagnostic.best_hero_id is None:
                recent.clear()
                continue
            recent.append((diagnostic.best_hero_id, diagnostic.best_score))
            if len(recent) < self.confirmations:
                continue
            hero_ids = {hero_id for hero_id, _ in recent}
            if len(hero_ids) != 1:
                continue
            hero_id = recent[-1][0]
            if any(locked_id == hero_id for locked_id, _ in self._locked.values()):
                recent.clear()
                continue
            self._locked[index] = (
                hero_id,
                min(score for _, score in recent),
            )

        if len(self._locked) != 10:
            return None
        ids = [self._locked[index][0] for index in range(10)]
        if len(set(ids)) != 10:
            return None
        confidence = min(score for _, score in self._locked.values())
        return DraftReading(
            tuple(ids[:5]),
            tuple(ids[5:]),
            confidence,
        )
