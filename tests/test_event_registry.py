from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from event_intelligence.models import (
    ApprovalStatus,
    EventScope,
    ReconciliationStatus,
    StageScope,
)
from event_intelligence.registry import EventRegistry
from event_intelligence.storage import IntelligenceStorage


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
APPROVED_LEAGUE_IDS = (19543, 19696, 19101, 19785)


class EventRegistryTests(unittest.TestCase):
    def test_seed_is_idempotent_and_returns_only_the_four_audited_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligence.db"
            with IntelligenceStorage(path) as storage:
                storage.init_schema()
                registry = EventRegistry(storage)
                before = {
                    row["opendota_league_id"]: row["event_id"]
                    for row in storage.connection.execute(
                        "SELECT event_id, opendota_league_id FROM event_registry"
                    )
                }

                storage.init_schema()
                registry.seed_approved_events()
                after = {
                    row["opendota_league_id"]: row["event_id"]
                    for row in storage.connection.execute(
                        "SELECT event_id, opendota_league_id FROM event_registry"
                    )
                }

                self.assertEqual(before, after)
                self.assertEqual(tuple(before), APPROVED_LEAGUE_IDS)
                self.assertEqual(
                    tuple(event.opendota_league_id for event in registry.formal_events()),
                    APPROVED_LEAGUE_IDS,
                )
                self.assertTrue(all(event.scope is EventScope.FORMAL_MAIN_EVENT
                                    for event in registry.formal_events()))
                self.assertTrue(all(event.approval_status is ApprovalStatus.APPROVED
                                    for event in registry.formal_events()))

    def test_blast_lcq_and_explicit_exclusions_are_part_of_the_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with IntelligenceStorage(Path(directory) / "intelligence.db") as storage:
                storage.init_schema()
                blast = EventRegistry(storage).get_by_league_id(19101)

                self.assertIsNotNone(blast)
                assert blast is not None
                self.assertEqual(
                    blast.included_stages,
                    (StageScope.MAIN_EVENT, StageScope.INTERNAL_LCQ),
                )
                self.assertTrue(blast.include_internal_lcq)
                self.assertEqual(
                    set(blast.excluded_categories),
                    {"qualifier", "division_2", "exhibition", "forfeit", "void_remake"},
                )

    def test_ewc_count_discrepancy_remains_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with IntelligenceStorage(Path(directory) / "intelligence.db") as storage:
                storage.init_schema()
                ewc = EventRegistry(storage).get_by_league_id(19785)

                self.assertIsNotNone(ewc)
                assert ewc is not None
                self.assertIs(ewc.reconciliation_status, ReconciliationStatus.PENDING)
                self.assertEqual(ewc.expected_map_count, 120)
                self.assertEqual(ewc.observed_map_count, 120)
                self.assertEqual(ewc.public_map_count, 121)
                self.assertIn("120", ewc.reconciliation_note or "")
                self.assertIn("121", ewc.reconciliation_note or "")

    def test_candidate_discovery_is_audit_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with IntelligenceStorage(Path(directory) / "intelligence.db") as storage:
                storage.init_schema()
                registry = EventRegistry(storage)
                first_id = registry.discover_candidate(
                    source="opendota",
                    provider_event_id="99999",
                    canonical_name="Unreviewed Qualifier",
                    evidence_urls=("https://www.opendota.com/leagues/99999",),
                    discovered_at=NOW,
                )
                second_id = registry.discover_candidate(
                    source="opendota",
                    provider_event_id="99999",
                    canonical_name="Unreviewed Qualifier renamed",
                    evidence_urls=("https://www.opendota.com/leagues/99999",),
                    discovered_at=NOW,
                )

                self.assertEqual(first_id, second_id)
                self.assertEqual(
                    tuple(event.opendota_league_id for event in registry.formal_events()),
                    APPROVED_LEAGUE_IDS,
                )
                self.assertEqual(registry.formal_matches(), ())
                self.assertEqual(
                    storage.connection.execute(
                        "SELECT COUNT(*) FROM event_candidates WHERE audit_status='pending'"
                    ).fetchone()[0],
                    1,
                )

    def test_formal_map_view_enforces_stage_and_result_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with IntelligenceStorage(Path(directory) / "intelligence.db") as storage:
                storage.init_schema()
                registry = EventRegistry(storage)
                blast = registry.get_by_league_id(19101)
                pgl = registry.get_by_league_id(19543)
                assert blast is not None and pgl is not None

                rows = [
                    (1, blast.event_id, "main_event", 1, 1, 0, 0, 0),
                    (2, blast.event_id, "internal_lcq", 1, 1, 0, 0, 0),
                    (3, blast.event_id, "qualifier", 0, 1, 0, 0, 0),
                    (4, blast.event_id, "main_event", 1, 1, 1, 0, 0),
                    (5, blast.event_id, "main_event", 1, 1, 0, 1, 0),
                    (6, blast.event_id, "main_event", 1, 1, 0, 0, 1),
                    (7, blast.event_id, "main_event", 1, 0, 0, 0, 0),
                    (8, pgl.event_id, "internal_lcq", 1, 1, 0, 0, 0),
                ]
                storage.connection.executemany(
                    """INSERT INTO match_ingest_status
                    (match_id, event_id, stage_scope, stage_in_scope,
                     has_valid_result, is_exhibition, is_forfeit, is_void_remake,
                     discovered_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [row + (NOW.isoformat(), NOW.isoformat()) for row in rows],
                )
                storage.connection.commit()

                self.assertEqual(
                    tuple(row.match_id for row in registry.formal_matches()),
                    (1, 2),
                )


if __name__ == "__main__":
    unittest.main()
