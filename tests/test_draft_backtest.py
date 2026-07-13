from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_intelligence.backtest import (
    EvaluationPoint,
    _profile_state,
    evaluate_points,
    load_draft_corpus,
    run_strict_draft_backtest,
)
from event_intelligence.draft_features import (
    AvailabilityMode,
    build_draft_feature_snapshot,
)
from event_intelligence.player_scoring import score_version_for_role
from event_intelligence.storage import IntelligenceStorage
from event_intelligence.team_profiles import (
    AvailabilityMode as ProfileAvailabilityMode,
    ProfileMap,
    build_team_style_profile,
)
from event_intelligence.team_states import Side, build_team_map_states
from fetch.db import Database


UTC = timezone.utc
START = datetime(2026, 4, 10, 0, 0, tzinfo=UTC)
ASSIGNMENT_VERSION = "role-assignment-test-v1-reconstructed-walk-forward"
PROSPECTIVE_ASSIGNMENT_VERSION = "role-assignment-test-v1-prospective"
EVENT_ID = "pgl-wallachia-s8-2026"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _facts(hero_id: int) -> dict[str, object]:
    return {
        "hero_id": hero_id,
        "stuns": 12.0,
        "hero_healing": 100,
        "last_hits": 200,
        "tower_damage": 2_000,
        "net_worth": 20_000,
        "buyback_log": [],
    }


class DraftBacktestFixture:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.storage = IntelligenceStorage(path)
        self.storage.init_schema()
        Database(connection=self.storage.connection).init_db()
        self.connection = self.storage.connection
        self.league_id = int(
            self.connection.execute(
                "SELECT opendota_league_id FROM event_registry WHERE event_id=?",
                (EVENT_ID,),
            ).fetchone()[0]
        )

    def close(self) -> None:
        self.storage.close()

    def add_map(
        self,
        sequence: int,
        *,
        in_scope: bool = True,
        duration_minutes: int = 60,
        observed_reverse: bool = False,
    ) -> int:
        match_id = 9_000 + sequence
        started = START + timedelta(hours=2 * sequence)
        duration = duration_minutes * 60
        completed = started + timedelta(seconds=duration)
        usable = completed + timedelta(minutes=1)
        content_hash = _hash(f"map:{match_id}")
        artifact_id = f"opendota:{content_hash}"
        now = usable.isoformat()
        self.connection.execute(
            """INSERT INTO raw_source_artifacts
               (artifact_id, content_hash, source, artifact_use, endpoint,
                sanitized_request_identity, storage_path, uncompressed_bytes,
                compressed_bytes, received_at, first_usable_at,
                schema_fingerprint, event_id, match_id, created_at)
               VALUES (?, ?, 'opendota', 'primary', ?, ?, ?, 1, 1, ?, ?,
                       'test-schema', ?, ?, ?)""",
            (
                artifact_id,
                content_hash,
                f"/api/matches/{match_id}",
                f"GET /api/matches/{match_id}",
                f"raw/{match_id}.json.gz",
                now,
                now,
                EVENT_ID,
                match_id,
                now,
            ),
        )
        self.connection.execute(
            """INSERT INTO match_ingest_status
               (match_id, event_id, start_time, series_id, map_number,
                stage_scope, stage_in_scope, has_valid_result, is_exhibition,
                is_forfeit, is_void_remake, ingest_state, basic_result_state,
                detailed_parse_state, player_readiness, state_readiness,
                draft_readiness, latest_raw_artifact_id,
                latest_raw_content_hash, normalizer_version, first_usable_at,
                discovered_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'main_event', ?, 1, 0, 0, 0, 'complete',
                       'ready', 'ready', 'ready', 'ready', 'ready', ?, ?,
                       'opendota-exact-v1', ?, ?, ?)""",
            (
                match_id,
                EVENT_ID,
                int(started.timestamp()),
                20_000 + (sequence - 1) // 3,
                (sequence - 1) % 3 + 1,
                int(in_scope),
                artifact_id,
                content_hash,
                now,
                now,
                now,
            ),
        )
        radiant_win = sequence % 2 == 0
        self.connection.execute(
            """INSERT INTO matches
               (match_id, radiant_team_id, dire_team_id, radiant_win, duration,
                start_time, leagueid, series_id, patch)
               VALUES (?, 100, 200, ?, ?, ?, ?, ?, 59)""",
            (
                match_id,
                int(radiant_win),
                duration,
                int(started.timestamp()),
                self.league_id,
                20_000 + (sequence - 1) // 3,
            ),
        )
        heroes = tuple(range(1, 11))
        self.connection.executemany(
            "INSERT OR IGNORE INTO heroes(hero_id) VALUES (?)",
            ((hero_id,) for hero_id in heroes),
        )
        for index, hero_id in enumerate(heroes):
            radiant = index < 5
            player_slot = index if radiant else 128 + index - 5
            team_id = 100 if radiant else 200
            account_id = 1_000 + index
            self.connection.execute(
                """INSERT INTO match_players
                   (match_id, account_id, player_slot, hero_id, is_radiant, team_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (match_id, account_id, player_slot, hero_id, int(radiant), team_id),
            )
            self.connection.execute(
                """INSERT INTO picks_bans(match_id, hero_id, is_pick, team, ord)
                   VALUES (?, ?, 1, ?, ?)""",
                (match_id, hero_id, int(not radiant), index),
            )
            facts = _facts(hero_id)
            if sequence == 1 and index == 9:
                facts["stuns"] = -1
            self.connection.execute(
                """INSERT INTO player_map_facts
                   (match_id, player_slot, account_id, team_id, hero_id,
                    is_radiant, facts_json, missing_fields_json, coverage,
                    source_artifact_id, source_content_hash, fact_version,
                    first_usable_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '[]', 1.0, ?, ?, ?, ?, ?)""",
                (
                    match_id,
                    player_slot,
                    account_id,
                    team_id,
                    hero_id,
                    int(radiant),
                    json.dumps(facts, sort_keys=True),
                    artifact_id,
                    content_hash,
                    f"opendota-exact-v1:{content_hash}",
                    now,
                    now,
                ),
            )
            expected = index % 5 + 1
            observed = 6 - expected if observed_reverse else expected
            reconstructed_created = START + timedelta(days=365)
            for purpose, position, cutoff, created in (
                ("expected_position", expected, started, usable),
                (
                    "observed_position",
                    observed,
                    reconstructed_created,
                    reconstructed_created,
                ),
            ):
                for version in (
                    ASSIGNMENT_VERSION,
                    PROSPECTIVE_ASSIGNMENT_VERSION,
                ):
                    self.connection.execute(
                        """INSERT INTO player_role_assignments
                           (match_id, player_slot, account_id, team_id, purpose,
                            position, assignment_source, confidence, input_cutoff,
                            input_hash, assignment_version, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, 'historical_pattern', 0.9,
                                   ?, ?, ?, ?)""",
                        (
                            match_id,
                            player_slot,
                            account_id,
                            team_id,
                            purpose,
                            position,
                            cutoff.isoformat(),
                            _hash(
                                f"role:{match_id}:{player_slot}:{purpose}:{version}"
                            ),
                            version,
                            created.isoformat(),
                        ),
                    )
            self.connection.execute(
                """INSERT INTO player_map_scores
                   (match_id, player_slot, account_id, position, execution_score,
                    result_adjusted_score, component_facts_json,
                    component_scores_json, weights_json, coverage,
                    role_confidence, benchmark_cutoff, benchmark_hash,
                    input_hash, score_version, explanation_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, '{}', '{}', '{}', 1.0, 0.9,
                           ?, ?, ?, ?, '{}', ?)""",
                (
                    match_id,
                    player_slot,
                    account_id,
                    observed,
                    55.0 + index,
                    55.0 + index,
                    reconstructed_created.isoformat(),
                    _hash(f"benchmark:{match_id}:{player_slot}"),
                    _hash(f"score:{match_id}:{player_slot}"),
                    score_version_for_role(ASSIGNMENT_VERSION),
                    reconstructed_created.isoformat(),
                ),
            )
        self.connection.commit()
        return match_id


class DraftBacktestTests(unittest.TestCase):
    def _database(
        self,
        directory: str,
        *,
        count: int = 6,
        order: tuple[int, ...] | None = None,
        excluded: bool = False,
    ) -> Path:
        path = Path(directory) / "strict-draft.db"
        fixture = DraftBacktestFixture(path)
        try:
            sequence = order or tuple(range(1, count + 1))
            for value in sequence:
                fixture.add_map(value, observed_reverse=value == count)
            if excluded:
                fixture.add_map(99, in_scope=False)
        finally:
            fixture.close()
        return path

    def test_loader_is_strict_exact_and_never_uses_target_observed_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory, excluded=True)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                corpus = load_draft_corpus(
                    connection,
                    availability_mode=AvailabilityMode.RECONSTRUCTED,
                    assignment_version=ASSIGNMENT_VERSION,
                )
                prospective = load_draft_corpus(
                    connection,
                    availability_mode=AvailabilityMode.PROSPECTIVE,
                    assignment_version=PROSPECTIVE_ASSIGNMENT_VERSION,
                )
                with self.assertRaisesRegex(ValueError, "does not match"):
                    load_draft_corpus(
                        connection,
                        availability_mode=AvailabilityMode.PROSPECTIVE,
                        assignment_version=ASSIGNMENT_VERSION,
                    )
            finally:
                connection.close()

            self.assertEqual(corpus.formal_draft_maps, 6)
            self.assertEqual(len(corpus.maps), 6)
            self.assertEqual(len(corpus.targets), 6)
            self.assertEqual(len(prospective.targets), 0)
            self.assertIsNone(
                corpus.maps[0].evidence.dire_hero_evidence[-1].control_seconds
            )
            last = corpus.targets[-1]
            self.assertIsNotNone(last.target)
            self.assertEqual(
                [row.expected_position for row in last.target.radiant.players],
                [1, 2, 3, 4, 5],
            )
            self.assertEqual(
                [row.observed_position for row in last.evidence.radiant_hero_evidence],
                [5, 4, 3, 2, 1],
            )
            second = corpus.targets[1]
            snapshot = build_draft_feature_snapshot(
                second.target, tuple(row.evidence for row in corpus.maps)
            )
            self.assertGreater(snapshot.feature("role_fit_win_rate_diff").support, 0)
            self.assertGreater(snapshot.feature("context_player_form_diff").support, 0)

    def test_each_eligible_target_horizon_is_oos_and_modes_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            dry = run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                dry_run=True,
                min_samples=2,
            )
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM draft_model_runs").fetchone()[0],
                    0,
                )
            report = run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            prospective = run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.PROSPECTIVE,
                assignment_version=PROSPECTIVE_ASSIGNMENT_VERSION,
                min_samples=2,
            )

            self.assertEqual((dry.runs, report.runs), (60, 60))
            self.assertEqual(report.inserted_runs, 60)
            self.assertEqual(prospective.runs, 0)
            self.assertEqual(report.cold_start_support, 0)
            self.assertEqual(
                tuple(row.event_id for row in report.event_order),
                (
                    "pgl-wallachia-s8-2026",
                    "dreamleague-s29-2026",
                    "blast-slam-vii-2026",
                    "ewc-dota2-2026",
                ),
            )
            self.assertEqual(len(report.event_slices), 40)
            self.assertTrue(
                all(
                    row.event_id == "pgl-wallachia-s8-2026"
                    and row.eligible_targets == 6
                    for row in report.event_slices[:10]
                )
            )
            self.assertTrue(
                all(row.eligible_targets == 6 for row in report.slices)
            )
            with closing(sqlite3.connect(database)) as connection:
                rows = connection.execute(
                    """SELECT run.model_kind, run.horizon_minutes,
                              prediction.match_id, run.training_cutoff,
                              prediction.prediction_cutoff
                       FROM draft_model_runs AS run
                       JOIN draft_predictions AS prediction USING(run_id)"""
                ).fetchall()
            self.assertEqual(len(rows), 60)
            self.assertTrue(all(row[3] == row[4] for row in rows))
            self.assertEqual(len({(row[0], row[1], row[2]) for row in rows}), 60)

    def test_physical_future_row_shuffle_does_not_change_predictions(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = self._database(first_dir, order=(1, 2, 3, 4, 5, 6))
            second = self._database(second_dir, order=(6, 5, 4, 3, 2, 1))
            with closing(sqlite3.connect(second)) as connection:
                connection.execute(
                    "UPDATE matches SET radiant_win=1-radiant_win WHERE match_id IN (9005, 9006)"
                )
                connection.commit()
            for database in (first, second):
                run_strict_draft_backtest(
                    database,
                    availability_mode=AvailabilityMode.RECONSTRUCTED,
                    assignment_version=ASSIGNMENT_VERSION,
                    min_samples=2,
                )

            def predictions(path: Path) -> list[tuple[object, ...]]:
                with closing(sqlite3.connect(path)) as connection:
                    return connection.execute(
                        """SELECT prediction.match_id, run.model_kind,
                                  run.horizon_minutes, prediction.probability,
                                  prediction.input_snapshot_hash
                           FROM draft_predictions AS prediction
                           JOIN draft_model_runs AS run USING(run_id)
                           WHERE prediction.match_id <= 9004
                           ORDER BY prediction.match_id, run.model_kind,
                                    run.horizon_minutes"""
                    ).fetchall()

            self.assertEqual(predictions(first), predictions(second))

    def test_repeated_run_is_idempotent_and_conflict_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            first = run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            second = run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            self.assertEqual((first.inserted_runs, second.unchanged_runs), (60, 60))

            with closing(sqlite3.connect(database)) as connection:
                run_ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT run_id FROM draft_model_runs ORDER BY training_cutoff, run_id"
                    )
                ]
                first_run, last_run = run_ids[0], run_ids[-1]
                connection.execute(
                    "DELETE FROM draft_predictions WHERE run_id=?", (first_run,)
                )
                connection.execute(
                    "UPDATE draft_model_runs SET metrics_json='{}' WHERE run_id=?",
                    (last_run,),
                )
                connection.commit()

            with self.assertRaisesRegex(ValueError, "immutable draft run conflict"):
                run_strict_draft_backtest(
                    database,
                    availability_mode=AvailabilityMode.RECONSTRUCTED,
                    assignment_version=ASSIGNMENT_VERSION,
                    min_samples=2,
                )
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM draft_predictions WHERE run_id=?",
                        (first_run,),
                    ).fetchone()[0],
                    0,
                )

    def test_metrics_gate_and_series_bootstrap_are_deterministic(self) -> None:
        points = tuple(
            EvaluationPoint(
                match_id=index + 1,
                series_id=index // 5 + 1,
                event_id=EVENT_ID,
                probability=(
                    0.95 + index / 10_000 if index % 2 else 0.01 + index / 10_000
                ),
                outcome=bool(index % 2),
            )
            for index in range(100)
        )
        first = evaluate_points(points, seed_material="stable")
        second = evaluate_points(tuple(reversed(points)), seed_material="stable")

        self.assertEqual(first, second)
        self.assertEqual(first.gate_status, "passed")
        self.assertLess(first.brier_score, 0.25)
        self.assertLess(first.log_loss, 0.6932)
        self.assertLessEqual(first.ece_5_bin, 0.10)
        self.assertLessEqual(first.ece_90_upper, 0.15)
        unsupported = evaluate_points(points[:99], seed_material="stable")
        self.assertEqual(unsupported.gate_status, "unsupported")
        self.assertIn("support_below_100", unsupported.gate_failures)

        tied = tuple(
            EvaluationPoint(
                index + 1, index // 5 + 1, EVENT_ID, 0.5, bool(index % 2)
            )
            for index in range(100)
        )
        reassigned = tuple(
            EvaluationPoint(
                index + 1,
                index // 5 + 1,
                EVENT_ID,
                0.5,
                index % 5 < (2 if (index // 5) % 2 == 0 else 3),
            )
            for index in range(100)
        )
        self.assertEqual(
            evaluate_points(tied, seed_material="ties"),
            evaluate_points(reassigned, seed_material="ties"),
        )

    def test_persisted_state_projection_preserves_profile_opportunities(self) -> None:
        curve = [0] * 10 + [6_000] * 19
        original, _ = build_team_map_states(
            match_id=1,
            duration_seconds=31 * 60,
            radiant_win=True,
            radiant_team_id=100,
            dire_team_id=200,
            radiant_gold_adv=curve,
            objectives=None,
            source_versions={"opendota": _hash("state-source")},
        )
        persisted = {
            "label": original.label.value,
            "duration_seconds": original.duration_seconds,
            "max_lead": original.max_lead,
            "max_deficit": original.max_deficit,
            "ahead_fraction": original.ahead_fraction,
            "behind_fraction": original.behind_fraction,
            "even_fraction": original.even_fraction,
            "signed_auc": original.signed_auc,
            "absolute_auc": original.absolute_auc,
            "crossings_json": json.dumps([asdict(row) for row in original.crossings]),
            "first_significant_lead_at": original.first_significant_lead_at,
            "first_significant_deficit_at": original.first_significant_deficit_at,
            "closeout_seconds": original.closeout_seconds,
            "objective_conversion_json": json.dumps(
                asdict(original.objective_conversion)
            ),
            "curve_coverage": original.curve_coverage,
            "source_versions_json": json.dumps(original.source_versions),
            "input_hash": original.input_hash,
            "label_version": original.label_version,
        }
        projected = _profile_state(
            persisted,
            match_id=1,
            team_id=100,
            opponent_id=200,
            side=Side.RADIANT,
            won=True,
        )
        completed = START + timedelta(minutes=31)

        def profile(state: object):
            return build_team_style_profile(
                team_id=100,
                cutoff=completed + timedelta(days=1),
                maps=(
                    ProfileMap(
                        state=state,
                        completed_at=completed,
                        first_usable_at=completed,
                        event_id=EVENT_ID,
                        patch=59,
                        roster=(1, 2, 3, 4, 5),
                    ),
                ),
                target_roster=(1, 2, 3, 4, 5),
                target_patch=59,
                availability_mode=ProfileAvailabilityMode.RECONSTRUCTED,
            )

        actual = profile(original)
        rebuilt = profile(projected)
        self.assertEqual(actual.opportunity_counts, rebuilt.opportunity_counts)
        self.assertEqual(actual.posterior_rates, rebuilt.posterior_rates)
        self.assertEqual(actual.input_hash, rebuilt.input_hash)


if __name__ == "__main__":
    unittest.main()
