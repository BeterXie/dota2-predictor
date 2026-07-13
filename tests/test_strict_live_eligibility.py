from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from event_intelligence.storage import IntelligenceStorage
from live_betting.storage import LiveBettingStore
from live_betting.strict_eligibility import (
    StrictMappingConflictError,
    StrictMappingError,
    accept_strict_live_map_mapping,
    init_strict_live_eligibility_schema,
    query_strict_live_eligibility,
    record_strict_live_mapping_candidate,
)


EVENT_ID = "pgl-wallachia-s8-2026"
ACCEPTED_AT = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
RECORDED_AT = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
METADATA_AT = datetime(2026, 7, 13, 11, 0, tzinfo=timezone.utc)
SCHEDULE_RAW = "2026-04-20 12:00:00"
SCHEDULE_UTC = datetime(2026, 4, 20, 4, 0, tzinfo=timezone.utc)


def evidence(
    *,
    raybet_tournament: str = "PGL Wallachia Season 8",
    event_name: str = "PGL Wallachia Season 8",
    scheduled_at: str = SCHEDULE_RAW,
    scheduled_at_utc: datetime = SCHEDULE_UTC,
    stage_scope: str = "main_event",
    canonical_team_one_id: int = 101,
    canonical_team_one_name: str = "Alpha Canonical",
    canonical_team_two_id: int = 202,
    canonical_team_two_name: str = "Beta Canonical",
) -> dict[str, object]:
    return {
        "kind": "manual_cross_source_review",
        "raybet_url": "https://example.invalid/raybet/match-1",
        "official_event_url": "https://example.invalid/event",
        "tournament": {
            "raybet_name": raybet_tournament,
            "event_name": event_name,
        },
        "schedule": {
            "raybet_scheduled_at": scheduled_at,
            "utc_offset_minutes": 480,
            "scheduled_at_utc": scheduled_at_utc.isoformat(),
            "timezone_evidence": "audited RayBet UTC+08 display contract",
        },
        "stage": {
            "scope": stage_scope,
            "source_url": "https://example.invalid/event/stage",
        },
        "team_crosswalk": {
            "team_one": {
                "raybet_team_id": 501,
                "raybet_team_name": "Alpha",
                "canonical_team_id": canonical_team_one_id,
                "canonical_team_name": canonical_team_one_name,
                "source_url": "https://example.invalid/teams/alpha",
            },
            "team_two": {
                "raybet_team_id": 502,
                "raybet_team_name": "Beta",
                "canonical_team_id": canonical_team_two_id,
                "canonical_team_name": canonical_team_two_name,
                "source_url": "https://example.invalid/teams/beta",
            },
        },
    }


def raybet_payload(
    *,
    tournament: str = "PGL Wallachia Season 8",
    scheduled_at: str = SCHEDULE_RAW,
    best_of: int = 3,
    team_one_id: int = 501,
    team_one_name: str = "Alpha",
    team_two_id: int = 502,
    team_two_name: str = "Beta",
    stage: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "match-1",
        "game_id": 151,
        "tournament_name": tournament,
        "start_time": scheduled_at,
        "round": f"bo{best_of}",
        "team": [
            {"pos": 1, "team_id": team_one_id, "team_name": team_one_name},
            {"pos": 2, "team_id": team_two_id, "team_name": team_two_name},
        ],
    }
    if stage is not None:
        row["stage"] = stage
    return row


class StrictLiveEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "strict.db"
        with IntelligenceStorage(self.path) as storage:
            storage.init_schema()
        self.set_canonical_teams()
        self.upsert_raybet(updated_at=METADATA_AT)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        init_strict_live_eligibility_schema(self.connection)
        self.clock = patch(
            "live_betting.strict_eligibility._utc_now", return_value=RECORDED_AT
        )
        self.clock.start()

    def tearDown(self) -> None:
        self.clock.stop()
        self.connection.close()
        self.directory.cleanup()

    def upsert_raybet(
        self,
        payload: dict[str, object] | None = None,
        *,
        updated_at: datetime = METADATA_AT,
    ) -> None:
        with LiveBettingStore(self.path) as store:
            store.init_schema()
            store.upsert_raybet_match(payload or raybet_payload(), updated_at)
            store.connection.commit()

    def set_canonical_teams(
        self,
        rows: tuple[tuple[int, str], ...] = (
            (101, "Alpha Canonical"),
            (202, "Beta Canonical"),
        ),
    ) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS teams (
                   team_id INTEGER PRIMARY KEY,
                   name TEXT,
                   tag TEXT,
                   logo_url TEXT,
                   updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        connection.execute("DELETE FROM teams")
        connection.executemany(
            "INSERT INTO teams(team_id, name) VALUES (?, ?)", rows
        )
        connection.commit()
        connection.close()

    def accept(self, **overrides: object):
        values: dict[str, object] = {
            "raybet_match_id": "match-1",
            "map_number": 1,
            "event_id": EVENT_ID,
            "team_one_id": 501,
            "team_two_id": 502,
            "canonical_team_one_id": 101,
            "canonical_team_two_id": 202,
            "source": "manual_event_team_audit",
            "evidence": evidence(),
            "accepted_by": "operator-a",
            "accepted_at": ACCEPTED_AT,
        }
        values.update(overrides)
        return accept_strict_live_map_mapping(self.connection, **values)  # type: ignore[arg-type]

    def query(self, *, at: datetime = RECORDED_AT, map_number: int = 1):
        return query_strict_live_eligibility(
            self.connection,
            raybet_match_id="match-1",
            map_number=map_number,
            transport_observed_at=at,
        )

    def test_schema_is_additive_and_accepted_identity_is_immutable(self) -> None:
        self.connection.execute("CREATE TABLE unrelated (value TEXT)")
        self.connection.execute("INSERT INTO unrelated VALUES ('kept')")
        self.connection.commit()
        init_strict_live_eligibility_schema(self.connection)

        mapping = self.accept()

        self.assertEqual(
            self.connection.execute("SELECT value FROM unrelated").fetchone()[0],
            "kept",
        )
        columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(strict_live_map_mappings)"
            )
        }
        self.assertTrue(
            {
                "raybet_identity_json",
                "raybet_identity_hash",
                "raybet_metadata_updated_at",
                "recorded_at",
                "scheduled_at_utc",
                "stage_scope",
            }
            <= columns
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.connection.execute(
                "UPDATE strict_live_map_mappings SET team_one_id=999 WHERE mapping_id=?",
                (mapping.mapping_id,),
            )
        self.connection.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be deleted"):
            self.connection.execute(
                "DELETE FROM strict_live_map_mappings WHERE mapping_id=?",
                (mapping.mapping_id,),
            )
        self.connection.rollback()

    def test_unknown_candidate_and_fuzzy_are_never_eligible(self) -> None:
        self.assertEqual(self.query().reason, "accepted_mapping_missing")
        for method in ("candidate", "fuzzy"):
            record_strict_live_mapping_candidate(
                self.connection,
                raybet_match_id="match-1",
                map_number=1,
                source="name_search",
                evidence={"matched_text": "PGL Wallachia"},
                observed_at=ACCEPTED_AT,
                match_method=method,
                proposed_event_id=EVENT_ID,
                proposed_team_one_id=501,
                proposed_team_two_id=502,
                proposed_canonical_team_one_id=101,
                proposed_canonical_team_two_id=202,
            )

        self.assertEqual(self.query().reason, "accepted_mapping_missing")
        self.assertEqual(
            [tuple(row) for row in self.connection.execute(
                """SELECT match_method, decision
                   FROM strict_live_map_mapping_audit ORDER BY audit_id"""
            )],
            [("candidate", "audit_only"), ("fuzzy", "audit_only")],
        )
        with self.assertRaises(StrictMappingError):
            record_strict_live_mapping_candidate(
                self.connection,
                raybet_match_id="match-1",
                map_number=1,
                source="bad",
                evidence={"name": "Alpha"},
                observed_at=ACCEPTED_AT,
                match_method="manual_exact",
            )

    def test_accept_requires_exact_raybet_team_ids_and_order(self) -> None:
        with self.assertRaisesRegex(
            StrictMappingError, "raybet_exact_team_ids_mismatch"
        ):
            self.accept(team_one_id=10, team_two_id=20)
        with self.assertRaisesRegex(
            StrictMappingError, "raybet_exact_team_order_mismatch"
        ):
            self.accept(team_one_id=502, team_two_id=501)

        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM strict_live_map_mappings"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            [row[0] for row in self.connection.execute(
                "SELECT reason FROM strict_live_map_mapping_audit ORDER BY audit_id"
            )],
            ["raybet_exact_team_ids_mismatch", "raybet_exact_team_order_mismatch"],
        )

    def test_accepted_mapping_is_causal_and_exposes_identity_refs(self) -> None:
        mapping = self.accept(accepted_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

        before_recording = self.query(at=RECORDED_AT - timedelta(microseconds=1))
        self.assertFalse(before_recording.eligible)
        self.assertEqual(before_recording.reason, "mapping_not_yet_recorded")
        eligible = self.query()
        self.assertTrue(eligible.eligible)
        self.assertEqual(eligible.mapping, mapping)
        self.assertEqual(mapping.recorded_at, RECORDED_AT)
        self.assertEqual(mapping.raybet_metadata_updated_at, METADATA_AT)
        self.assertEqual(eligible.mapping_refs, eligible.input_refs())
        self.assertEqual(eligible.mapping_refs["strict_raybet_team_one_id"], 501)
        self.assertEqual(eligible.mapping_refs["strict_canonical_team_one_id"], 101)
        self.assertNotEqual(mapping.raybet_team_one_id, mapping.canonical_team_one_id)
        self.assertEqual(mapping.canonical_team_one_name, "Alpha Canonical")
        self.assertEqual(len(mapping.canonical_identity_hash), 64)
        self.assertEqual(len(mapping.crosswalk_evidence_hash), 64)
        self.assertEqual(len(mapping.raybet_identity_hash), 64)
        identity = json.loads(mapping.raybet_identity_json)
        self.assertEqual(identity["team_one"], {"name": "Alpha", "pos": 1, "team_id": 501})

        audit = self.connection.execute(
            """SELECT observed_at, recorded_at, raybet_metadata_updated_at
               FROM strict_live_map_mapping_audit"""
        ).fetchone()
        self.assertEqual(audit[0], datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat())
        self.assertEqual(audit[1], RECORDED_AT.isoformat())
        self.assertEqual(audit[2], METADATA_AT.isoformat())

    def test_future_accepted_or_metadata_times_cannot_be_backdated(self) -> None:
        with self.assertRaisesRegex(StrictMappingError, "accepted_at_in_future"):
            self.accept(accepted_at=RECORDED_AT + timedelta(seconds=1))

        self.upsert_raybet(updated_at=RECORDED_AT + timedelta(seconds=1))
        with self.assertRaisesRegex(StrictMappingError, "raybet_metadata_from_future"):
            self.accept(accepted_at=ACCEPTED_AT)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM strict_live_map_mappings"
            ).fetchone()[0],
            0,
        )

    def test_identity_snapshot_detects_team_upsert_drift(self) -> None:
        mapping = self.accept()
        self.upsert_raybet(
            raybet_payload(team_one_id=503, team_one_name="Gamma"),
            updated_at=RECORDED_AT + timedelta(minutes=1),
        )

        result = self.query(at=RECORDED_AT + timedelta(minutes=2))

        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "raybet_metadata_drift")
        self.assertEqual(result.mapping_refs["strict_raybet_identity_hash"], mapping.raybet_identity_hash)

    def test_crosswalk_evidence_and_canonical_teams_are_required(self) -> None:
        missing_crosswalk = evidence()
        del missing_crosswalk["team_crosswalk"]
        with self.assertRaisesRegex(
            StrictMappingError, "team_crosswalk_evidence_missing"
        ):
            self.accept(evidence=missing_crosswalk)

        wrong_name = evidence(canonical_team_one_name="Wrong Canonical Team")
        with self.assertRaisesRegex(
            StrictMappingError, "team_crosswalk_evidence_mismatch"
        ):
            self.accept(evidence=wrong_name)

        with self.assertRaisesRegex(StrictMappingError, "canonical_team_missing"):
            self.accept(
                canonical_team_one_id=999,
                evidence=evidence(
                    canonical_team_one_id=999,
                    canonical_team_one_name="Unknown Canonical Team",
                ),
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM strict_live_map_mappings"
            ).fetchone()[0],
            0,
        )

    def test_canonical_crosswalk_drift_or_missing_team_is_fail_closed(self) -> None:
        mapping = self.accept()
        self.set_canonical_teams(
            ((101, "Alpha Renamed"), (202, "Beta Canonical"))
        )

        drift = self.query()

        self.assertFalse(drift.eligible)
        self.assertEqual(drift.reason, "canonical_team_metadata_drift")
        self.assertEqual(
            drift.mapping_refs["strict_canonical_identity_hash"],
            mapping.canonical_identity_hash,
        )

        self.set_canonical_teams(((101, "Alpha Canonical"),))
        missing = self.query()
        self.assertFalse(missing.eligible)
        self.assertEqual(missing.reason, "canonical_team_missing")

    def test_same_identity_repoll_does_not_create_false_drift(self) -> None:
        self.accept()
        self.upsert_raybet(updated_at=RECORDED_AT + timedelta(minutes=1))

        result = self.query(at=RECORDED_AT + timedelta(minutes=2))

        self.assertTrue(result.eligible)

    def test_map_number_must_not_exceed_raybet_best_of(self) -> None:
        with self.assertRaisesRegex(StrictMappingError, "map_number_exceeds_best_of"):
            self.accept(map_number=4)
        mapping = self.accept(map_number=3)

        self.assertEqual(mapping.raybet_best_of, 3)
        self.assertTrue(self.query(map_number=3).eligible)

    def test_schedule_must_be_audited_and_inside_event_window(self) -> None:
        outside_raw = "2026-05-20 12:00:00"
        outside_utc = datetime(2026, 5, 20, 4, 0, tzinfo=timezone.utc)
        self.upsert_raybet(raybet_payload(scheduled_at=outside_raw))
        with self.assertRaisesRegex(
            StrictMappingError, "raybet_schedule_outside_event_window"
        ):
            self.accept(
                evidence=evidence(
                    scheduled_at=outside_raw, scheduled_at_utc=outside_utc
                )
            )

        self.upsert_raybet(raybet_payload())
        missing_timezone = evidence()
        del missing_timezone["schedule"]["timezone_evidence"]  # type: ignore[index]
        with self.assertRaisesRegex(
            StrictMappingError, "raybet_schedule_timezone_evidence_missing"
        ):
            self.accept(evidence=missing_timezone)

    def test_tournament_alias_requires_explicit_exact_evidence(self) -> None:
        localized_name = "RayBet Localized PGL Season 8"
        self.upsert_raybet(raybet_payload(tournament=localized_name))
        with self.assertRaisesRegex(
            StrictMappingError, "raybet_tournament_evidence_mismatch"
        ):
            self.accept()

        mapping = self.accept(evidence=evidence(raybet_tournament=localized_name))

        self.assertEqual(
            json.loads(mapping.raybet_identity_json)["tournament"], localized_name
        )
        self.assertTrue(self.query().eligible)

    def test_qualifier_or_unapproved_stage_is_fail_closed(self) -> None:
        self.upsert_raybet(raybet_payload(stage="qualifier"))
        with self.assertRaisesRegex(StrictMappingError, "event_stage_excluded"):
            self.accept(evidence=evidence(stage_scope="qualifier"))

        self.upsert_raybet(raybet_payload())
        with self.assertRaisesRegex(StrictMappingError, "event_stage_not_included"):
            self.accept(evidence=evidence(stage_scope="play_in"))

    def test_event_policy_window_and_evidence_are_rechecked_by_query(self) -> None:
        self.accept()
        cases = (
            ("scope='audit_only'", "event_scope_not_formal_main_event"),
            ("approval_status='pending'", "event_not_approved"),
            ("evidence_status='unverified'", "event_evidence_not_manually_audited"),
            ("official_evidence_urls_json='[]'", "event_official_evidence_missing"),
            (
                "main_event_start_at='2026-04-21T00:00:00+00:00'",
                "raybet_schedule_outside_event_window",
            ),
            ("included_stages_json='[]'", "event_exclusion_policy_incomplete"),
        )
        for update, expected_reason in cases:
            with self.subTest(update=update):
                self.connection.execute(
                    f"UPDATE event_registry SET {update} WHERE event_id=?",  # noqa: S608
                    (EVENT_ID,),
                )
                result = self.query()
                self.assertFalse(result.eligible)
                self.assertEqual(result.reason, expected_reason)
                self.connection.rollback()

    def test_future_event_approval_is_not_knowable_at_transport(self) -> None:
        self.accept()
        self.connection.execute(
            "UPDATE event_registry SET approved_at=? WHERE event_id=?",
            ((RECORDED_AT + timedelta(hours=1)).isoformat(), EVENT_ID),
        )

        result = self.query()

        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "event_approval_not_yet_available")

    def test_exact_value_is_idempotent_across_restart_but_rebind_conflicts(self) -> None:
        first = self.accept()
        self.connection.close()
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        init_strict_live_eligibility_schema(self.connection)

        second = self.accept(accepted_at=ACCEPTED_AT + timedelta(minutes=1))
        self.assertEqual(first, second)
        with self.assertRaisesRegex(
            StrictMappingConflictError, "accepted_mapping_rebind_forbidden"
        ):
            self.accept(team_two_id=503)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM strict_live_map_mappings"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            [row[0] for row in self.connection.execute(
                "SELECT decision FROM strict_live_map_mapping_audit ORDER BY audit_id"
            )],
            ["accepted", "idempotent", "conflict"],
        )

    def test_missing_raw_identity_and_invalid_inputs_fail_closed(self) -> None:
        self.connection.execute(
            "UPDATE raybet_matches SET raw_json='{}' WHERE raybet_match_id='match-1'"
        )
        self.connection.commit()
        self.assertEqual(self.query().reason, "accepted_mapping_missing")
        with self.assertRaisesRegex(StrictMappingError, "raybet_raw_match_id_mismatch"):
            self.accept()

        self.assertEqual(
            query_strict_live_eligibility(
                self.connection,
                raybet_match_id="match-1",
                map_number=0,
                transport_observed_at=RECORDED_AT,
            ).reason,
            "invalid_map_number",
        )
        self.assertEqual(
            query_strict_live_eligibility(
                self.connection,
                raybet_match_id="match-1",
                map_number=1,
                transport_observed_at=RECORDED_AT.replace(tzinfo=None),
            ).reason,
            "invalid_transport_time",
        )

    def test_query_is_pure_and_mapping_evidence_is_canonical(self) -> None:
        supplied = evidence()
        supplied["extra"] = {"z": 2, "a": 1}
        mapping = self.accept(evidence=copy.deepcopy(supplied))
        before = self.connection.total_changes

        for _ in range(3):
            self.assertTrue(self.query().eligible)

        self.assertEqual(self.connection.total_changes, before)
        self.assertEqual(json.loads(mapping.evidence_json), supplied)


if __name__ == "__main__":
    unittest.main()
