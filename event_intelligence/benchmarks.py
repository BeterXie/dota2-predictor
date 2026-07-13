"""Earlier-only robust benchmark snapshots for player-map scoring."""

from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .raw_archive import canonical_json_bytes


BENCHMARK_VERSION = "player-benchmark-v2"
MAD_SCALE = 1.4826


@dataclass(frozen=True)
class BenchmarkObservation:
    match_id: int
    player_id: int
    position: int
    patch: int | None
    duration_seconds: int
    event_strength: float
    completed_at: datetime
    first_usable_at: datetime | None
    role_assignment_source: str
    role_assignment_cutoff: datetime
    role_assignment_input_hash: str
    role_assignment_version: str
    metrics: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class BenchmarkMetric:
    metric_id: str
    median: float
    mad: float
    sample_size: int
    fallback_level: str


@dataclass(frozen=True)
class BenchmarkSnapshot:
    version: str
    target_match_id: int
    target_started_at: datetime
    cutoff: datetime
    patch: int | None
    position: int
    duration_band: str
    event_strength_band: str
    minimum_samples: int
    metrics: tuple[BenchmarkMetric, ...]
    source_match_ids: tuple[int, ...]
    source_input_hash: str

    def get(self, metric_id: str) -> BenchmarkMetric | None:
        return next((metric for metric in self.metrics if metric.metric_id == metric_id), None)

    def require(self, metric_id: str) -> BenchmarkMetric:
        metric = self.get(metric_id)
        if metric is None:
            raise KeyError(metric_id)
        return metric

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(_snapshot_payload(self))

    @property
    def benchmark_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def duration_band(duration_seconds: int) -> str:
    if isinstance(duration_seconds, bool) or duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    minutes = duration_seconds / 60
    if minutes < 25:
        return "lt25"
    if minutes < 35:
        return "25_34"
    if minutes < 45:
        return "35_44"
    if minutes < 60:
        return "45_59"
    return "ge60"


def event_strength_band(event_strength: float) -> str:
    if not math.isfinite(event_strength) or event_strength < 0:
        raise ValueError("event_strength must be a finite non-negative number")
    if event_strength >= 0.9:
        return "elite"
    if event_strength >= 0.75:
        return "strong"
    return "standard"


def robust_z(value: float, *, median: float, mad: float) -> float:
    if mad < 0:
        raise ValueError("mad cannot be negative")
    if mad == 0:
        if value == median:
            return 0.0
        return 3.0 if value > median else -3.0
    return max(-3.0, min(3.0, (value - median) / (MAD_SCALE * mad)))


def _statistic(metric_id: str, values: list[float], level: str) -> BenchmarkMetric:
    center = float(statistics.median(values))
    mad = float(statistics.median(abs(value - center) for value in values))
    return BenchmarkMetric(metric_id, center, mad, len(values), level)


def _observation_payload(row: BenchmarkObservation) -> tuple[object, ...]:
    return (
        row.match_id,
        row.player_id,
        row.position,
        row.patch,
        row.duration_seconds,
        row.event_strength,
        row.completed_at.astimezone(timezone.utc).isoformat(),
        row.first_usable_at.astimezone(timezone.utc).isoformat()
        if row.first_usable_at is not None
        else None,
        row.role_assignment_source,
        row.role_assignment_cutoff.astimezone(timezone.utc).isoformat(),
        row.role_assignment_input_hash,
        row.role_assignment_version,
        tuple(sorted(row.metrics)),
    )


def _snapshot_payload(snapshot: BenchmarkSnapshot) -> dict[str, object]:
    return {
        "version": snapshot.version,
        "target_match_id": snapshot.target_match_id,
        "target_started_at": snapshot.target_started_at.isoformat(),
        "cutoff": snapshot.cutoff.isoformat(),
        "patch": snapshot.patch,
        "position": snapshot.position,
        "duration_band": snapshot.duration_band,
        "event_strength_band": snapshot.event_strength_band,
        "minimum_samples": snapshot.minimum_samples,
        "metrics": [
            (
                metric.metric_id,
                metric.median,
                metric.mad,
                metric.sample_size,
                metric.fallback_level,
            )
            for metric in snapshot.metrics
        ],
        "source_match_ids": snapshot.source_match_ids,
        "source_input_hash": snapshot.source_input_hash,
    }


def build_benchmark_snapshot(
    observations: Iterable[BenchmarkObservation],
    *,
    target_match_id: int,
    target_started_at: datetime,
    cutoff: datetime,
    patch: int | None,
    position: int,
    duration_seconds: int,
    event_strength: float,
    min_samples: int = 5,
) -> BenchmarkSnapshot:
    """Build a snapshot after excluding target, future, and late-usable rows."""
    if position not in (1, 2, 3, 4, 5):
        raise ValueError("position must be between 1 and 5")
    if min_samples < 1:
        raise ValueError("min_samples must be positive")
    target_started_at = _utc(target_started_at, "target_started_at")
    cutoff = _utc(cutoff, "cutoff")
    target_duration_band = duration_band(duration_seconds)
    target_strength_band = event_strength_band(event_strength)
    eligible = tuple(
        sorted(
            (
                row
                for row in observations
                if row.match_id != target_match_id
                and row.completed_at < target_started_at
                and row.completed_at <= cutoff
                and row.first_usable_at is not None
                and row.first_usable_at <= cutoff
                and row.role_assignment_cutoff <= cutoff
            ),
            key=lambda row: (row.completed_at, row.match_id, row.player_id),
        )
    )
    source_bytes = canonical_json_bytes([_observation_payload(row) for row in eligible])
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    level_names = (
        "patch_position_duration_event",
        "patch_position_duration",
        "patch_position",
        "position",
        "global",
    )
    buckets: dict[str, dict[str, list[float]]] = {
        level: {} for level in level_names
    }
    for row in eligible:
        matching_levels = ["global"]
        if row.position == position:
            matching_levels.append("position")
            if row.patch == patch:
                matching_levels.append("patch_position")
                if duration_band(row.duration_seconds) == target_duration_band:
                    matching_levels.append("patch_position_duration")
                    if (
                        event_strength_band(row.event_strength)
                        == target_strength_band
                    ):
                        matching_levels.append("patch_position_duration_event")
        for metric_id, value in dict(row.metrics).items():
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                continue
            for level in matching_levels:
                buckets[level].setdefault(metric_id, []).append(float(value))

    metric_ids = sorted(buckets["global"])
    statistics_rows: list[BenchmarkMetric] = []
    for metric_id in metric_ids:
        selected: BenchmarkMetric | None = None
        for level in level_names:
            values = buckets[level].get(metric_id, [])
            if len(values) >= min_samples:
                selected = _statistic(metric_id, values, level)
                break
        if selected is None:
            values = buckets["global"].get(metric_id, [])
            if values:
                selected = _statistic(metric_id, values, "global_sparse")
        if selected is not None:
            statistics_rows.append(selected)

    return BenchmarkSnapshot(
        version=BENCHMARK_VERSION,
        target_match_id=target_match_id,
        target_started_at=target_started_at,
        cutoff=cutoff,
        patch=patch,
        position=position,
        duration_band=target_duration_band,
        event_strength_band=target_strength_band,
        minimum_samples=min_samples,
        metrics=tuple(statistics_rows),
        source_match_ids=tuple(sorted({row.match_id for row in eligible})),
        source_input_hash=source_hash,
    )
