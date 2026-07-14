"""Hero portrait recognition for the ten standard spectator HUD slots."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment

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
class DraftReading:
    radiant_hero_ids: tuple[int, ...]
    dire_hero_ids: tuple[int, ...]
    confidence: float


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
        if len(score_rows) != 10 or any(row is None for row in score_rows):
            return DraftReading((), (), 0.0)
        matrix = np.vstack(score_rows)
        slot_indices, hero_indices = linear_sum_assignment(-matrix)
        assigned = dict(zip(slot_indices.tolist(), hero_indices.tolist()))
        ids = []
        confidences = []
        for slot in range(10):
            hero_index = assigned[slot]
            score = float(matrix[slot, hero_index])
            alternatives = np.delete(matrix[slot], hero_index)
            margin = score - float(alternatives.max())
            if score < 0.60 or margin < -0.015:
                return DraftReading((), (), score)
            ids.append(int(self.ids[hero_index]))
            confidences.append(score)
        confidence = min(confidences)
        return DraftReading(tuple(ids[:5]), tuple(ids[5:]), confidence)


class DraftTracker:
    def __init__(self, confirmations: int = 3) -> None:
        self._recent: deque[DraftReading] = deque(maxlen=confirmations)

    def reset(self) -> None:
        self._recent.clear()

    def update(self, reading: DraftReading) -> DraftReading | None:
        if len(reading.radiant_hero_ids) != 5 or len(reading.dire_hero_ids) != 5:
            self._recent.clear()
            return None
        self._recent.append(reading)
        if len(self._recent) < self._recent.maxlen:
            return None
        drafts = {(row.radiant_hero_ids, row.dire_hero_ids) for row in self._recent}
        if len(drafts) != 1:
            return None
        return DraftReading(
            reading.radiant_hero_ids,
            reading.dire_hero_ids,
            min(0.99, 0.75 + min(row.confidence for row in self._recent) * 0.25),
        )
