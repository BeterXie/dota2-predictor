from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import DBAPIError

from event_intelligence.backtest import (
    draft_lineage_tracking_is_current,
    ensure_draft_lineage_tracking,
)
from event_intelligence.coverage import build_coverage_report
from event_intelligence.incremental import StrictDerivedPipeline
from event_intelligence.registry import EventRegistry
from event_intelligence.report import build_intelligence_report
from event_intelligence.storage import IntelligenceStorage


def test_event_registry_runs_on_postgres_and_remains_idempotent(
    postgres_engine,
) -> None:
    storage = IntelligenceStorage(engine=postgres_engine)
    storage.init_schema(seed_events=True)
    storage.init_schema(seed_events=True)

    registry = EventRegistry(storage)
    events = registry.formal_events()
    assert len(events) == 5
    assert registry.get_by_event_id("ewc-dota2-2026") is not None

    gotf_candidate_id = registry.discover_candidate(
        source="opendota_league_catalog",
        provider_event_id="19917",
        canonical_name="The Games of the Future 2026",
        evidence_urls=("https://www.opendota.com/leagues/19917",),
        discovered_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    registry.seed_approved_events()
    gotf_candidate = next(
        candidate
        for candidate in registry.candidates()
        if candidate.candidate_id == gotf_candidate_id
    )
    assert gotf_candidate.approval_status.value == "approved"
    assert gotf_candidate.promoted_event_id == "games-of-the-future-2026"

    candidate_id = registry.discover_candidate(
        source="opendota",
        provider_event_id="candidate-1",
        canonical_name="Candidate Event",
        evidence_urls=("https://example.invalid/event",),
        discovered_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    same_candidate_id = registry.discover_candidate(
        source="opendota",
        provider_event_id="candidate-1",
        canonical_name="Candidate Event Updated",
        evidence_urls=("https://example.invalid/event",),
        discovered_at=datetime(2026, 7, 30, 0, 1, tzinfo=timezone.utc),
    )

    assert same_candidate_id == candidate_id
    candidate = next(
        item for item in registry.candidates() if item.candidate_id == candidate_id
    )
    assert candidate.canonical_name == "Candidate Event Updated"

    report = build_coverage_report(
        storage.connection,
        database="integration-test",
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        include_integrity=True,
    )
    assert report["database"] == "integration-test"
    assert report["integrity_check"] == "ok"
    assert report["invalid_constraints"] == 0
    intelligence = build_intelligence_report(storage.connection)
    assert intelligence["strict_event_count"] == 5
    assert intelligence["formal_maps"] == 0

    ensure_draft_lineage_tracking(storage.connection)
    assert draft_lineage_tracking_is_current(storage.connection)
    before = storage.connection.execute(
        """SELECT dependency_revision, artifact_revision
             FROM draft_lineage_revisions WHERE singleton=1"""
    ).fetchone()
    assert before is not None
    with storage.connection.transaction():
        storage.connection.execute(
            "UPDATE event_registry SET canonical_name=canonical_name || ' Updated' "
            "WHERE event_id='ewc-dota2-2026'"
        )
        storage.connection.execute(
            """INSERT INTO draft_model_runs
               (run_id, model_version, model_kind, horizon_minutes,
                availability_mode, training_cutoff, feature_schema_hash,
                configuration_json, metrics_json, status, created_at)
               VALUES (?, ?, 'pure_draft', 10, 'prospective', ?, ?, '{}', NULL,
                       'ready', ?)""",
            (
                "integration-run",
                "integration-model",
                "2026-07-30T00:00:00+00:00",
                "a" * 64,
                "2026-07-30T00:00:00+00:00",
            ),
        )
    after = storage.connection.execute(
        """SELECT dependency_revision, artifact_revision
             FROM draft_lineage_revisions WHERE singleton=1"""
    ).fetchone()
    assert after is not None
    assert int(after[0]) == int(before[0]) + 1
    assert int(after[1]) == int(before[1]) + 1
    with pytest.raises(DBAPIError, match="draft lineage changes are immutable"):
        with storage.connection.transaction():
            storage.connection.execute(
                "UPDATE draft_lineage_changes SET source_relation='tampered' "
                "WHERE dependency_revision=1"
            )
    derived = StrictDerivedPipeline(storage).run()
    assert derived.pending_maps == 0
    assert derived.derived_maps == 0
    storage.close()


def test_draft_lineage_bounds_current_raw_observation_to_match_start(
    postgres_engine,
) -> None:
    storage = IntelligenceStorage(engine=postgres_engine)
    storage.init_schema(seed_events=True)
    match_id = 9_920_001
    start_time = 2_000_000_000
    artifact_id = "opendota:" + "b" * 64
    content_hash = "b" * 64
    now = "2033-05-18T03:33:20+00:00"

    before = storage.connection.execute(
        "SELECT dependency_revision FROM draft_lineage_revisions WHERE singleton=1"
    ).fetchone()
    assert before is not None
    with storage.connection.transaction():
        storage.connection.execute(
            """INSERT INTO raw_source_artifacts
               (artifact_id, content_hash, source, artifact_use, endpoint,
                sanitized_request_identity, storage_path, uncompressed_bytes,
                compressed_bytes, source_at, received_at, first_usable_at,
                schema_fingerprint, event_id, match_id, created_at)
               VALUES (?, ?, 'opendota', 'primary', ?, ?, ?, 10, 10, ?, ?, ?,
                       ?, 'ewc-dota2-2026', ?, ?)""",
            (
                artifact_id,
                content_hash,
                "https://api.opendota.com/api/matches/9920001",
                "https://api.opendota.com/api/matches/9920001",
                "C:/integration/raw/9920001.json.gz",
                now,
                now,
                now,
                "schema-v1",
                match_id,
                now,
            ),
        )
        storage.connection.execute(
            """INSERT INTO matches
               (match_id, radiant_team_id, dire_team_id, radiant_win,
                duration, start_time, leagueid)
               VALUES (?, 1, 2, TRUE, 1800, ?, 19543)""",
            (match_id, start_time),
        )
    unscoped = storage.connection.execute(
        "SELECT dependency_revision FROM draft_lineage_revisions WHERE singleton=1"
    ).fetchone()
    assert unscoped is not None
    assert int(unscoped[0]) == int(before[0])

    with storage.connection.transaction():
        storage.connection.execute(
            """INSERT INTO match_ingest_status
               (match_id, event_id, start_time, series_id, map_number,
                stage_scope, stage_in_scope, has_valid_result, is_exhibition,
                is_forfeit, is_void_remake, ingest_state, basic_result_state,
                detailed_parse_state, cross_check_state, reconciliation_status,
                missing_fields_json, latest_raw_artifact_id,
                latest_raw_content_hash, normalizer_version,
                raw_artifact_version, attempt_generation, retry_count,
                first_usable_at, player_readiness, state_readiness,
                draft_readiness, discovered_at, updated_at)
               VALUES (?, 'ewc-dota2-2026', ?, 9920, 1, 'main_event', 1, 1,
                       0, 0, 0, 'complete', 'ready', 'ready', 'ready',
                       'reconciled', '[]', ?, ?, 'opendota-exact-v1', 1, 1, 0,
                       ?, 'ready', 'ready', 'ready', ?, ?)""",
            (
                match_id,
                start_time,
                artifact_id,
                content_hash,
                now,
                now,
                now,
            ),
        )
    scoped = storage.connection.execute(
        "SELECT dependency_revision FROM draft_lineage_revisions WHERE singleton=1"
    ).fetchone()
    assert scoped is not None
    assert int(scoped[0]) == int(before[0]) + 1

    with storage.connection.transaction():
        storage.connection.execute(
            """INSERT INTO raw_source_observations
               (observation_id, artifact_id, content_hash, source, artifact_use,
                endpoint, sanitized_request_identity, source_at, received_at,
                first_usable_at, schema_fingerprint, event_id, match_id,
                http_status, created_at)
               VALUES ('observation-9920001', ?, ?, 'opendota', 'primary', ?, ?,
                       ?, ?, ?, ?, 'ewc-dota2-2026', ?, 200, ?)""",
            (
                artifact_id,
                content_hash,
                "https://api.opendota.com/api/matches/9920001",
                "https://api.opendota.com/api/matches/9920001",
                now,
                now,
                now,
                "schema-v1",
                match_id,
                now,
            ),
        )
    change = storage.connection.execute(
        """SELECT affected_from_unix, source_relation, operation
             FROM draft_lineage_changes
            ORDER BY dependency_revision DESC LIMIT 1"""
    ).fetchone()
    assert change is not None
    assert tuple(change) == (start_time, "raw_source_observations", "INSERT")
    storage.close()
