from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from event_intelligence.models import RolePurpose
from event_intelligence.roles import (
    AuditedRosterPosition,
    HistoricalPositionEvidence,
    RoleSource,
    SingleMapRoleEvidence,
    assign_expected_positions,
    assign_observed_positions,
)


UTC = timezone.utc
TARGET_START = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
EXPECTED_CUTOFF = TARGET_START - timedelta(minutes=5)
OBSERVED_CUTOFF = TARGET_START + timedelta(hours=1)
PLAYER_IDS = (101, 102, 103, 104, 105)


def target_evidence() -> tuple[SingleMapRoleEvidence, ...]:
    usable = TARGET_START + timedelta(minutes=45)
    return (
        SingleMapRoleEvidence(101, usable, 1, 5_000, 70, False, 0, 0, 0, 900),
        SingleMapRoleEvidence(102, usable, 2, 4_500, 55, False, 0, 0, 0, 800),
        SingleMapRoleEvidence(103, usable, 3, 3_800, 35, False, 0, 0, 0, 700),
        SingleMapRoleEvidence(104, usable, 3, 2_500, 15, True, 1, 2, 2, 600),
        SingleMapRoleEvidence(105, usable, 1, 1_800, 5, False, 5, 5, 5, 300),
    )


def roster() -> tuple[AuditedRosterPosition, ...]:
    audited = EXPECTED_CUTOFF - timedelta(days=1)
    return tuple(
        AuditedRosterPosition(
            player_id=player_id,
            position=position,
            audited_at=audited,
            first_usable_at=audited,
        )
        for position, player_id in enumerate(PLAYER_IDS, start=1)
    )


def position_map(assignments) -> dict[int, int | None]:
    return {assignment.player_id: assignment.position for assignment in assignments}


class RoleAssignmentTests(unittest.TestCase):
    def test_audited_roster_has_priority_for_both_purposes(self) -> None:
        misleading = tuple(
            replace(evidence, lane_role=2 if evidence.player_id == 101 else 1)
            for evidence in target_evidence()
        )

        observed = assign_observed_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=OBSERVED_CUTOFF,
            players=misleading,
            audited_roster=roster(),
        )
        expected = assign_expected_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=EXPECTED_CUTOFF,
            player_ids=PLAYER_IDS,
            audited_roster=roster(),
        )

        wanted = dict(zip(PLAYER_IDS, range(1, 6)))
        self.assertEqual(position_map(observed), wanted)
        self.assertEqual(position_map(expected), wanted)
        for assignment in (*observed, *expected):
            self.assertEqual(assignment.source, RoleSource.AUDITED_ROSTER)
            self.assertEqual(assignment.confidence, 1.0)
            self.assertTrue(assignment.usable_for_role_dependent)

    def test_target_ten_minute_mutation_can_only_change_observed(self) -> None:
        original = target_evidence()
        support_four = original[3]
        support_five = original[4]
        mutated = (
            *original[:3],
            replace(
                support_four,
                gold_at_10=support_five.gold_at_10,
                last_hits_at_10=support_five.last_hits_at_10,
                is_roaming=support_five.is_roaming,
                observer_wards_at_10=support_five.observer_wards_at_10,
                sentry_wards_at_10=support_five.sentry_wards_at_10,
                stacks_at_10=support_five.stacks_at_10,
            ),
            replace(
                support_five,
                gold_at_10=support_four.gold_at_10,
                last_hits_at_10=support_four.last_hits_at_10,
                is_roaming=support_four.is_roaming,
                observer_wards_at_10=support_four.observer_wards_at_10,
                sentry_wards_at_10=support_four.sentry_wards_at_10,
                stacks_at_10=support_four.stacks_at_10,
            ),
        )

        observed_before = assign_observed_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=OBSERVED_CUTOFF,
            players=original,
        )
        observed_after = assign_observed_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=OBSERVED_CUTOFF,
            players=mutated,
        )
        expected_before = assign_expected_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=EXPECTED_CUTOFF,
            player_ids=PLAYER_IDS,
        )
        expected_after = assign_expected_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=EXPECTED_CUTOFF,
            player_ids=PLAYER_IDS,
        )

        self.assertNotEqual(position_map(observed_before), position_map(observed_after))
        self.assertEqual(observed_before[3].purpose, RolePurpose.OBSERVED_POSITION)
        self.assertEqual(expected_before, expected_after)
        self.assertTrue(all(row.position is None for row in expected_before))

    def test_future_and_late_usable_history_cannot_enter_expected_position(self) -> None:
        late_usable = HistoricalPositionEvidence(
            player_id=101,
            match_id=7_001,
            position=1,
            confidence=1.0,
            completed_at=TARGET_START - timedelta(days=2),
            first_usable_at=EXPECTED_CUTOFF + timedelta(seconds=1),
        )
        future_match = HistoricalPositionEvidence(
            player_id=102,
            match_id=7_002,
            position=2,
            confidence=1.0,
            completed_at=TARGET_START + timedelta(seconds=1),
            first_usable_at=EXPECTED_CUTOFF - timedelta(days=1),
        )

        assignments = assign_expected_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=EXPECTED_CUTOFF,
            player_ids=PLAYER_IDS,
            history=(late_usable, future_match),
        )
        baseline = assign_expected_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=EXPECTED_CUTOFF,
            player_ids=PLAYER_IDS,
        )

        self.assertEqual(assignments, baseline)
        self.assertTrue(all(row.source is RoleSource.UNKNOWN for row in assignments))

    def test_pattern_uses_only_most_recent_twenty_eligible_maps(self) -> None:
        history = []
        for index in range(20):
            completed = TARGET_START - timedelta(days=index + 1)
            history.append(
                HistoricalPositionEvidence(
                    101, 7_100 + index, 1, 1.0, completed, completed
                )
            )
        for index in range(30):
            completed = TARGET_START - timedelta(days=index + 40)
            history.append(
                HistoricalPositionEvidence(
                    101, 6_000 + index, 2, 1.0, completed, completed
                )
            )

        assignments = assign_expected_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=EXPECTED_CUTOFF,
            player_ids=PLAYER_IDS,
            audited_roster=roster()[1:],
            history=tuple(reversed(history)),
        )

        carry = assignments[0]
        self.assertEqual(carry.position, 1)
        self.assertEqual(carry.source, RoleSource.HISTORICAL_PATTERN)
        self.assertEqual(len(carry.supporting_match_ids), 20)
        self.assertEqual(carry.confidence, 1.0)

    def test_final_gpm_permutation_changes_neither_assignment_nor_hash(self) -> None:
        original = target_evidence()
        permuted_gpm = tuple(
            replace(evidence, final_gpm=original[-index - 1].final_gpm)
            for index, evidence in enumerate(original)
        )

        before = assign_observed_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=OBSERVED_CUTOFF,
            players=original,
        )
        after = assign_observed_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=OBSERVED_CUTOFF,
            players=permuted_gpm,
        )

        self.assertEqual(before, after)

    def test_known_positions_are_unique_and_insufficient_players_are_unknown(self) -> None:
        usable = TARGET_START + timedelta(minutes=30)
        sparse = (
            SingleMapRoleEvidence(101, usable, lane_role=2),
            *(SingleMapRoleEvidence(player_id, usable) for player_id in PLAYER_IDS[1:]),
        )

        assignments = assign_observed_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=OBSERVED_CUTOFF,
            players=sparse,
        )

        known = [row.position for row in assignments if row.position is not None]
        self.assertEqual(len(assignments), 5)
        self.assertEqual(known, [2])
        self.assertEqual(len(known), len(set(known)))
        self.assertFalse(assignments[0].usable_for_role_dependent)
        self.assertTrue(all(row.source is RoleSource.UNKNOWN for row in assignments[1:]))

    def test_complete_single_map_assignment_is_one_to_one(self) -> None:
        assignments = assign_observed_positions(
            match_id=8_001,
            target_started_at=TARGET_START,
            cutoff=OBSERVED_CUTOFF,
            players=target_evidence(),
        )
        self.assertEqual(set(position_map(assignments).values()), {1, 2, 3, 4, 5})
        self.assertTrue(all(row.version == "role-assignment-v1" for row in assignments))
        self.assertTrue(all(len(row.input_hash) == 64 for row in assignments))


if __name__ == "__main__":
    unittest.main()
