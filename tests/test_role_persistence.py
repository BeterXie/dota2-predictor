from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_intelligence.raw_archive import canonical_json_bytes, schema_fingerprint
from event_intelligence.registry import EventRegistry
from event_intelligence.storage import IntelligenceStorage
from scripts.assign_strict_event_roles import (
    ASSIGNMENT_VERSIONS,
    AvailabilityMode,
    _role_evidence,
    load_strict_maps,
    run_assignment,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
SLOTS = (0, 1, 2, 3, 4, 128, 129, 130, 131, 132)


def _facts(position: int) -> dict[str, object]:
    values = (
        (1, 5_000, 70, False, 0, 0),
        (2, 4_500, 55, False, 0, 0),
        (3, 3_800, 35, False, 0, 0),
        (3, 2_500, 15, True, 1, 2),
        (1, 1_800, 5, False, 5, 5),
    )[position - 1]
    lane, gold, last_hits, roaming, observers, sentries = values
    return {
        "lane_role": lane,
        "gold_at_10": gold,
        "last_hits_at_10": last_hits,
        "is_roaming": roaming,
        "observer_wards_at_10": observers,
        "sentry_wards_at_10": sentries,
        "gold_per_min": 999 - position,
        "camps_stacked": 100 + position,
    }


class StrictRoleAssignmentCliTests(unittest.TestCase):
    def _add_map(
        self,
        storage: IntelligenceStorage,
        root: Path,
        *,
        match_id: int,
        started_at: datetime,
        first_usable_at: datetime,
    ) -> None:
        payload = {
            "match_id": match_id,
            "start_time": int(started_at.timestamp()),
            "duration": 1_800,
        }
        content = canonical_json_bytes(payload)
        content_hash = hashlib.sha256(content).hexdigest()
        compressed = gzip.compress(content, mtime=0)
        artifact_path = root / f"{content_hash}.json.gz"
        artifact_path.write_bytes(compressed)
        usable = first_usable_at.isoformat()
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
                f"https://api.opendota.com/api/matches/{match_id}",
                f"https://api.opendota.com/api/matches/{match_id}",
                str(artifact_path),
                len(content),
                len(compressed),
                usable,
                usable,
                schema_fingerprint(payload),
                "pgl-wallachia-s8-2026",
                match_id,
                usable,
            ),
        )
        connection.execute(
            """INSERT INTO match_ingest_status
               (match_id, event_id, start_time, stage_scope, stage_in_scope,
                has_valid_result, ingest_state, player_readiness,
                latest_raw_artifact_id, latest_raw_content_hash,
                discovered_at, updated_at)
               VALUES (?, 'pgl-wallachia-s8-2026', ?, 'main_event', 1, 1,
                       'detailed', 'ready', ?, ?, ?, ?)""",
            (
                match_id,
                int(started_at.timestamp()),
                content_hash,
                content_hash,
                usable,
                usable,
            ),
        )
        for index, slot in enumerate(SLOTS):
            position = index % 5 + 1
            account_id = None if position == 5 else 10_000 + index % 5 + (index // 5) * 100
            facts = _facts(position)
            connection.execute(
                """INSERT INTO player_map_facts
                   (match_id, player_slot, account_id, team_id, hero_id,
                    is_radiant, facts_json, coverage, source_artifact_id,
                    source_content_hash, fact_version, first_usable_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?)""",
                (
                    match_id,
                    slot,
                    account_id,
                    100 if slot < 128 else 200,
                    position,
                    int(slot < 128),
                    json.dumps(facts),
                    content_hash,
                    content_hash,
                    f"opendota-exact-v1:{content_hash}",
                    usable,
                    usable,
                ),
            )

    def _database(self, root: Path) -> Path:
        database = root / "strict.db"
        with IntelligenceStorage(database) as storage:
            storage.init_schema()
            EventRegistry(storage).seed_approved_events()
            second_start = START + timedelta(hours=2)
            backfilled = second_start + timedelta(days=30)
            self._add_map(
                storage,
                root,
                match_id=9_001,
                started_at=START,
                first_usable_at=backfilled,
            )
            self._add_map(
                storage,
                root,
                match_id=9_002,
                started_at=second_start,
                first_usable_at=backfilled,
            )
            storage.connection.commit()
        return database

    def test_reconstructed_and_prospective_versions_do_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(Path(directory))

            reconstructed = run_assignment(database)
            prospective = run_assignment(
                database, availability_mode=AvailabilityMode.PROSPECTIVE
            )

            self.assertEqual((reconstructed.inserted, prospective.inserted), (40, 40))
            self.assertNotEqual(
                reconstructed.assignment_version, prospective.assignment_version
            )
            with IntelligenceStorage(database) as storage:
                rows = storage.connection.execute(
                    """SELECT player_slot, position, assignment_source,
                              assignment_version
                       FROM player_role_assignments
                       WHERE match_id=9002 AND purpose='expected_position'
                       ORDER BY assignment_version, player_slot"""
                ).fetchall()
            by_version = {
                version: [row for row in rows if row["assignment_version"] == version]
                for version in ASSIGNMENT_VERSIONS.values()
            }
            rebuilt = by_version[
                ASSIGNMENT_VERSIONS[AvailabilityMode.RECONSTRUCTED_WALK_FORWARD]
            ]
            live = by_version[ASSIGNMENT_VERSIONS[AvailabilityMode.PROSPECTIVE]]
            self.assertEqual(sum(row["position"] is not None for row in rebuilt), 8)
            self.assertTrue(
                all(
                    row["assignment_source"] == "historical_pattern"
                    for row in rebuilt
                    if row["position"] is not None
                )
            )
            self.assertTrue(all(row["position"] is None for row in live))

    def test_dry_run_repeated_write_and_audit_fields_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(Path(directory))

            dry_run = run_assignment(database, dry_run=True)
            first = run_assignment(database)
            second = run_assignment(database)

            self.assertEqual((dry_run.inserted, first.inserted), (40, 40))
            self.assertEqual((second.inserted, second.updated, second.unchanged), (0, 0, 40))
            with IntelligenceStorage(database) as storage:
                self.assertEqual(
                    storage.connection.execute(
                        "SELECT COUNT(*) FROM player_role_assignments"
                    ).fetchone()[0],
                    40,
                )
                row = storage.connection.execute(
                    """SELECT purpose, assignment_source, confidence, input_cutoff,
                              input_hash, assignment_version, account_id
                       FROM player_role_assignments
                       WHERE match_id=9001 AND player_slot=4
                         AND purpose='observed_position'"""
                ).fetchone()
            self.assertEqual(row["purpose"], "observed_position")
            self.assertIn(row["assignment_source"], {"single_map", "historical_pattern"})
            self.assertGreaterEqual(row["confidence"], 0.0)
            self.assertIsNotNone(datetime.fromisoformat(row["input_cutoff"]))
            self.assertEqual(len(row["input_hash"]), 64)
            self.assertEqual(
                row["assignment_version"],
                ASSIGNMENT_VERSIONS[AvailabilityMode.RECONSTRUCTED_WALK_FORWARD],
            )
            self.assertIsNone(row["account_id"])

    def test_anonymous_identity_is_negative_and_unique_per_map_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(Path(directory))
            with IntelligenceStorage(database) as storage:
                games = load_strict_maps(storage.connection, database_path=database)
            first = next(row for row in games[0].players if row.player_slot == 4)
            second = next(row for row in games[1].players if row.player_slot == 4)
            self.assertLess(first.evidence.player_id, 0)
            self.assertLess(second.evidence.player_id, 0)
            self.assertNotEqual(first.evidence.player_id, second.evidence.player_id)

    def test_terminal_gpm_and_stacks_never_enter_single_map_evidence(self) -> None:
        facts = _facts(1)
        evidence = _role_evidence(
            facts,
            player_id=101,
            first_usable_at=START + timedelta(minutes=31),
        )
        self.assertIsNone(evidence.final_gpm)
        self.assertIsNone(evidence.stacks_at_10)
        self.assertEqual(evidence.gold_at_10, facts["gold_at_10"])

    def test_same_team_hash_still_updates_slot_identity_position_and_team(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(Path(directory))
            first = run_assignment(database)
            self.assertEqual(first.inserted, 40)

            with IntelligenceStorage(database) as storage:
                baseline_hashes = {
                    (row["player_slot"], row["purpose"]): row["input_hash"]
                    for row in storage.connection.execute(
                        """SELECT player_slot, purpose, input_hash
                           FROM player_role_assignments WHERE match_id=9001"""
                    ).fetchall()
                }
                rows = storage.connection.execute(
                    """SELECT player_slot, account_id, facts_json
                       FROM player_map_facts
                       WHERE match_id=9001 AND player_slot IN (0, 1)
                       ORDER BY player_slot"""
                ).fetchall()
                storage.connection.execute(
                    """UPDATE player_map_facts
                       SET account_id=?, facts_json=?
                       WHERE match_id=9001 AND player_slot=0""",
                    (rows[1]["account_id"], rows[1]["facts_json"]),
                )
                storage.connection.execute(
                    """UPDATE player_map_facts
                       SET account_id=?, facts_json=?
                       WHERE match_id=9001 AND player_slot=1""",
                    (rows[0]["account_id"], rows[0]["facts_json"]),
                )
                storage.connection.execute(
                    """UPDATE player_map_facts SET team_id=101
                       WHERE match_id=9001 AND is_radiant=1"""
                )
                storage.connection.commit()

            corrected = run_assignment(database)

            self.assertEqual(
                (corrected.inserted, corrected.updated, corrected.unchanged),
                (0, 10, 30),
            )
            with IntelligenceStorage(database) as storage:
                rows = storage.connection.execute(
                    """SELECT player_slot, purpose, account_id, team_id, position,
                              input_hash
                       FROM player_role_assignments
                       WHERE match_id=9001 AND player_slot IN (0, 1)
                       ORDER BY player_slot, purpose"""
                ).fetchall()
            self.assertEqual({row["team_id"] for row in rows}, {101})
            self.assertEqual(
                {
                    (row["player_slot"], row["purpose"]): row["input_hash"]
                    for row in rows
                },
                {
                    key: value
                    for key, value in baseline_hashes.items()
                    if key[0] in (0, 1)
                },
            )
            for row in rows:
                expected_account = (
                    10_001 if row["player_slot"] == 0 else 10_000
                )
                expected_position = (
                    2 if row["player_slot"] == 0 else 1
                ) if row["purpose"] == "observed_position" else None
                self.assertEqual(row["account_id"], expected_account)
                self.assertEqual(row["position"], expected_position)

    def test_load_rejects_cross_side_account_and_team_identity_conflicts(self) -> None:
        cases = (
            (
                "duplicate positive account IDs",
                """UPDATE player_map_facts SET account_id=10001
                   WHERE match_id=9001 AND player_slot=128""",
            ),
            (
                "mixed team IDs on one side",
                """UPDATE player_map_facts SET team_id=999
                   WHERE match_id=9001 AND player_slot=1""",
            ),
            (
                "same team ID on both sides",
                """UPDATE player_map_facts SET team_id=100
                   WHERE match_id=9001 AND is_radiant=0""",
            ),
        )
        for message, statement in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                database = self._database(Path(directory))
                with IntelligenceStorage(database) as storage:
                    storage.connection.execute(statement)
                    storage.connection.commit()
                    with self.assertRaisesRegex(ValueError, message):
                        load_strict_maps(
                            storage.connection, database_path=database
                        )


if __name__ == "__main__":
    unittest.main()
