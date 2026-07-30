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
    assert len(events) == 4
    assert registry.get_by_event_id("ewc-dota2-2026") is not None

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
    assert registry.candidates()[0].canonical_name == "Candidate Event Updated"

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
    assert intelligence["strict_event_count"] == 4
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
            "UPDATE event_registry SET canonical_name=canonical_name "
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
