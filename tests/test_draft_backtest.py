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
from unittest.mock import patch

import event_intelligence.incremental as incremental
import event_intelligence.report as intelligence_report
from event_intelligence.backtest import (
    EvaluationPoint,
    _profile_state,
    draft_dependency_fingerprint,
    draft_lineage_tracking_is_current,
    draft_prediction_artifacts,
    ensure_draft_lineage_tracking,
    evaluate_points,
    load_draft_corpus,
    persist_draft_prediction_validations,
    run_strict_draft_backtest,
)
from event_intelligence.draft_features import (
    AvailabilityMode,
    build_draft_feature_snapshot,
)
from event_intelligence.incremental import (
    _current_draft_prediction_keys,
    refresh_draft_prediction_validations,
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

    def test_event_registry_writes_advance_draft_dependency_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory, count=1)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                ensure_draft_lineage_tracking(connection)

                def revision() -> int:
                    return int(
                        connection.execute(
                            """SELECT dependency_revision
                                 FROM draft_lineage_revisions WHERE singleton=1"""
                        ).fetchone()[0]
                    )

                source = list(
                    connection.execute(
                        "SELECT * FROM event_registry WHERE event_id=?", (EVENT_ID,)
                    ).fetchone()
                )
                source[0] = "draft-lineage-test-event"
                source[1] = "Draft Lineage Test Event"
                source[6] = 99_999_999
                placeholders = ", ".join("?" for _ in source)

                before = revision()
                connection.execute(
                    f"INSERT INTO event_registry VALUES ({placeholders})",  # noqa: S608
                    source,
                )
                after_insert = revision()
                connection.execute(
                    """UPDATE event_registry SET canonical_name=?
                         WHERE event_id='draft-lineage-test-event'""",
                    ("Renamed Draft Lineage Test Event",),
                )
                after_update = revision()
                connection.execute(
                    """DELETE FROM event_registry
                         WHERE event_id='draft-lineage-test-event'"""
                )
                after_delete = revision()
            finally:
                connection.close()

            self.assertGreater(after_insert, before)
            self.assertGreater(after_update, after_insert)
            self.assertGreater(after_delete, after_update)

    def test_event_without_maps_does_not_invalidate_existing_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                source = dict(
                    connection.execute(
                        "SELECT * FROM event_registry WHERE event_id=?", (EVENT_ID,)
                    ).fetchone()
                )
                source.update(
                    {
                        "event_id": "historical-empty-event",
                        "canonical_name": "Historical Empty Event",
                        "main_event_start_at": "2020-01-01T00:00:00+00:00",
                        "main_event_end_at": "2020-01-02T00:00:00+00:00",
                        "opendota_league_id": 99_999_998,
                    }
                )
                columns = tuple(source)
                connection.execute(
                    f"INSERT INTO event_registry ({', '.join(columns)}) "  # noqa: S608
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    tuple(source[column] for column in columns),
                )
                latest_change = int(
                    connection.execute(
                        """SELECT affected_from_unix FROM draft_lineage_changes
                            ORDER BY dependency_revision DESC LIMIT 1"""
                    ).fetchone()[0]
                )
                current = _current_draft_prediction_keys(connection, match_ids)
            finally:
                connection.close()

            self.assertGreater(latest_change, 2**62)
            self.assertEqual(len(current), 60)

    def test_validation_persist_requires_generation_dependency_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory, count=1)
            with closing(sqlite3.connect(database)) as connection:
                with self.assertRaises(TypeError):
                    persist_draft_prediction_validations(connection, ())
                with self.assertRaisesRegex(ValueError, "fingerprint"):
                    persist_draft_prediction_validations(
                        connection,
                        (),
                        expected_dependency_fingerprint=None,  # type: ignore[arg-type]
                        expected_dependency_revision=None,  # type: ignore[arg-type]
                    )

    def test_lineage_tracking_rejects_and_repairs_same_name_noop_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory, count=1)
            connection = sqlite3.connect(database)
            try:
                ensure_draft_lineage_tracking(connection)
                trigger_name = "draft_lineage_dependency_event_registry_update"
                before = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )
                connection.execute(f'DROP TRIGGER "{trigger_name}"')
                connection.execute(
                    f'''CREATE TRIGGER "{trigger_name}"
                          AFTER UPDATE ON event_registry
                          BEGIN SELECT 1; END'''
                )

                self.assertFalse(draft_lineage_tracking_is_current(connection))
                ensure_draft_lineage_tracking(connection)
                repaired = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )
                self.assertTrue(draft_lineage_tracking_is_current(connection))
                self.assertGreater(repaired, before)

                connection.execute(
                    """UPDATE event_registry SET prize_pool_usd=prize_pool_usd+1
                         WHERE event_id=?""",
                    (EVENT_ID,),
                )
                self.assertGreater(
                    int(
                        connection.execute(
                            """SELECT dependency_revision
                                 FROM draft_lineage_revisions WHERE singleton=1"""
                        ).fetchone()[0]
                    ),
                    repaired,
                )
            finally:
                connection.close()

    def test_lineage_journal_rejects_insert_or_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                revision = int(
                    connection.execute(
                        "SELECT MAX(dependency_revision) FROM draft_lineage_changes"
                    ).fetchone()[0]
                )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    connection.execute(
                        """INSERT OR REPLACE INTO draft_lineage_changes
                           (dependency_revision, affected_from_unix,
                            source_relation, operation, changed_at)
                           VALUES (?, ?, 'attack', 'UPDATE', '2099-01-01')""",
                        (revision, 4_102_444_800),
                    )
                connection.rollback()
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                tracking_current = draft_lineage_tracking_is_current(connection)
                current = _current_draft_prediction_keys(connection, match_ids)
            finally:
                connection.close()

            self.assertTrue(tracking_current)
            self.assertEqual(len(current), 60)

    def test_schema_init_is_idempotent_after_lineage_guards_are_installed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            with IntelligenceStorage(database) as storage:
                storage.init_schema(seed_events=False)
            connection = sqlite3.connect(database)
            try:
                tracking_current = draft_lineage_tracking_is_current(connection)
                validation_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM draft_prediction_validations"
                    ).fetchone()[0]
                )
            finally:
                connection.close()

            self.assertTrue(tracking_current)
            self.assertEqual(validation_count, 60)

    def test_lineage_journal_gap_fails_closed_and_clears_proofs_on_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                connection.execute(
                    'DROP TRIGGER "draft_lineage_changes_no_delete"'
                )
                connection.execute(
                    """DELETE FROM draft_lineage_changes
                        WHERE dependency_revision=(
                            SELECT MAX(dependency_revision)
                              FROM draft_lineage_changes
                        )"""
                )
                connection.commit()
                self.assertFalse(draft_lineage_tracking_is_current(connection))
                self.assertEqual(
                    _current_draft_prediction_keys(connection, match_ids),
                    frozenset(),
                )
                ensure_draft_lineage_tracking(connection)
                validations = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM draft_prediction_validations"
                    ).fetchone()[0]
                )
            finally:
                connection.close()

            self.assertEqual(validations, 0)

    def test_missing_revision_row_invalidates_existing_draft_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                self.assertGreater(
                    connection.execute(
                        "SELECT COUNT(*) FROM draft_prediction_validations"
                    ).fetchone()[0],
                    0,
                )
                connection.execute("DELETE FROM draft_lineage_revisions")
                connection.commit()

                self.assertFalse(draft_lineage_tracking_is_current(connection))
                ensure_draft_lineage_tracking(connection)
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM draft_prediction_validations"
                ).fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(remaining, 0)

    def test_malformed_revision_table_is_rebuilt_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                proof_revision = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_prediction_validations LIMIT 1"""
                    ).fetchone()[0]
                )
                artifact_revision = int(
                    connection.execute(
                        """SELECT artifact_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )
                connection.execute("DROP TABLE draft_lineage_revisions")
                connection.execute(
                    """CREATE TABLE draft_lineage_revisions (
                        singleton INTEGER,
                        dependency_revision INTEGER,
                        artifact_revision INTEGER,
                        updated_at TEXT
                    )"""
                )
                connection.executemany(
                    "INSERT INTO draft_lineage_revisions VALUES (1, ?, ?, ?)",
                    (
                        (proof_revision, artifact_revision, "2026-01-01"),
                        (proof_revision + 100, artifact_revision + 100, "2026-01-02"),
                    ),
                )
                connection.commit()

                self.assertFalse(draft_lineage_tracking_is_current(connection))
                self.assertEqual(
                    _current_draft_prediction_keys(connection, match_ids),
                    frozenset(),
                )
                ensure_draft_lineage_tracking(connection)
                rows = connection.execute(
                    """SELECT singleton, dependency_revision, artifact_revision
                         FROM draft_lineage_revisions"""
                ).fetchall()
                validations = connection.execute(
                    "SELECT COUNT(*) FROM draft_prediction_validations"
                ).fetchone()[0]
                tracking_repaired = draft_lineage_tracking_is_current(connection)
            finally:
                connection.close()

            self.assertTrue(tracking_repaired)
            self.assertEqual(len(rows), 1)
            self.assertEqual(int(rows[0][0]), 1)
            self.assertGreaterEqual(int(rows[0][1]), 1)
            self.assertGreaterEqual(int(rows[0][2]), 1)
            self.assertEqual(validations, 0)

    def test_noncausal_and_nonready_status_writes_do_not_advance_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory, count=1)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                ensure_draft_lineage_tracking(connection)

                def revision() -> int:
                    return int(
                        connection.execute(
                            """SELECT dependency_revision
                                 FROM draft_lineage_revisions WHERE singleton=1"""
                        ).fetchone()[0]
                    )

                before_revision = revision()
                before_fingerprint = draft_dependency_fingerprint(connection)
                connection.execute(
                    """UPDATE match_ingest_status
                          SET retry_count=retry_count+1,
                              updated_at='2026-12-31T00:00:00+00:00'
                        WHERE match_id=9001"""
                )
                self.assertEqual(revision(), before_revision)
                self.assertEqual(
                    draft_dependency_fingerprint(connection), before_fingerprint
                )

                source = dict(
                    connection.execute(
                        "SELECT * FROM match_ingest_status WHERE match_id=9001"
                    ).fetchone()
                )
                source["match_id"] = 99_999
                source["draft_readiness"] = "pending"
                columns = tuple(source)
                quoted = ", ".join(f'"{column}"' for column in columns)
                placeholders = ", ".join("?" for _ in columns)
                connection.execute(
                    f"INSERT INTO match_ingest_status ({quoted}) "  # noqa: S608
                    f"VALUES ({placeholders})",
                    tuple(source[column] for column in columns),
                )
                connection.execute(
                    """UPDATE match_ingest_status SET series_id=series_id+1
                         WHERE match_id=99999"""
                )
                connection.execute(
                    "DELETE FROM match_ingest_status WHERE match_id=99999"
                )
                self.assertEqual(revision(), before_revision)
                self.assertEqual(
                    draft_dependency_fingerprint(connection), before_fingerprint
                )

                connection.execute(
                    """UPDATE match_ingest_status SET series_id=series_id+1
                         WHERE match_id=9001"""
                )
                self.assertGreater(revision(), before_revision)
                self.assertNotEqual(
                    draft_dependency_fingerprint(connection), before_fingerprint
                )
            finally:
                connection.close()

    def test_player_fact_availability_change_advances_dependency_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory, count=1)
            connection = sqlite3.connect(database)
            try:
                ensure_draft_lineage_tracking(connection)
                before_revision = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )
                before_fingerprint = draft_dependency_fingerprint(connection)
                connection.execute(
                    """UPDATE player_map_facts
                          SET first_usable_at='2027-01-01T00:00:00+00:00'
                        WHERE match_id=9001 AND player_slot=0"""
                )
                after_revision = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )
                after_fingerprint = draft_dependency_fingerprint(connection)
            finally:
                connection.close()

            self.assertGreater(after_revision, before_revision)
            self.assertNotEqual(after_fingerprint, before_fingerprint)

    def test_drifted_formal_views_fail_closed_and_are_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                self.assertEqual(
                    len(_current_draft_prediction_keys(connection, match_ids)), 60
                )
                view_sql = {
                    str(row[0]): str(row[1])
                    for row in connection.execute(
                        """SELECT name, sql FROM sqlite_master
                             WHERE type='view' AND name IN
                                   ('formal_events', 'formal_map_eligibility')"""
                    ).fetchall()
                }
                before_revision = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )
                connection.execute("DROP VIEW formal_map_eligibility")
                connection.execute(
                    view_sql["formal_map_eligibility"] + " AND m.match_id != 9001"
                )
                connection.commit()

                self.assertFalse(draft_lineage_tracking_is_current(connection))
                self.assertEqual(
                    _current_draft_prediction_keys(connection, match_ids),
                    frozenset(),
                )
                ensure_draft_lineage_tracking(connection)
                repaired_revision = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )
                self.assertTrue(draft_lineage_tracking_is_current(connection))
                self.assertGreater(repaired_revision, before_revision)
                self.assertEqual(
                    _current_draft_prediction_keys(connection, match_ids),
                    frozenset(),
                )

                connection.execute("DROP VIEW formal_map_eligibility")
                connection.execute("DROP VIEW formal_events")
                connection.execute(
                    view_sql["formal_events"].replace("'approved'", "'APPROVED'")
                )
                connection.execute(view_sql["formal_map_eligibility"])
                connection.commit()
                self.assertFalse(draft_lineage_tracking_is_current(connection))
                ensure_draft_lineage_tracking(connection)
                self.assertTrue(draft_lineage_tracking_is_current(connection))
                self.assertGreater(
                    int(
                        connection.execute(
                            """SELECT dependency_revision
                                 FROM draft_lineage_revisions WHERE singleton=1"""
                        ).fetchone()[0]
                    ),
                    repaired_revision,
                )
            finally:
                connection.close()

    def test_status_referenced_raw_rows_do_not_require_row_match_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory, count=1)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                ensure_draft_lineage_tracking(connection)
                original = connection.execute(
                    """SELECT latest_raw_artifact_id, latest_raw_content_hash
                         FROM match_ingest_status WHERE match_id=9001"""
                ).fetchone()
                content_hash = _hash("status-referenced-null-match")
                artifact_id = f"opendota:{content_hash}"
                connection.execute(
                    """INSERT INTO raw_source_artifacts
                       (artifact_id, content_hash, source, artifact_use, endpoint,
                        sanitized_request_identity, storage_path, uncompressed_bytes,
                        compressed_bytes, received_at, first_usable_at,
                        schema_fingerprint, event_id, match_id, created_at)
                       SELECT ?, ?, source, artifact_use, endpoint,
                              sanitized_request_identity, storage_path || '.null-match',
                              uncompressed_bytes, compressed_bytes, received_at,
                              first_usable_at, schema_fingerprint, event_id, NULL,
                              created_at
                         FROM raw_source_artifacts WHERE artifact_id=?""",
                    (artifact_id, content_hash, original["latest_raw_artifact_id"]),
                )
                connection.execute(
                    """UPDATE match_ingest_status
                          SET latest_raw_artifact_id=?, latest_raw_content_hash=?
                        WHERE match_id=9001""",
                    (artifact_id, content_hash),
                )
                before_fingerprint = draft_dependency_fingerprint(connection)
                before_revision = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )

                connection.execute(
                    """UPDATE raw_source_artifacts SET endpoint=endpoint || '?changed=1'
                         WHERE artifact_id=?""",
                    (artifact_id,),
                )
                endpoint_fingerprint = draft_dependency_fingerprint(connection)
                endpoint_revision = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )
                connection.execute(
                    """UPDATE raw_source_artifacts
                          SET first_usable_at='2026-01-02T00:00:00+00:00'
                        WHERE artifact_id=?""",
                    (artifact_id,),
                )
                artifact_fingerprint = draft_dependency_fingerprint(connection)
                artifact_revision = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )
                connection.execute(
                    """INSERT INTO raw_source_observations
                       (observation_id, artifact_id, content_hash, source,
                        artifact_use, endpoint, sanitized_request_identity,
                        received_at, first_usable_at, schema_fingerprint,
                        event_id, match_id, created_at)
                       VALUES ('null-match-observation', ?, ?, 'opendota',
                               'primary', '/api/null-match', 'GET /api/null-match',
                               '2026-01-01T00:00:00+00:00',
                               '2026-01-01T00:00:00+00:00', 'test-schema',
                               ?, NULL, '2026-01-01T00:00:00+00:00')""",
                    (artifact_id, content_hash, EVENT_ID),
                )
                observation_fingerprint = draft_dependency_fingerprint(connection)
                observation_revision = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )
            finally:
                connection.close()

            self.assertEqual(endpoint_fingerprint, before_fingerprint)
            self.assertEqual(endpoint_revision, before_revision)
            self.assertNotEqual(artifact_fingerprint, endpoint_fingerprint)
            self.assertGreater(artifact_revision, endpoint_revision)
            self.assertNotEqual(observation_fingerprint, artifact_fingerprint)
            self.assertGreater(observation_revision, artifact_revision)

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

    def test_current_prediction_keys_rebuild_input_snapshot_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                current = _current_draft_prediction_keys(connection, match_ids)
                self.assertEqual(len(current), 60)
                initial_revisions = tuple(
                    connection.execute(
                        """SELECT dependency_revision, artifact_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()
                )

                source = connection.execute(
                    """SELECT prediction.run_id, prediction.match_id
                         FROM draft_predictions AS prediction LIMIT 1"""
                ).fetchone()
                connection.execute(
                    """INSERT INTO draft_model_runs
                       SELECT 'unvalidated-run', model_version, model_kind,
                              horizon_minutes, availability_mode, training_cutoff,
                              feature_schema_hash, configuration_json, metrics_json,
                              status, created_at
                         FROM draft_model_runs WHERE run_id=?""",
                    (source["run_id"],),
                )
                connection.execute(
                    """INSERT INTO draft_predictions
                       (run_id, match_id, prediction_cutoff, cutoff_source,
                        input_snapshot_hash, probability, uncertainty, support,
                        eventual_radiant_win, status, created_at)
                       SELECT 'unvalidated-run', match_id, prediction_cutoff,
                              cutoff_source, input_snapshot_hash, probability,
                              uncertainty, support, eventual_radiant_win, status,
                              created_at
                         FROM draft_predictions
                        WHERE run_id=? AND match_id=?""",
                    (source["run_id"], source["match_id"]),
                )
                self.assertEqual(
                    len(_current_draft_prediction_keys(connection, match_ids)),
                    60,
                )
                cloned_revisions = tuple(
                    connection.execute(
                        """SELECT dependency_revision, artifact_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()
                )
                self.assertEqual(cloned_revisions[0], initial_revisions[0])
                self.assertGreater(cloned_revisions[1], initial_revisions[1])

                victim = connection.execute(
                    """SELECT run_id, match_id FROM draft_predictions
                         WHERE probability IS NOT NULL AND run_id!='unvalidated-run'
                           AND match_id!=9006
                         LIMIT 1"""
                ).fetchone()
                connection.execute(
                    """UPDATE draft_predictions SET probability=probability
                        WHERE run_id=? AND match_id=?""",
                    (victim["run_id"], victim["match_id"]),
                )
                self.assertEqual(
                    connection.execute(
                        """SELECT artifact_revision FROM draft_lineage_revisions
                             WHERE singleton=1"""
                    ).fetchone()[0],
                    cloned_revisions[1],
                )
                connection.execute(
                    """UPDATE draft_predictions
                          SET probability=CASE WHEN probability < 0.5
                                               THEN probability + 0.01
                                               ELSE probability - 0.01 END
                        WHERE run_id=? AND match_id=?""",
                    (victim["run_id"], victim["match_id"]),
                )
                self.assertEqual(
                    len(_current_draft_prediction_keys(connection, match_ids)),
                    59,
                )

                connection.execute(
                    """UPDATE player_role_assignments SET input_hash=?
                         WHERE match_id=9006 AND purpose='expected_position'
                           AND assignment_version=?""",
                    (_hash("changed-target-role"), ASSIGNMENT_VERSION),
                )
                self.assertGreater(
                    connection.execute(
                        """SELECT dependency_revision FROM draft_lineage_revisions
                             WHERE singleton=1"""
                    ).fetchone()[0],
                    initial_revisions[0],
                )
                changed = _current_draft_prediction_keys(connection, match_ids)
                connection.commit()
                rebuilt = refresh_draft_prediction_validations(
                    connection, match_ids
                )
                published = _current_draft_prediction_keys(connection, match_ids)
            finally:
                connection.close()

            self.assertEqual(len(changed), 49)
            self.assertFalse(any(match_id == 9006 for _, match_id in changed))
            self.assertNotIn(
                (str(victim["run_id"]), int(victim["match_id"])), changed
            )
            self.assertEqual(rebuilt, published)
            self.assertEqual(len(published), 49)
            self.assertFalse(any(match_id == 9006 for _, match_id in published))
            self.assertNotIn(
                (str(victim["run_id"]), int(victim["match_id"])), published
            )

    def test_future_formal_map_does_not_invalidate_earlier_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            with closing(sqlite3.connect(database)) as connection:
                proof_revision = int(
                    connection.execute(
                        """SELECT MIN(dependency_revision)
                             FROM draft_prediction_validations"""
                    ).fetchone()[0]
                )
                before_fingerprint = draft_dependency_fingerprint(connection)

            fixture = DraftBacktestFixture(database)
            try:
                future_match_id = fixture.add_map(7)
            finally:
                fixture.close()

            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                current_revision = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )
                changes = connection.execute(
                    """SELECT affected_from_unix FROM draft_lineage_changes
                        WHERE dependency_revision>?""",
                    (proof_revision,),
                ).fetchall()
                after_fingerprint = draft_dependency_fingerprint(connection)
                current = _current_draft_prediction_keys(connection, match_ids)
            finally:
                connection.close()

            self.assertIn(future_match_id, match_ids)
            self.assertGreater(current_revision, proof_revision)
            self.assertNotEqual(before_fingerprint, after_fingerprint)
            self.assertTrue(changes)
            self.assertTrue(all(row[0] is not None for row in changes))
            self.assertEqual(len(current), 60)

    def test_change_before_prediction_cutoff_invalidates_affected_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                connection.execute(
                    """UPDATE player_role_assignments SET input_hash=?
                         WHERE match_id=9001 AND purpose='expected_position'
                           AND assignment_version=?""",
                    (_hash("changed-earliest-target-role"), ASSIGNMENT_VERSION),
                )
                current = _current_draft_prediction_keys(connection, match_ids)
            finally:
                connection.close()

            self.assertEqual(current, frozenset())

    def test_persisted_cutoff_before_map_start_is_used_for_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                victim = connection.execute(
                    """SELECT run_id, match_id FROM draft_predictions
                         WHERE match_id=9006 LIMIT 1"""
                ).fetchone()
                started_at = int(
                    connection.execute(
                        "SELECT start_time FROM matches WHERE match_id=9006"
                    ).fetchone()[0]
                )
                earlier_cutoff = datetime.fromtimestamp(
                    started_at - 3600, UTC
                ).isoformat()
                connection.execute(
                    """UPDATE draft_predictions SET prediction_cutoff=?
                         WHERE run_id=? AND match_id=?""",
                    (earlier_cutoff, victim["run_id"], victim["match_id"]),
                )
                connection.execute(
                    "UPDATE draft_model_runs SET training_cutoff=? WHERE run_id=?",
                    (earlier_cutoff, victim["run_id"]),
                )
                artifact = draft_prediction_artifacts(connection)[
                    (str(victim["run_id"]), int(victim["match_id"]))
                ]
                connection.execute(
                    """UPDATE draft_prediction_validations
                          SET input_snapshot_hash=?, artifact_fingerprint=?
                        WHERE run_id=? AND match_id=?""",
                    (*artifact, victim["run_id"], victim["match_id"]),
                )
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                self.assertIn(
                    (str(victim["run_id"]), int(victim["match_id"])),
                    _current_draft_prediction_keys(connection, match_ids),
                )

                connection.execute(
                    """UPDATE player_role_assignments SET input_hash=?
                         WHERE match_id=9006 AND purpose='expected_position'
                           AND assignment_version=?""",
                    (_hash("changed-before-recorded-draft"), ASSIGNMENT_VERSION),
                )
                current = _current_draft_prediction_keys(connection, match_ids)
                latest_change = connection.execute(
                    """SELECT affected_from_unix FROM draft_lineage_changes
                        ORDER BY dependency_revision DESC LIMIT 1"""
                ).fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(
                latest_change,
                int(datetime.fromisoformat(earlier_cutoff).timestamp()),
            )
            self.assertNotIn(
                (str(victim["run_id"]), int(victim["match_id"])), current
            )

    def test_partial_validation_refresh_preserves_other_match_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                refreshed = refresh_draft_prediction_validations(connection, {9006})
                validation_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM draft_prediction_validations"
                    ).fetchone()[0]
                )
            finally:
                connection.close()

            self.assertEqual(len(refreshed), 10)
            self.assertEqual(validation_count, 60)

    def test_refresh_rejects_artifact_change_between_rebuild_and_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                victim = connection.execute(
                    """SELECT run_id, match_id FROM draft_predictions
                         WHERE probability IS NOT NULL LIMIT 1"""
                ).fetchone()
                original_rebuild = incremental._rebuild_current_draft_prediction_keys

                def rebuild_then_mutate(
                    active: sqlite3.Connection,
                    candidates: set[int],
                ) -> frozenset[tuple[str, int]]:
                    rebuilt = original_rebuild(active, candidates)
                    with closing(sqlite3.connect(database, timeout=10)) as writer:
                        writer.execute(
                            """UPDATE draft_predictions
                                  SET probability=CASE WHEN probability < 0.5
                                                       THEN probability + 0.01
                                                       ELSE probability - 0.01 END
                                WHERE run_id=? AND match_id=?""",
                            (victim["run_id"], victim["match_id"]),
                        )
                        writer.commit()
                    return rebuilt

                with patch(
                    "event_intelligence.incremental._rebuild_current_draft_prediction_keys",
                    side_effect=rebuild_then_mutate,
                ):
                    with self.assertRaisesRegex(RuntimeError, "artifacts changed"):
                        refresh_draft_prediction_validations(connection, match_ids)

                published = _current_draft_prediction_keys(connection, match_ids)
            finally:
                connection.close()

            self.assertNotIn(
                (str(victim["run_id"]), int(victim["match_id"])), published
            )

    def test_refresh_allows_only_future_dependency_change_during_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                original_rebuild = incremental._rebuild_current_draft_prediction_keys

                def rebuild_then_add_future(
                    active: sqlite3.Connection,
                    candidates: set[int],
                ) -> frozenset[tuple[str, int]]:
                    rebuilt = original_rebuild(active, candidates)
                    fixture = DraftBacktestFixture(database)
                    try:
                        fixture.add_map(7)
                    finally:
                        fixture.close()
                    return rebuilt

                with patch(
                    "event_intelligence.incremental._rebuild_current_draft_prediction_keys",
                    side_effect=rebuild_then_add_future,
                ):
                    refreshed = refresh_draft_prediction_validations(
                        connection, match_ids
                    )
                current_revision = int(
                    connection.execute(
                        """SELECT dependency_revision
                             FROM draft_lineage_revisions WHERE singleton=1"""
                    ).fetchone()[0]
                )
                proof_revisions = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT dependency_revision FROM draft_prediction_validations"
                    ).fetchall()
                }
            finally:
                connection.close()

            self.assertEqual(len(refreshed), 60)
            self.assertEqual(proof_revisions, {current_revision})

    def test_refresh_rejects_historical_dependency_change_during_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                original_rebuild = incremental._rebuild_current_draft_prediction_keys

                def rebuild_then_change_history(
                    active: sqlite3.Connection,
                    candidates: set[int],
                ) -> frozenset[tuple[str, int]]:
                    rebuilt = original_rebuild(active, candidates)
                    with closing(sqlite3.connect(database, timeout=10)) as writer:
                        writer.execute(
                            """UPDATE player_role_assignments SET input_hash=?
                                 WHERE match_id=9006
                                   AND purpose='expected_position'
                                   AND assignment_version=?""",
                            (_hash("concurrent-history-change"), ASSIGNMENT_VERSION),
                        )
                        writer.commit()
                    return rebuilt

                with patch(
                    "event_intelligence.incremental._rebuild_current_draft_prediction_keys",
                    side_effect=rebuild_then_change_history,
                ):
                    with self.assertRaisesRegex(RuntimeError, "dependencies changed"):
                        refresh_draft_prediction_validations(connection, match_ids)
            finally:
                connection.close()

    def test_rolled_back_artifact_revision_does_not_authenticate_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }
                rows = connection.execute(
                    """SELECT run_id, match_id FROM draft_predictions
                         WHERE probability IS NOT NULL LIMIT 2"""
                ).fetchall()
                victim, other = rows

                connection.execute(
                    """UPDATE draft_predictions
                          SET probability=CASE WHEN probability < 0.5
                                               THEN probability + 0.01
                                               ELSE probability - 0.01 END
                        WHERE run_id=? AND match_id=?""",
                    (other["run_id"], other["match_id"]),
                )
                _current_draft_prediction_keys(connection, match_ids)
                connection.rollback()

                connection.execute(
                    """UPDATE draft_predictions
                          SET probability=CASE WHEN probability < 0.5
                                               THEN probability + 0.02
                                               ELSE probability - 0.02 END
                        WHERE run_id=? AND match_id=?""",
                    (victim["run_id"], victim["match_id"]),
                )
                current = _current_draft_prediction_keys(connection, match_ids)
                connection.rollback()
            finally:
                connection.close()

            self.assertNotIn(
                (str(victim["run_id"]), int(victim["match_id"])), current
            )

    def test_intelligence_report_reads_proof_and_prediction_in_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            run_strict_draft_backtest(
                database,
                availability_mode=AvailabilityMode.RECONSTRUCTED,
                assignment_version=ASSIGNMENT_VERSION,
                min_samples=2,
            )
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                victim = connection.execute(
                    """SELECT run_id, match_id, probability
                         FROM draft_predictions
                         WHERE probability IS NOT NULL LIMIT 1"""
                ).fetchone()
                original_probability = float(victim["probability"])
                changed_probability = (
                    original_probability + 0.01
                    if original_probability < 0.5
                    else original_probability - 0.01
                )
                original_draft_rows = intelligence_report._draft_rows
                observed: list[float] = []
                match_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT match_id FROM formal_map_eligibility"
                    ).fetchall()
                }

                def current_draft_scopes(
                    active: sqlite3.Connection,
                ) -> incremental.CurrentDerivedScopes:
                    keys = _current_draft_prediction_keys(active, match_ids)
                    return incremental.CurrentDerivedScopes(
                        available=True,
                        formal=frozenset(match_ids),
                        current=frozenset(match_ids),
                        draft=frozenset(match_id for _, match_id in keys),
                        draft_predictions=keys,
                    )

                def mutate_then_read(
                    active: sqlite3.Connection,
                    prediction_keys: set[tuple[str, int]],
                ) -> list[dict[str, object]]:
                    with closing(sqlite3.connect(database, timeout=10)) as writer:
                        writer.execute(
                            """UPDATE draft_predictions SET probability=?
                                 WHERE run_id=? AND match_id=?""",
                            (
                                changed_probability,
                                victim["run_id"],
                                victim["match_id"],
                            ),
                        )
                        writer.commit()
                    rows = original_draft_rows(active, prediction_keys)
                    observed.extend(
                        float(row["probability"])
                        for row in rows
                        if row["run_id"] == victim["run_id"]
                        and row["match_id"] == victim["match_id"]
                    )
                    return rows

                with patch(
                    "event_intelligence.report.current_derived_scopes",
                    side_effect=current_draft_scopes,
                ), patch(
                    "event_intelligence.report._draft_rows",
                    side_effect=mutate_then_read,
                ):
                    intelligence_report.build_intelligence_report(connection)
            finally:
                connection.close()

            self.assertEqual(observed, [original_probability])
            with closing(sqlite3.connect(database)) as verification:
                self.assertEqual(
                    float(
                        verification.execute(
                            """SELECT probability FROM draft_predictions
                                 WHERE run_id=? AND match_id=?""",
                            (victim["run_id"], victim["match_id"]),
                        ).fetchone()[0]
                    ),
                    changed_probability,
                )

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
