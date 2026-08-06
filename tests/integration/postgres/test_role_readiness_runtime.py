from __future__ import annotations

from datetime import datetime, timezone

from event_intelligence.coverage import build_coverage_report
from event_intelligence.incremental import ROLE_VERSION
from event_intelligence.storage import IntelligenceStorage


def _coverage(storage: IntelligenceStorage) -> dict[str, object]:
    return build_coverage_report(
        storage.connection,
        database="integration-test",
        generated_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
    )


def test_coverage_exposes_current_role_readiness_and_missing_maps(
    postgres_engine,
) -> None:
    storage = IntelligenceStorage(engine=postgres_engine)
    storage.init_schema(seed_events=True)
    match_id = 9_930_001
    now = "2033-05-18T03:33:20+00:00"
    with storage.connection.transaction():
        storage.connection.execute(
            """INSERT INTO matches
               (match_id, radiant_team_id, dire_team_id, radiant_win,
                duration, start_time, leagueid)
               VALUES (?, 1, 2, TRUE, 1800, 2000000000, 19543)""",
            (match_id,),
        )
        storage.connection.execute(
            """INSERT INTO match_ingest_status
               (match_id, event_id, start_time, series_id, map_number,
                stage_scope, stage_in_scope, has_valid_result, is_exhibition,
                is_forfeit, is_void_remake, ingest_state, basic_result_state,
                detailed_parse_state, cross_check_state, reconciliation_status,
                missing_fields_json, raw_artifact_version, attempt_generation,
                retry_count, player_readiness, state_readiness, draft_readiness,
                discovered_at, updated_at)
               VALUES (?, 'ewc-dota2-2026', 2000000000, 9930, 1,
                       'main_event', 1, 1, 0, 0, 0, 'complete', 'ready',
                       'ready', 'ready', 'reconciled', '[]', 1, 1, 0,
                       'ready', 'ready', 'ready', ?, ?)""",
            (match_id, now, now),
        )

    missing = _coverage(storage)
    assert missing["formal_maps"] == 1
    assert missing["expected_role_ready_maps"] == 0
    assert missing["observed_role_ready_maps"] == 0
    assert missing["complete_position_maps"] == 0
    issue = next(row for row in missing["issues"] if row["match_id"] == match_id)
    assert issue["missing_expected_role_rows"] == 10
    assert issue["missing_observed_role_rows"] == 10
    assert issue["missing_position_values"] == 20

    with storage.connection.transaction():
        for index, slot in enumerate((0, 1, 2, 3, 4, 128, 129, 130, 131, 132)):
            for purpose in ("expected_position", "observed_position"):
                storage.connection.execute(
                    """INSERT INTO player_role_assignments
                       (match_id, player_slot, account_id, team_id, purpose,
                        position, assignment_source, confidence, input_cutoff,
                        input_hash, assignment_version, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'historical_pattern', 0.9,
                               ?, ?, ?, ?)""",
                    (
                        match_id,
                        slot,
                        10_000 + index,
                        1 if slot < 128 else 2,
                        purpose,
                        index % 5 + 1,
                        now,
                        f"{index:064x}",
                        ROLE_VERSION,
                        now,
                    ),
                )

    ready = _coverage(storage)
    assert ready["expected_role_ready_maps"] == 1
    assert ready["observed_role_ready_maps"] == 1
    assert ready["complete_position_maps"] == 1
    event = next(
        row for row in ready["events"] if row["event_id"] == "ewc-dota2-2026"
    )
    assert event["expected_role_ready_maps"] == 1
    assert event["observed_role_ready_maps"] == 1
    assert event["complete_position_maps"] == 1
    assert all(row["match_id"] != match_id for row in ready["issues"])
    storage.close()
