"""Leakage checks and map-grouped chronological dataset splitting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class EpisodeRow:
    raybet_match_id: str
    map_number: int
    feature_at: datetime
    decision_at: datetime
    label_available_at: datetime | None
    features: dict[str, float]
    label: int | None = None


def audit_no_leakage(row: EpisodeRow) -> None:
    if row.feature_at > row.decision_at:
        raise ValueError("feature timestamp follows decision timestamp")
    if row.label is not None and row.label_available_at is None:
        raise ValueError("labeled row must record label availability")
    if row.label_available_at is not None and row.label_available_at <= row.decision_at:
        raise ValueError("post-match label was available at or before decision")


def chronological_split(
    rows: list[EpisodeRow], train_fraction: float = 0.70, validation_fraction: float = 0.15
) -> tuple[list[EpisodeRow], list[EpisodeRow], list[EpisodeRow]]:
    if not 0 < train_fraction < 1 or not 0 <= validation_fraction < 1:
        raise ValueError("invalid split fractions")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("train and validation fractions must leave a test set")
    for row in rows:
        audit_no_leakage(row)
    maps: dict[tuple[str, int], list[EpisodeRow]] = {}
    for row in rows:
        maps.setdefault((row.raybet_match_id, row.map_number), []).append(row)
    ordered = sorted(maps.values(), key=lambda group: min(row.decision_at for row in group))
    train_end = int(len(ordered) * train_fraction)
    validation_end = int(len(ordered) * (train_fraction + validation_fraction))

    def flatten(groups: list[list[EpisodeRow]]) -> list[EpisodeRow]:
        return [row for group in groups for row in group]

    return (
        flatten(ordered[:train_end]),
        flatten(ordered[train_end:validation_end]),
        flatten(ordered[validation_end:]),
    )
