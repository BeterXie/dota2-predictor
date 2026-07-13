from __future__ import annotations

import gzip
import hashlib
import json
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_intelligence.benchmarks import BenchmarkObservation, build_benchmark_snapshot
from event_intelligence.raw_archive import canonical_json_bytes, schema_fingerprint
from event_intelligence.registry import EventRegistry
from event_intelligence.storage import IntelligenceStorage
from event_intelligence.player_scoring import (
    ROLE_SCORING_CONFIGS,
    ResidualAdjustments,
    PlayerScoreInput,
    score_player_map,
    score_version_for_role,
    transform_player_metrics,
)
from scripts.score_strict_event_players import (
    StrictMap,
    StrictPlayerFact,
    _raw_metrics,
    build_scores,
    run_scoring,
)


UTC = timezone.utc
TARGET_START = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
CUTOFF = TARGET_START + timedelta(hours=1)


def complete_raw_metrics() -> dict[str, float]:
    return {
        "last_hits": 180,
        "net_worth": 12_000,
        "hero_damage": 24_000,
        "tower_damage": 3_000,
        "team_hero_damage": 80_000,
        "team_tower_damage": 8_000,
        "kills": 6,
        "deaths": 3,
        "assists": 14,
        "kills_assists": 20,
        "team_kills": 30,
        "roshan_participations": 2,
        "roshan_opportunities": 3,
        "late_fight_participations": 3,
        "late_fight_opportunities": 4,
        "late_fight_output": 8_000,
        "team_late_fight_output": 20_000,
        "gold_10_diff": 500,
        "last_hits_10_diff": 8,
        "early_kill_participations": 5,
        "early_team_kills": 8,
        "rune_pickups": 6,
        "early_objective_participations": 2,
        "early_objective_opportunities": 3,
        "opposing_carry_gold_suppression_at_10": 600,
        "opposing_carry_lh_suppression_at_10": 10,
        "damage_taken": 30_000,
        "control_seconds": 75,
        "initiations": 8,
        "initiation_opportunities": 10,
        "teamfight_participations": 7,
        "teamfight_opportunities": 8,
        "teamfight_impact": 18_000,
        "team_teamfight_impact": 50_000,
        "high_ground_participations": 2,
        "high_ground_opportunities": 3,
        "rotations_at_10": 4,
        "observer_wards": 8,
        "sentry_wards": 12,
        "dewards": 5,
        "objective_participations": 5,
        "objective_opportunities": 7,
        "saves": 3,
        "save_opportunities": 4,
        "hero_healing": 4_000,
        "pulls": 5,
        "pull_opportunities": 7,
        "stacks": 6,
        "lane_support_events": 8,
    }


def score_input(
    position: int,
    raw: dict[str, float] | None = None,
    *,
    confidence: float = 1.0,
    result_adjustment: float = 0.0,
    residuals: ResidualAdjustments | None = None,
    role_assignment_version: str = "observed-position-v1",
    role_assignment_cutoff: datetime | None = None,
) -> PlayerScoreInput:
    return PlayerScoreInput(
        match_id=8_001,
        player_id=10_001,
        player_slot=0,
        position=position,
        role_confidence=confidence,
        patch=60,
        duration_seconds=1_800,
        event_strength=1.0,
        target_started_at=TARGET_START,
        first_usable_at=CUTOFF - timedelta(minutes=1),
        role_assignment_source="single_map_evidence",
        role_assignment_cutoff=role_assignment_cutoff or CUTOFF - timedelta(minutes=2),
        role_assignment_input_hash="a" * 64,
        role_assignment_version=role_assignment_version,
        raw_metrics=tuple(sorted((raw or complete_raw_metrics()).items())),
        residuals=residuals or ResidualAdjustments(),
        result_adjustment=result_adjustment,
    )


def benchmark_for(value: PlayerScoreInput):
    transformed = transform_player_metrics(
        value.position, dict(value.raw_metrics), value.duration_seconds
    )
    observations = []
    for index, factor in enumerate((0.8, 0.9, 1.0, 1.1, 1.2), 1):
        completed = TARGET_START - timedelta(days=index)
        observations.append(
            BenchmarkObservation(
                match_id=7_000 + index,
                player_id=20_000 + index,
                position=value.position,
                patch=value.patch,
                duration_seconds=value.duration_seconds,
                event_strength=value.event_strength,
                completed_at=completed,
                first_usable_at=completed,
                role_assignment_source="single_map_evidence",
                role_assignment_cutoff=completed,
                role_assignment_input_hash=f"{index:064x}",
                role_assignment_version=value.role_assignment_version,
                metrics=tuple(
                    sorted(
                        (metric_id, metric_value * factor)
                        for metric_id, metric_value in transformed.items()
                        if metric_value is not None
                    )
                ),
            )
        )
    return build_benchmark_snapshot(
        observations,
        target_match_id=value.match_id,
        target_started_at=value.target_started_at,
        cutoff=CUTOFF,
        patch=value.patch,
        position=value.position,
        duration_seconds=value.duration_seconds,
        event_strength=value.event_strength,
        min_samples=5,
    )


class PlayerScoringTests(unittest.TestCase):
    def test_frozen_role_weights_sum_to_exactly_one(self) -> None:
        self.assertEqual({config.position for config in ROLE_SCORING_CONFIGS}, set(range(1, 6)))
        for config in ROLE_SCORING_CONFIGS:
            self.assertEqual(math.fsum(component.weight for component in config.components), 1.0)

    def test_transformations_are_explicit_and_source_exact(self) -> None:
        raw = complete_raw_metrics()
        transformed = transform_player_metrics(4, raw, duration_seconds=1_800)

        self.assertEqual(transformed["control_initiation.control_seconds"], 25.0)
        self.assertEqual(
            transformed["teamfights.teamfight_participations"],
            raw["teamfight_participations"] / raw["teamfight_opportunities"],
        )
        self.assertEqual(
            transformed["low_resource_efficiency.hero_damage"],
            raw["hero_damage"] / raw["net_worth"] * 1_000,
        )

    def test_missing_control_vision_and_damage_taken_reduce_coverage(self) -> None:
        for position, missing in (
            (3, {"damage_taken", "control_seconds"}),
            (4, {"control_seconds", "observer_wards", "sentry_wards", "dewards"}),
            (5, {"control_seconds", "observer_wards", "sentry_wards", "dewards"}),
        ):
            full_input = score_input(position)
            benchmark = benchmark_for(full_input)
            full = score_player_map(full_input, benchmark)
            reduced_raw = complete_raw_metrics()
            for metric in missing:
                reduced_raw.pop(metric)
            reduced = score_player_map(score_input(position, reduced_raw), benchmark)

            self.assertEqual(full.coverage, 1.0)
            self.assertLess(reduced.coverage, full.coverage)
            missing_results = [
                metric
                for component in reduced.components
                for metric in component.metrics
                if metric.raw_metric in missing
            ]
            self.assertTrue(missing_results)
            self.assertTrue(all(metric.transformed_value is None for metric in missing_results))
            self.assertTrue(all(metric.missing_reason == "source_missing" for metric in missing_results))

    def test_role_confidence_and_coverage_shrink_toward_neutral(self) -> None:
        base_input = score_input(1)
        benchmark = benchmark_for(base_input)
        stronger_raw = complete_raw_metrics()
        stronger_raw["last_hits"] *= 2
        stronger_raw["hero_damage"] *= 2
        full = score_player_map(score_input(1, stronger_raw), benchmark)
        uncertain = score_player_map(
            score_input(1, stronger_raw, confidence=0.5), benchmark
        )

        self.assertGreater(full.execution_score, 50.0)
        self.assertLess(abs(uncertain.execution_score - 50), abs(full.execution_score - 50))
        self.assertFalse(uncertain.ranking_eligible)

        empty = score_player_map(
            score_input(1, {"last_hits": None}, result_adjustment=5), benchmark
        )
        self.assertEqual(empty.execution_score, 50.0)
        self.assertEqual(empty.result_adjusted_score, 50.0)
        self.assertEqual(empty.result_adjustment_applied, 0.0)

    def test_residuals_are_explicit_and_result_adjustment_is_capped(self) -> None:
        value = score_input(
            2,
            residuals=ResidualAdjustments(
                opponent_strength=1.0,
                hero_matchup=-0.5,
                draft_expectation=0.25,
            ),
            result_adjustment=100,
        )
        benchmark = benchmark_for(value)
        positive = score_player_map(value, benchmark)
        negative = score_player_map(replace(value, result_adjustment=-100), benchmark)

        self.assertEqual(positive.result_adjustment_applied, 5.0)
        self.assertEqual(negative.result_adjustment_applied, -5.0)
        self.assertEqual(dict(positive.residual_points)["opponent_strength"], 1.0)
        self.assertLessEqual(positive.result_adjusted_score - positive.execution_score, 5.0)
        self.assertGreaterEqual(negative.result_adjusted_score - negative.execution_score, -5.0)

    def test_recompute_from_same_inputs_is_byte_equivalent(self) -> None:
        value = score_input(5)
        benchmark = benchmark_for(value)

        first = score_player_map(value, benchmark)
        second = score_player_map(value, benchmark)

        self.assertEqual(first, second)
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(len(first.input_hash), 64)
        self.assertEqual(first.benchmark_hash, benchmark.benchmark_hash)

    def test_role_dependency_is_in_input_hash_and_score_version(self) -> None:
        value = score_input(1)
        benchmark = benchmark_for(value)
        first = score_player_map(value, benchmark)
        changed = score_player_map(
            replace(value, role_assignment_version="observed-position-v2"), benchmark
        )

        self.assertNotEqual(first.input_hash, changed.input_hash)
        self.assertNotEqual(first.version, changed.version)
        self.assertEqual(
            changed.version, score_version_for_role("observed-position-v2")
        )

    def test_role_assignment_after_cutoff_is_rejected(self) -> None:
        value = score_input(
            1, role_assignment_cutoff=CUTOFF + timedelta(seconds=1)
        )
        benchmark = benchmark_for(replace(value, role_assignment_cutoff=CUTOFF))

        with self.assertRaisesRegex(ValueError, "role assignment"):
            score_player_map(value, benchmark)

    def test_exact_artifact_metrics_include_teamfights_objectives_and_carry_suppression(self) -> None:
        roles = (3, 2, 1, 4, 5, 1, 2, 3, 4, 5)
        slots = (0, 1, 2, 3, 4, 128, 129, 130, 131, 132)
        players = []
        raw_players = []
        for index, (slot, position) in enumerate(zip(slots, roles, strict=True)):
            facts = {
                **complete_raw_metrics(),
                "gold_at_10": 5_000 if slot == 0 else 4_500,
                "last_hits_at_10": 55 if slot == 0 else 50,
                "observer_wards_placed": 4,
                "sentry_wards_placed": 5,
                "observer_kills": 1,
                "sentry_kills": 1,
                "camps_stacked": 2,
                "stuns": 75,
            }
            players.append(
                StrictPlayerFact(
                    match_id=8_001,
                    player_slot=slot,
                    account_id=10_000 + index,
                    team_id=100 if slot < 128 else 200,
                    is_radiant=slot < 128,
                    position=position,
                    role_confidence=1.0,
                    role_assignment_source="single_map_evidence",
                    role_assignment_cutoff=CUTOFF,
                    role_assignment_input_hash=f"{index + 1:064x}",
                    role_assignment_version="observed-position-v1",
                    facts=facts,
                    first_usable_at=CUTOFF,
                    artifact_path=Path("unused"),
                    content_hash="b" * 64,
                )
            )
            raw_players.append({"player_slot": slot})
        fight_players = [
            {
                "damage": 100 + index,
                "healing": 10,
                "deaths": int(index == 2),
                "buybacks": 0,
                "killed": {"hero": 1} if index == 2 else {},
            }
            for index in range(10)
        ]
        game = StrictMap(
            8_001,
            TARGET_START,
            CUTOFF,
            2_400,
            60,
            True,
            tuple(players),
            {
                "players": raw_players,
                "teamfights": [
                    {"start": 900, "players": fight_players},
                    {"start": 1_900, "players": fight_players},
                ],
                "objectives": [
                    {
                        "type": "building_kill",
                        "key": "npc_dota_badguys_tower3_top",
                        "player_slot": 0,
                    },
                    {"type": "CHAT_MESSAGE_ROSHAN_KILL", "team": 2},
                ],
            },
        )

        raw = _raw_metrics(game, players[0])

        self.assertEqual(raw["opposing_carry_gold_suppression_at_10"], 500.0)
        self.assertEqual(raw["opposing_carry_lh_suppression_at_10"], 5.0)
        self.assertEqual(raw["teamfight_participations"], 2.0)
        self.assertEqual(raw["teamfight_opportunities"], 2.0)
        self.assertEqual(raw["late_fight_participations"], 1.0)
        self.assertEqual(raw["high_ground_participations"], 1.0)
        self.assertEqual(raw["high_ground_opportunities"], 1.0)
        self.assertEqual(raw["roshan_opportunities"], 1.0)
        self.assertIsNone(raw["roshan_participations"])
        self.assertIsNone(raw["objective_participations"])
        self.assertIsNone(raw.get("initiations"))
        transformed = transform_player_metrics(3, raw, game.duration_seconds)
        self.assertGreaterEqual(
            sum(value is not None for value in transformed.values()), 11
        )

        earlier_usable = TARGET_START - timedelta(hours=1)
        earlier_players = tuple(
            replace(
                player,
                match_id=8_000,
                first_usable_at=earlier_usable,
                role_assignment_cutoff=earlier_usable,
                role_assignment_input_hash=f"{index + 100:064x}",
            )
            for index, player in enumerate(players)
        )
        earlier_game = replace(
            game,
            match_id=8_000,
            started_at=TARGET_START - timedelta(hours=2),
            completed_at=earlier_usable,
            players=earlier_players,
        )
        all_scores = build_scores((earlier_game, game))
        one_map_scores = build_scores(
            (earlier_game, game), target_match_id=game.match_id
        )
        self.assertEqual(
            tuple(row for row in all_scores if row.score.match_id == game.match_id),
            one_map_scores,
        )

    def test_boolean_values_never_become_source_facts(self) -> None:
        value = score_input(1)
        benchmark = benchmark_for(value)
        raw = complete_raw_metrics()
        raw["last_hits"] = True

        result = score_player_map(score_input(1, raw), benchmark)

        farm = next(row for row in result.components if row.name == "farm_efficiency")
        last_hits = next(metric for metric in farm.metrics if metric.raw_metric == "last_hits")
        self.assertEqual(last_hits.missing_reason, "source_invalid")
        with self.assertRaisesRegex(ValueError, "non-finite"):
            score_player_map(
                score_input(1, {**complete_raw_metrics(), "last_hits": float("inf")}),
                benchmark,
            )


class StrictPlayerScoringCliTests(unittest.TestCase):
    def test_dry_run_and_repeated_write_are_idempotent_without_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "strict.db"
            storage = IntelligenceStorage(database)
            storage.init_schema()
            EventRegistry(storage).seed_approved_events()
            payload = {
                "match_id": 9_001,
                "start_time": int(TARGET_START.timestamp()),
                "duration": 1_800,
                "radiant_win": True,
                "patch": 60,
            }
            content = canonical_json_bytes(payload)
            content_hash = hashlib.sha256(content).hexdigest()
            artifact_path = root / f"{content_hash}.json.gz"
            compressed = gzip.compress(content, mtime=0)
            artifact_path.write_bytes(compressed)
            usable_at = (TARGET_START + timedelta(minutes=31)).isoformat()
            connection = storage.connection
            connection.execute(
                """INSERT INTO raw_source_artifacts
                   (artifact_id, content_hash, source, artifact_use, endpoint,
                    sanitized_request_identity, storage_path, uncompressed_bytes,
                    compressed_bytes, received_at, first_usable_at,
                    schema_fingerprint, event_id, match_id, created_at)
                   VALUES (?, ?, 'opendota', 'primary', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    content_hash,
                    content_hash,
                    "https://api.opendota.com/api/matches/9001",
                    "https://api.opendota.com/api/matches/9001",
                    str(artifact_path),
                    len(content),
                    len(compressed),
                    usable_at,
                    usable_at,
                    schema_fingerprint(payload),
                    "pgl-wallachia-s8-2026",
                    9_001,
                    usable_at,
                ),
            )
            connection.execute(
                """INSERT INTO match_ingest_status
                   (match_id, event_id, start_time, stage_scope, stage_in_scope,
                    has_valid_result, ingest_state, player_readiness,
                    latest_raw_artifact_id, latest_raw_content_hash,
                    discovered_at, updated_at)
                   VALUES (?, ?, ?, 'main_event', 1, 1, 'detailed', 'ready',
                           ?, ?, ?, ?)""",
                (
                    9_001,
                    "pgl-wallachia-s8-2026",
                    int(TARGET_START.timestamp()),
                    content_hash,
                    content_hash,
                    usable_at,
                    usable_at,
                ),
            )
            slots = (0, 1, 2, 3, 4, 128, 129, 130, 131, 132)
            for index, slot in enumerate(slots):
                facts = {
                    "kills": 1,
                    "deaths": 1,
                    "assists": 2,
                    "net_worth": 10_000,
                    "last_hits": 100 + index,
                    "hero_damage": 10_000,
                    "hero_healing": 100,
                    "tower_damage": 500,
                    "damage_taken": {"npc_dota_hero_axe": 2_000},
                    "stuns": 20,
                    "camps_stacked": 2,
                    "rune_pickups": 3,
                    "observer_wards_placed": 2,
                    "sentry_wards_placed": 3,
                    "observer_kills": 1,
                    "sentry_kills": 1,
                    "gold_at_10": 4_000 + index,
                    "last_hits_at_10": 40 + index,
                    "kills_at_10": 1,
                    "assists_at_10": 1,
                }
                connection.execute(
                    """INSERT INTO player_map_facts
                       (match_id, player_slot, account_id, team_id, hero_id,
                        is_radiant, facts_json, coverage, source_artifact_id,
                        source_content_hash, fact_version, first_usable_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?)""",
                    (
                        9_001,
                        slot,
                        20_000 + index,
                        100 if slot < 128 else 200,
                        index + 1,
                        int(slot < 128),
                        json.dumps(facts),
                        content_hash,
                        content_hash,
                        f"opendota-exact-v1:{content_hash}",
                        usable_at,
                        usable_at,
                    ),
                )
                connection.execute(
                    """INSERT INTO player_role_assignments
                       (match_id, player_slot, account_id, team_id, purpose,
                        position, assignment_source, confidence, input_cutoff,
                        input_hash, assignment_version, created_at)
                       VALUES (?, ?, ?, ?, 'observed_position', ?, 'audited_roster',
                               1.0, ?, ?, 'observed-position-v1', ?)""",
                    (
                        9_001,
                        slot,
                        20_000 + index,
                        100 if slot < 128 else 200,
                        index % 5 + 1,
                        usable_at,
                        hashlib.sha256(f"role-{index}".encode()).hexdigest(),
                        usable_at,
                    ),
                )
            connection.commit()
            storage.close()

            dry_run = run_scoring(database, dry_run=True)
            self.assertEqual((dry_run.eligible_maps, dry_run.scored_players), (1, 10))
            self.assertEqual(dry_run.inserted, 10)
            with IntelligenceStorage(database) as verification:
                self.assertEqual(
                    verification.connection.execute(
                        "SELECT COUNT(*) FROM player_map_scores"
                    ).fetchone()[0],
                    0,
                )

            first = run_scoring(database)
            second = run_scoring(database)
            self.assertEqual((first.inserted, first.updated, first.unchanged), (10, 0, 0))
            self.assertEqual((second.inserted, second.updated, second.unchanged), (0, 0, 10))
            with IntelligenceStorage(database) as verification:
                row = verification.connection.execute(
                    """SELECT score_version, component_facts_json
                       FROM player_map_scores WHERE match_id=9001 AND player_slot=0"""
                ).fetchone()
                self.assertEqual(
                    row["score_version"],
                    score_version_for_role("observed-position-v1"),
                )
                components = json.loads(row["component_facts_json"])
                last_hits = next(
                    metric
                    for component in components
                    for metric in component["metrics"]
                    if metric["raw_metric"] == "last_hits"
                )
                self.assertEqual(last_hits["numerator"], 100.0)

            with IntelligenceStorage(database) as update:
                update.connection.execute(
                    """INSERT INTO player_map_scores
                       (match_id, player_slot, account_id, position, execution_score,
                        result_adjusted_score, component_facts_json,
                        component_scores_json, weights_json, coverage,
                        role_confidence, benchmark_cutoff, benchmark_hash,
                        input_hash, score_version, explanation_json, created_at)
                       SELECT match_id, player_slot, account_id, position,
                              execution_score, result_adjusted_score,
                              component_facts_json, component_scores_json,
                              weights_json, coverage, role_confidence,
                              benchmark_cutoff, benchmark_hash, ?,
                              'player-score-v1', explanation_json, created_at
                       FROM player_map_scores
                       WHERE match_id=9001 AND player_slot=0""",
                    ("f" * 64,),
                )
                update.connection.execute(
                    """INSERT INTO player_role_assignments
                       (match_id, player_slot, account_id, team_id, purpose,
                        position, assignment_source, confidence, input_cutoff,
                        input_hash, assignment_version, created_at)
                       SELECT match_id, player_slot, account_id, team_id, purpose,
                              position, assignment_source, confidence, input_cutoff,
                              lower(hex(randomblob(32))), 'observed-position-v2', created_at
                       FROM player_role_assignments
                       WHERE assignment_version='observed-position-v1'"""
                )
                update.connection.commit()

            with self.assertRaisesRegex(ValueError, "--assignment-version"):
                run_scoring(database, dry_run=True)
            old = run_scoring(
                database, assignment_version="observed-position-v1"
            )
            new = run_scoring(
                database, assignment_version="observed-position-v2"
            )
            self.assertEqual(old.unchanged, 10)
            self.assertEqual(new.inserted, 10)
            with IntelligenceStorage(database) as verification:
                versions = verification.connection.execute(
                    """SELECT score_version, COUNT(*) AS count
                       FROM player_map_scores GROUP BY score_version
                       ORDER BY score_version"""
                ).fetchall()
                self.assertEqual(
                    [(row["score_version"], row["count"]) for row in versions],
                    [
                        ("player-score-v1", 1),
                        (score_version_for_role("observed-position-v1"), 10),
                        (score_version_for_role("observed-position-v2"), 10),
                    ],
                )


if __name__ == "__main__":
    unittest.main()
