from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from event_intelligence.benchmarks import (
    BenchmarkObservation,
    build_benchmark_snapshot,
    duration_band,
    event_strength_band,
    robust_z,
)


UTC = timezone.utc
TARGET_START = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
CUTOFF = TARGET_START + timedelta(hours=1)
METRIC = "farm_efficiency.last_hits"


def observation(
    match_id: int,
    value: float,
    *,
    position: int = 1,
    patch: int = 60,
    duration: int = 2_400,
    strength: float = 1.0,
    completed_at: datetime | None = None,
    first_usable_at: datetime | None = None,
    role_assignment_cutoff: datetime | None = None,
    role_assignment_version: str = "observed-position-v1",
) -> BenchmarkObservation:
    completed = completed_at or TARGET_START - timedelta(days=match_id)
    usable = first_usable_at if first_usable_at is not None else completed
    return BenchmarkObservation(
        match_id=match_id,
        player_id=10_000 + match_id,
        position=position,
        patch=patch,
        duration_seconds=duration,
        event_strength=strength,
        completed_at=completed,
        first_usable_at=usable,
        role_assignment_source="single_map_evidence",
        role_assignment_cutoff=role_assignment_cutoff or usable,
        role_assignment_input_hash=f"{match_id:064x}",
        role_assignment_version=role_assignment_version,
        metrics=((METRIC, value),),
    )


def snapshot(rows, *, min_samples: int = 3):
    return build_benchmark_snapshot(
        rows,
        target_match_id=8_001,
        target_started_at=TARGET_START,
        cutoff=CUTOFF,
        patch=60,
        position=1,
        duration_seconds=2_400,
        event_strength=1.0,
        min_samples=min_samples,
    )


class PlayerBenchmarkTests(unittest.TestCase):
    def test_exact_cell_uses_median_and_mad(self) -> None:
        result = snapshot(
            [observation(index, value) for index, value in enumerate((1, 2, 3, 4, 100), 1)]
        )
        metric = result.require(METRIC)

        self.assertEqual(metric.median, 3.0)
        self.assertEqual(metric.mad, 1.0)
        self.assertEqual(metric.sample_size, 5)
        self.assertEqual(metric.fallback_level, "patch_position_duration_event")
        self.assertEqual(len(result.benchmark_hash), 64)

    def test_sparse_exact_cell_uses_documented_fallback_order(self) -> None:
        rows = [
            observation(1, 10, strength=1.0),
            observation(2, 11, strength=1.0),
            observation(3, 12, strength=0.8),
            observation(4, 13, strength=0.8),
        ]
        metric = snapshot(rows, min_samples=3).require(METRIC)

        self.assertEqual(metric.sample_size, 4)
        self.assertEqual(metric.fallback_level, "patch_position_duration")

    def test_future_and_late_usable_rows_do_not_change_snapshot_or_hash(self) -> None:
        earlier = [observation(index, value) for index, value in enumerate((8, 9, 10), 1)]
        baseline = snapshot(earlier)
        future = observation(
            90,
            1_000_000,
            completed_at=TARGET_START + timedelta(seconds=1),
            first_usable_at=TARGET_START + timedelta(seconds=2),
        )
        late = observation(
            91,
            -1_000_000,
            completed_at=TARGET_START - timedelta(days=1),
            first_usable_at=CUTOFF + timedelta(seconds=1),
        )
        late_role = observation(
            92,
            500_000,
            completed_at=TARGET_START - timedelta(days=2),
            first_usable_at=TARGET_START - timedelta(days=1),
            role_assignment_cutoff=CUTOFF + timedelta(seconds=1),
        )

        changed = snapshot([future, *reversed(earlier), late, late_role])

        self.assertEqual(changed, baseline)
        self.assertEqual(changed.canonical_bytes(), baseline.canonical_bytes())
        self.assertNotIn(90, changed.source_match_ids)
        self.assertNotIn(91, changed.source_match_ids)
        self.assertNotIn(92, changed.source_match_ids)

    def test_role_identity_changes_benchmark_hash(self) -> None:
        rows = [observation(index, value) for index, value in enumerate((8, 9, 10), 1)]
        baseline = snapshot(rows)
        changed = snapshot(
            [replace(row, role_assignment_version="observed-position-v2") for row in rows]
        )

        self.assertNotEqual(changed.source_input_hash, baseline.source_input_hash)
        self.assertNotEqual(changed.benchmark_hash, baseline.benchmark_hash)

    def test_global_sparse_is_explicit_when_no_level_meets_minimum(self) -> None:
        result = snapshot(
            [observation(1, 7, position=5, patch=59, duration=1_200, strength=0.5)],
            min_samples=5,
        )
        metric = result.require(METRIC)
        self.assertEqual(metric.fallback_level, "global_sparse")
        self.assertEqual(metric.sample_size, 1)

    def test_robust_z_is_mad_scaled_and_bounded(self) -> None:
        self.assertAlmostEqual(robust_z(4.4826, median=3, mad=1), 1.0)
        self.assertEqual(robust_z(999, median=3, mad=1), 3.0)
        self.assertEqual(robust_z(-999, median=3, mad=1), -3.0)
        self.assertEqual(robust_z(3, median=3, mad=0), 0.0)
        self.assertEqual(robust_z(4, median=3, mad=0), 3.0)

    def test_boolean_metric_is_not_a_numeric_benchmark_fact(self) -> None:
        row = observation(1, 7)
        result = snapshot(
            [replace(row, metrics=((METRIC, True),))],
            min_samples=1,
        )
        self.assertIsNone(result.get(METRIC))

    def test_bands_have_stable_boundaries(self) -> None:
        self.assertEqual(duration_band(24 * 60), "lt25")
        self.assertEqual(duration_band(25 * 60), "25_34")
        self.assertEqual(duration_band(60 * 60), "ge60")
        self.assertEqual(event_strength_band(0.74), "standard")
        self.assertEqual(event_strength_band(0.75), "strong")
        self.assertEqual(event_strength_band(0.9), "elite")


if __name__ == "__main__":
    unittest.main()
