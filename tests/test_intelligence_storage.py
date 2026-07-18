from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

from event_intelligence.models import (
    ArtifactProvenance,
    ArtifactSource,
    ArtifactUse,
    ComponentReadiness,
    EventScope,
    IngestState,
    ReconciliationStatus,
    RolePurpose,
)
from event_intelligence.storage import CURRENT_SCHEMA_VERSION, IntelligenceStorage


EXPECTED_TABLES = {
    "event_registry",
    "event_candidates",
    "raw_source_artifacts",
    "raw_source_artifact_relocations",
    "raw_source_observations",
    "match_ingest_status",
    "player_role_assignments",
    "player_map_facts",
    "player_map_scores",
    "team_map_states",
    "team_style_profiles",
    "draft_model_runs",
    "draft_predictions",
    "draft_lineage_changes",
    "draft_lineage_revisions",
    "draft_prediction_validations",
    "notification_outbox",
    "service_health",
    "ingest_scheduler_retry_state",
    "strict_derived_status",
}


class IntelligenceStorageTests(unittest.TestCase):
    def test_every_connection_enables_safety_pragmas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligence.db"
            with IntelligenceStorage(path) as storage:
                storage.init_schema()
                self.assertEqual(storage.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(storage.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(storage.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

            with IntelligenceStorage(path) as reopened:
                self.assertEqual(reopened.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(reopened.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_additive_schema_contains_all_approved_logical_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with IntelligenceStorage(Path(directory) / "intelligence.db") as storage:
                storage.init_schema()
                tables = {
                    row[0]
                    for row in storage.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(EXPECTED_TABLES <= tables)
                self.assertIsNotNone(
                    storage.connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='formal_map_eligibility'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    storage.connection.execute(
                        """SELECT 1 FROM sqlite_master
                             WHERE type='index'
                               AND name='idx_draft_predictions_match'"""
                    ).fetchone()
                )

    def test_foreign_keys_and_checked_states_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with IntelligenceStorage(Path(directory) / "intelligence.db") as storage:
                storage.init_schema()
                with self.assertRaises(sqlite3.IntegrityError):
                    storage.connection.execute(
                        """INSERT INTO match_ingest_status
                        (match_id, event_id, stage_scope, stage_in_scope,
                         has_valid_result, is_exhibition, is_forfeit, is_void_remake,
                         discovered_at, updated_at)
                        VALUES (1, 'missing', 'main_event', 1, 1, 0, 0, 0, ?, ?)""",
                        ("2026-07-13T00:00:00+00:00", "2026-07-13T00:00:00+00:00"),
                    )
                storage.connection.rollback()

                with self.assertRaises(sqlite3.IntegrityError):
                    storage.connection.execute(
                        "UPDATE event_registry SET prize_pool_usd=999999 WHERE opendota_league_id=19543"
                    )
                storage.connection.rollback()

    def test_transaction_rolls_back_as_one_unit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with IntelligenceStorage(Path(directory) / "intelligence.db") as storage:
                storage.init_schema()
                with self.assertRaisesRegex(RuntimeError, "rollback"):
                    with storage.transaction():
                        storage.execute(
                            """INSERT INTO event_candidates
                            (source, provider_event_id, canonical_name, evidence_urls_json,
                             evidence_status, audit_status, discovered_at, last_seen_at)
                            VALUES ('opendota', '1', 'candidate', '[]', 'unverified',
                                    'pending', '2026-07-13', '2026-07-13')"""
                        )
                        raise RuntimeError("rollback")
                self.assertEqual(
                    storage.connection.execute("SELECT COUNT(*) FROM event_candidates").fetchone()[0],
                    0,
                )

    def test_public_models_are_typed_and_frozen(self) -> None:
        self.assertEqual(EventScope.FORMAL_MAIN_EVENT.value, "formal_main_event")
        self.assertEqual(IngestState.DISCOVERED.value, "discovered")
        self.assertEqual(ComponentReadiness.RETRYABLE.value, "retryable")
        self.assertEqual(RolePurpose.EXPECTED_POSITION.value, "expected_position")
        self.assertEqual(ReconciliationStatus.PENDING.value, "reconciliation_pending")

        provenance = ArtifactProvenance(
            source=ArtifactSource.OPENDOTA,
            use=ArtifactUse.PRIMARY,
            endpoint="/api/matches/1",
            request_identity="GET /api/matches/1",
            source_at=None,
            received_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
            first_usable_at=None,
            schema_fingerprint="schema-v1",
            event_id="event",
            match_id=1,
        )
        with self.assertRaises(FrozenInstanceError):
            provenance.match_id = 2  # type: ignore[misc]

    def test_schema_upgrade_records_version_only_after_atomic_seed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligence.db"
            with IntelligenceStorage(path) as storage:
                storage.init_schema()
                self.assertEqual(
                    storage.connection.execute(
                        "SELECT MAX(version) FROM intelligence_schema_version"
                    ).fetchone()[0],
                    CURRENT_SCHEMA_VERSION,
                )
                columns = {
                    row[1]
                    for row in storage.connection.execute(
                        "PRAGMA table_info(match_ingest_status)"
                    )
                }
                self.assertIn("start_time", columns)
                derived_columns = {
                    row[1]
                    for row in storage.connection.execute(
                        "PRAGMA table_info(strict_derived_status)"
                    )
                }
                self.assertTrue(
                    {
                        "normalizer_version",
                        "benchmark_version",
                        "profile_context_hash",
                    }
                    <= derived_columns
                )
                self.assertIsNotNone(
                    storage.connection.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type='table' AND name='ingest_scheduler_checkpoints'"""
                    ).fetchone()
                )

    def test_version_five_additively_migrates_derived_lineage_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligence.db"
            connection = sqlite3.connect(path)
            connection.execute(
                """CREATE TABLE strict_derived_status (
                    match_id INTEGER PRIMARY KEY,
                    source_content_hash TEXT NOT NULL,
                    role_assignment_version TEXT NOT NULL,
                    score_version TEXT NOT NULL,
                    team_state_version TEXT NOT NULL,
                    profile_version TEXT NOT NULL,
                    profile_cutoff TEXT NOT NULL,
                    derived_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """INSERT INTO strict_derived_status VALUES
                   (8001, ?, 'role-v1', 'score-v1', 'state-v1',
                    'profile-v1', '2026-01-01', '2026-01-01')""",
                ("a" * 64,),
            )
            connection.commit()
            connection.close()

            with IntelligenceStorage(path) as storage:
                storage.init_schema(seed_events=False)
                columns = {
                    row[1]: row[3]
                    for row in storage.connection.execute(
                        "PRAGMA table_info(strict_derived_status)"
                    )
                }
                self.assertEqual(columns["normalizer_version"], 0)
                self.assertEqual(columns["benchmark_version"], 0)
                self.assertEqual(columns["profile_context_hash"], 0)
                self.assertIsNone(
                    storage.connection.execute(
                        """SELECT profile_context_hash
                             FROM strict_derived_status WHERE match_id=8001"""
                    ).fetchone()[0]
                )
                self.assertEqual(
                    storage.connection.execute(
                        "SELECT MAX(version) FROM intelligence_schema_version"
                    ).fetchone()[0],
                    CURRENT_SCHEMA_VERSION,
                )

    def test_version_six_adds_draft_artifact_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligence.db"
            connection = sqlite3.connect(path)
            connection.execute(
                """CREATE TABLE draft_prediction_validations (
                    run_id TEXT NOT NULL,
                    match_id INTEGER NOT NULL,
                    input_snapshot_hash TEXT NOT NULL,
                    dependency_fingerprint TEXT NOT NULL,
                    validation_version TEXT NOT NULL,
                    validated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, match_id)
                )"""
            )
            connection.commit()
            connection.close()

            with IntelligenceStorage(path) as storage:
                storage.init_schema(seed_events=False)
                columns = {
                    str(row[1])
                    for row in storage.connection.execute(
                        "PRAGMA table_info(draft_prediction_validations)"
                    )
                }
                self.assertIn("artifact_fingerprint", columns)
                self.assertIn("dependency_revision", columns)
                self.assertEqual(
                    tuple(
                        storage.connection.execute(
                            """SELECT dependency_revision, artifact_revision
                                 FROM draft_lineage_revisions WHERE singleton=1"""
                        ).fetchone()
                    ),
                    (1, 1),
                )
                self.assertEqual(
                    storage.connection.execute(
                        "SELECT MAX(version) FROM intelligence_schema_version"
                    ).fetchone()[0],
                    CURRENT_SCHEMA_VERSION,
                )

    def test_version_seven_proofs_are_cleared_without_cutoff_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligence.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """CREATE TABLE intelligence_schema_version (
                       version INTEGER PRIMARY KEY,
                       applied_at TEXT NOT NULL
                   );
                   INSERT INTO intelligence_schema_version VALUES (1, 'v1');
                   INSERT INTO intelligence_schema_version VALUES (7, 'v7');
                   CREATE TABLE draft_prediction_validations (
                       run_id TEXT NOT NULL,
                       match_id INTEGER NOT NULL,
                       input_snapshot_hash TEXT NOT NULL,
                       artifact_fingerprint TEXT NOT NULL,
                       dependency_fingerprint TEXT NOT NULL,
                       dependency_revision INTEGER NOT NULL,
                       validation_version TEXT NOT NULL,
                       validated_at TEXT NOT NULL,
                       PRIMARY KEY (run_id, match_id)
                   );
                   INSERT INTO draft_prediction_validations VALUES (
                       'legacy', 1,
                       'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                       'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                       'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                       7, 'draft-input-lineage-v3', '2026-07-15'
                   );"""
            )
            connection.commit()
            connection.close()

            with IntelligenceStorage(path) as storage:
                storage.init_schema(seed_events=False)
                self.assertEqual(
                    storage.connection.execute(
                        "SELECT COUNT(*) FROM draft_prediction_validations"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    storage.connection.execute(
                        "SELECT MAX(version) FROM intelligence_schema_version"
                    ).fetchone()[0],
                    CURRENT_SCHEMA_VERSION,
                )

    def test_migration_backfills_legacy_normalizer_from_complete_v1_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligence.db"
            content_hash = "a" * 64
            with IntelligenceStorage(path) as storage:
                storage.init_schema()
                storage.connection.execute(
                    """INSERT INTO match_ingest_status
                       (match_id, event_id, stage_scope, stage_in_scope,
                        has_valid_result, latest_raw_content_hash,
                        discovered_at, updated_at)
                       VALUES (8001, 'pgl-wallachia-s8-2026', 'main_event', 1,
                               1, ?, '2026-01-01T00:00:00+00:00',
                               '2026-01-01T00:00:00+00:00')""",
                    (content_hash,),
                )
                storage.connection.executemany(
                    """INSERT INTO player_map_facts
                       (match_id, player_slot, facts_json, coverage,
                        source_content_hash, fact_version, created_at)
                       VALUES (8001, ?, '{}', 1.0, ?, ?,
                               '2026-01-01T00:00:00+00:00')""",
                    (
                        (
                            slot,
                            content_hash,
                            f"opendota-exact-v1:{content_hash}",
                        )
                        for slot in (0, 1, 2, 3, 4, 128, 129, 130, 131, 132)
                    ),
                )
                storage.connection.commit()

                storage.init_schema(seed_events=False)

                row = storage.connection.execute(
                    "SELECT normalizer_version FROM match_ingest_status WHERE match_id=8001"
                ).fetchone()
                self.assertEqual(row["normalizer_version"], "opendota-exact-v1")

    def test_incompatible_old_registry_rolls_back_new_schema_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligence.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE event_registry (event_id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO event_registry VALUES ('conflict')")
            connection.commit()
            connection.close()

            with IntelligenceStorage(path) as storage:
                with self.assertRaises(sqlite3.OperationalError):
                    storage.init_schema()
                tables = {
                    row[0]
                    for row in storage.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertEqual(tables, {"event_registry"})

    def test_drifted_seed_is_rejected_before_readding_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligence.db"
            with IntelligenceStorage(path) as storage:
                storage.init_schema()
                storage.connection.execute(
                    """UPDATE event_registry SET canonical_name='Drifted Name'
                       WHERE event_id='pgl-wallachia-s8-2026'"""
                )
                storage.connection.execute("DELETE FROM intelligence_schema_version")
                storage.connection.commit()

            with IntelligenceStorage(path) as reopened:
                with self.assertRaisesRegex(RuntimeError, "policy drift"):
                    reopened.init_schema()
                self.assertEqual(
                    reopened.connection.execute(
                        "SELECT COUNT(*) FROM intelligence_schema_version"
                    ).fetchone()[0],
                    0,
                )

    def test_runtime_reconciliation_changes_survive_schema_reinitialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligence.db"
            with IntelligenceStorage(path) as storage:
                storage.init_schema()
                storage.connection.execute(
                    """UPDATE event_registry
                       SET observed_map_count=0,
                           reconciliation_status='reconciliation_pending',
                           reconciliation_note='runtime audit still in progress'
                       WHERE event_id='pgl-wallachia-s8-2026'"""
                )
                storage.connection.commit()

            with IntelligenceStorage(path) as reopened:
                reopened.init_schema()
                row = reopened.connection.execute(
                    """SELECT observed_map_count, reconciliation_status,
                              reconciliation_note
                       FROM event_registry
                       WHERE event_id='pgl-wallachia-s8-2026'"""
                ).fetchone()
                self.assertEqual(
                    tuple(row),
                    (0, "reconciliation_pending", "runtime audit still in progress"),
                )

    def test_future_schema_version_is_rejected_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligence.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE intelligence_schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)"
            )
            connection.execute(
                "INSERT INTO intelligence_schema_version VALUES (99, 'future')"
            )
            connection.commit()
            connection.close()

            with IntelligenceStorage(path) as storage:
                with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                    storage.init_schema()
                self.assertIsNone(
                    storage.connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='event_registry'"
                    ).fetchone()
                )


if __name__ == "__main__":
    unittest.main()
