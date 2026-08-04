from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

import pytest
from sqlalchemy import text

from live_betting.draft_publisher import (
    append_prospective_outcomes,
    load_latest_frozen_deployment,
    ready_draft_anchors,
)
from live_betting.models import ProviderMatch
from live_betting.monitor import collect_completed_once
from live_betting.health import read_health, record_health
from live_betting.notifications import EVENT_MONITOR_ALERT, claim, enqueue
from live_betting.runtime_schema import verify_runtime_schema
from live_betting.postmatch_monitor import has_trusted_confirmed_draft
from live_betting.rosh_parity_storage import RoshRunRecord, RoshRunRepository
from live_betting.report import build_report
from live_betting.shadow_monitor import persist_alignments
from live_betting.storage import LiveBettingStore
from live_betting.strict_eligibility import init_strict_live_eligibility_schema


NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _match(match_id: str) -> ProviderMatch:
    return ProviderMatch(
        provider="opendota",
        provider_match_id=match_id,
        tournament="Integration Cup",
        team_one="Radiant",
        team_two="Dire",
        scheduled_at=NOW,
        best_of=3,
        status="scheduled",
        raw={"match_id": match_id},
    )


def test_live_betting_store_uses_postgres_transactions(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    store.init_schema()
    store.upsert_provider_match(_match("provider-1"), NOW)

    with postgres_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT tournament FROM provider_matches "
                "WHERE provider='opendota' AND provider_match_id='provider-1'"
            )
        ).scalar_one() == "Integration Cup"

    with pytest.raises(RuntimeError, match="rollback"):
        with store.transaction():
            store.upsert_provider_match(_match("provider-2"), NOW)
            raise RuntimeError("rollback")

    with postgres_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM provider_matches "
                "WHERE provider_match_id='provider-2'"
            )
        ).scalar_one() == 0
    store.close()


def test_runtime_services_use_alembic_managed_postgres(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    status = verify_runtime_schema(store.connection)
    assert status.version == 1
    init_strict_live_eligibility_schema(store.connection)

    record_health(
        store.connection,
        "integration-worker",
        "healthy",
        heartbeat_at=NOW,
        success_at=NOW,
        details={"database": "postgresql"},
    )
    health = read_health(store.connection)
    assert health == [
        {
            "component": "integration-worker",
            "status": "healthy",
            "last_heartbeat_at": NOW.isoformat(),
            "last_success_at": NOW.isoformat(),
            "last_error_at": None,
            "last_error": None,
            "details": {"database": "postgresql"},
            "updated_at": NOW.isoformat(),
        }
    ]

    assert store.reserve_map_attempt("raybet-1", 1, "order-1", "pending", NOW)
    assert not store.reserve_map_attempt(
        "raybet-1", 1, "order-1", "pending", NOW
    )
    store.close()


def test_notification_claim_skips_rows_locked_by_another_worker(
    postgres_engine,
    tmp_path,
) -> None:
    first = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw-first",
    )
    second = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw-second",
    )
    assert enqueue(
        first.connection,
        order_key="monitor:integration",
        event_type=EVENT_MONITOR_ALERT,
        payload={"category": "operational"},
        stats_cutoff_at=NOW,
        created_at=NOW,
    )
    outbox = first.connection.execute(
        "SELECT outbox_id FROM notification_outbox"
    ).fetchone()
    assert outbox is not None

    with first.transaction():
        first.connection.execute(
            "SELECT outbox_id FROM notification_outbox WHERE outbox_id=? FOR UPDATE",
            (int(outbox[0]),),
        ).fetchone()
        assert claim(second.connection, now=NOW) is None

    claimed = claim(second.connection, now=NOW)
    assert claimed is not None
    assert claimed.outbox_id == int(outbox[0])
    first.close()
    second.close()


def test_live_workers_execute_postgres_causal_queries(postgres_engine, tmp_path) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )

    assert persist_alignments(store, "missing-match", as_of=NOW) == 0
    assert not has_trusted_confirmed_draft(
        store.connection,
        "missing-match",
        1,
    )
    store.close()


def test_completed_collector_skips_persisted_final_odds(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    payload = {
        "result": {
            "id": "9001",
            "game_id": 151,
            "status": 3,
            "team": [
                {"team_id": 11, "pos": 1, "team_name": "Radiant Five"},
                {"team_id": 22, "pos": 2, "team_name": "Dire Five"},
            ],
            "odds": [
                {
                    "id": "winner-one",
                    "odds_group_id": "winner-map-1",
                    "team_id": 11,
                    "match_stage": "r1",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "odds": 1.8,
                    "status": 5,
                },
                {
                    "id": "winner-two",
                    "odds_group_id": "winner-map-1",
                    "team_id": 22,
                    "match_stage": "r1",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "odds": 2.0,
                    "status": 5,
                },
            ],
        }
    }

    class Client:
        calls = 0

        def match_odds(self, _match_id: str) -> dict[str, object]:
            self.calls += 1
            return payload

    client = Client()
    completed_rows = [{"id": "9001", "status": 3}]

    first = collect_completed_once(
        store,
        client,
        tmp_path / "raw",
        completed_rows=completed_rows,
        audit_match_list=False,
    )
    second = collect_completed_once(
        store,
        client,
        tmp_path / "raw",
        completed_rows=completed_rows,
        audit_match_list=False,
    )

    assert (first["matches"], first["skipped"]) == (1, 0)
    assert (second["matches"], second["skipped"]) == (0, 1)
    assert client.calls == 1
    assert store.connection.execute(
        """SELECT COUNT(*) FROM odds_transport_observations
            WHERE raybet_match_id='9001'"""
    ).fetchone()[0] == 1
    store.close()


def test_completed_collector_stops_retrying_stable_strict_rejection(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "9002"
    store.upsert_raybet_match(
        {
            "id": match_id,
            "game_id": 151,
            "status": 2,
            "team": [
                {"team_id": 11, "pos": 1, "team_name": "Radiant Five"},
                {"team_id": 22, "pos": 2, "team_name": "Dire Five"},
            ],
        },
        NOW,
    )
    rejected_payload = {
        "code": 200,
        "result": {
            "id": match_id,
            "game_id": 151,
            "status": 3,
            "odds": [{"status": 99, "odds": 0.01}],
        },
    }
    for index in range(3):
        receipt = store.archive_response_payload(
            rejected_payload,
            observed_at=NOW + timedelta(seconds=index),
            match_id=match_id,
            response_kind="completed_odds",
        )
        store.record_direct_response_audit(
            receipt,
            response_kind="completed_odds",
            claimed_raybet_match_id=match_id,
            observed_raybet_match_id=match_id,
            disposition="rejected",
            reason="validation_failed",
        )

    class Client:
        calls = 0

        def match_odds(self, _match_id: str) -> dict[str, object]:
            self.calls += 1
            raise AssertionError("terminal strict rejection must not be fetched again")

    client = Client()
    result = collect_completed_once(
        store,
        client,
        tmp_path / "raw",
        completed_rows=[{"id": match_id, "status": 3}],
        audit_match_list=False,
    )

    assert result == {
        "matches": 0,
        "listed": 1,
        "odds": 0,
        "changed": 0,
        "errors": 0,
        "skipped": 1,
    }
    assert client.calls == 0
    assert store.connection.execute(
        "SELECT status FROM raybet_matches WHERE raybet_match_id=?",
        (match_id,),
    ).fetchone()[0] == "3"
    store.close()


def test_rosh_repository_roundtrips_failed_run_on_postgres(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    run = RoshRunRecord(
        run_id=_hash("failed-run"),
        status="failed",
        mode="historical_match",
        match_id=8_904_419_709,
        date_time=1_784_485_548,
        draft_hash=_hash("draft"),
        draft={
            "radiant": [
                {"hero_id": position, "position_id": position}
                for position in range(1, 6)
            ],
            "dire": [
                {"hero_id": position + 5, "position_id": position}
                for position in range(1, 6)
            ],
        },
        rosh_profile_id="integration-profile",
        formula_version="integration-formula",
        request_profile_hash=_hash("request-profile"),
        upstream_bundle_hash=_hash("bundle"),
        scorer_source_hash=_hash("scorer"),
        canonical_profile_hash=_hash("canonical-profile"),
        serialization_version="rfc8785-jcs/v1",
        request_hash=_hash("request"),
        request_manifest={"schema": "integration/v1", "operations": []},
        response_manifest=(),
        evidence_hash=_hash("evidence"),
        collected_at=NOW.isoformat(),
        radiant_team_score=None,
        dire_team_score=None,
        relative_advantage=None,
        error_code="integration_failure",
    )
    repository = RoshRunRepository(store.connection)

    stored = repository.write_failed(run)

    assert stored.run == run
    assert repository.get(run.run_id) == stored
    store.close()


def test_draft_publisher_reads_empty_postgres_runtime(postgres_engine, tmp_path) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )

    assert load_latest_frozen_deployment(store.connection) is None
    assert ready_draft_anchors(store.connection) == ()
    assert append_prospective_outcomes(store.connection, created_at=NOW) == 0
    store.close()


def test_live_report_reads_empty_postgres_runtime(postgres_engine, tmp_path) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )

    report = build_report(store.connection)

    assert report["decision_count"] == 0
    assert report["raw_order_count"] == 0
    store.close()
