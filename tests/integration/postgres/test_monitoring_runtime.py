from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from database.session import PostgresSession
from live_betting.markets import normalized_state_hash, snapshots_from_payload
from live_betting.health import record_health
from live_betting.storage import LiveBettingStore
from web import queries
from web.app import app
from web.control import ControlService
from web.monitoring import (
    build_monitor_snapshot,
    monitor_history_page,
    monitor_match_detail,
    monitor_matches,
    winner_timeline,
)


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def _raybet_match(match_id: str, *, status: int, scheduled_at: datetime) -> dict:
    return {
        "id": match_id,
        "tournament_name": "PostgreSQL Integration Cup",
        "start_time": scheduled_at.isoformat(),
        "round": "bo3",
        "status": status,
        "team": [
            {"id": 11, "pos": 1, "team_name": "Radiant Five"},
            {"id": 22, "pos": 2, "team_name": "Dire Five"},
        ],
    }


def test_monitor_list_detail_and_history_use_postgres(postgres_engine, tmp_path) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    store.upsert_raybet_match(
        _raybet_match("live-match", status=2, scheduled_at=NOW - timedelta(hours=1)),
        NOW,
    )
    store.upsert_raybet_match(
        _raybet_match(
            "history-match",
            status=3,
            scheduled_at=NOW - timedelta(days=2),
        ),
        NOW - timedelta(days=1),
    )
    store.connection.commit()

    matches = monitor_matches(store.connection, now=NOW)
    assert any(item["raybet_match_id"] == "live-match" for item in matches)

    detail = monitor_match_detail(store.connection, "live-match", now=NOW)
    assert detail is not None
    assert detail["raybet_match_id"] == "live-match"
    assert detail["lifecycle"] in {"live", "degraded"}

    history = monitor_history_page(store.connection, now=NOW)
    assert [item["raybet_match_id"] for item in history["items"]] == [
        "history-match"
    ]
    store.close()


def test_near_start_prematch_snapshot_stays_visible_without_live_promotion(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "near-start-prematch"
    observed_at = NOW - timedelta(minutes=4)
    payload = _raybet_match(
        match_id,
        status=1,
        scheduled_at=NOW + timedelta(minutes=5),
    )
    payload["team"] = [
        {"team_id": 11, "pos": 1, "team_name": "Radiant Five"},
        {"team_id": 22, "pos": 2, "team_name": "Dire Five"},
    ]
    payload["odds"] = [
        {
            "id": "prematch-winner-one",
            "odds_group_id": "prematch-winner-map-1",
            "team_id": 11,
            "match_stage": "r1",
            "group_short_name": "Winner",
            "tag": "win",
            "odds": 1.65,
            "status": 1,
        },
        {
            "id": "prematch-winner-two",
            "odds_group_id": "prematch-winner-map-1",
            "team_id": 22,
            "match_stage": "r1",
            "group_short_name": "Winner",
            "tag": "win",
            "odds": 2.19,
            "status": 1,
        },
    ]
    store.upsert_raybet_match(payload, observed_at)
    response_payload = {"result": payload}
    snapshots = snapshots_from_payload(
        response_payload,
        received_at=observed_at,
    )
    store.store_odds_observation(
        source="direct",
        observation_key="near-start-prematch-response",
        source_event_id=None,
        raybet_match_id=match_id,
        observed_at=observed_at,
        normalized_state_hash=normalized_state_hash(snapshots),
        snapshots=snapshots,
        raw_payload=response_payload,
        audit_only=True,
    )

    detail = monitor_match_detail(store.connection, match_id, now=NOW)

    assert detail is not None
    assert detail["lifecycle"] == "degraded"
    assert detail["winner"] is None
    assert detail["prematch_winner"] == {
        "observed_at": observed_at.isoformat(),
        "period": "map_1",
        "complete": True,
        "prices": {"team_one": 1.65, "team_two": 2.19},
        "probabilities": {
            "team_one": 0.5703125,
            "team_two": 0.4296875,
        },
    }
    assert detail["readiness"]["odds"]["status"] == "missing"
    assert detail["markets"] == []
    assert detail["winner_timeline"] == []
    assert store.connection.execute(
        "SELECT COUNT(*) FROM odds_snapshots WHERE raybet_match_id=?",
        (match_id,),
    ).scalar_one() == 0
    assert store.connection.execute(
        """SELECT processing_status FROM odds_transport_observations
            WHERE raybet_match_id=?""",
        (match_id,),
    ).scalar_one() == "audit_only"
    store.close()


def test_monitor_api_uses_postgres_session(postgres_engine, tmp_path, monkeypatch) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    store.upsert_raybet_match(
        _raybet_match("api-match", status=2, scheduled_at=NOW - timedelta(hours=1)),
        NOW,
    )
    store.connection.commit()
    store.close()

    monkeypatch.setattr(queries, "get_db", lambda: PostgresSession(postgres_engine))

    with TestClient(app) as client:
        bootstrap = client.get("/api/monitor/bootstrap")
        detail = client.get("/api/monitor/matches/api-match")

    assert bootstrap.status_code == 200
    assert any(
        item["raybet_match_id"] == "api-match"
        for item in bootstrap.json()["matches"]
    )
    assert detail.status_code == 200
    assert detail.json()["raybet_match_id"] == "api-match"


def test_control_uses_supervisor_heartbeat_and_configuration_authority(
    postgres_engine,
    tmp_path,
) -> None:
    connection = PostgresSession(postgres_engine)
    heartbeat = datetime.now(timezone.utc)
    record_health(
        connection,
        "raybet_worker",
        "degraded",
        heartbeat_at=heartbeat,
        error_at=heartbeat,
        error="upstream_degraded",
    )
    record_health(
        connection,
        "mail_delivery",
        "degraded",
        heartbeat_at=heartbeat,
        error_at=heartbeat,
        error="configuration_missing",
        details={"smtp_configured": False},
    )

    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError(
            "fresh supervisor heartbeat must block a duplicate process"
        )

    service = ControlService(
        project_dir=tmp_path,
        popen_factory=unexpected_spawn,
    )

    statuses = {
        item["component"]: item for item in service.statuses(connection)
    }
    start = service.execute(
        connection,
        component="raybet_collector",
        action="start",
        request_id="supervisor-authority-start",
        client_host="127.0.0.1",
    )

    assert statuses["raybet_collector"]["status"] == "running"
    assert statuses["raybet_collector"]["pid"] is None
    assert statuses["raybet_collector"]["started_at"] is not None
    assert statuses["raybet_collector"]["detail"] == "由 Supervisor 托管"
    assert statuses["raybet_collector"]["control_allowed"] is False
    assert statuses["mail_worker"]["status"] == "stopped"
    assert statuses["mail_worker"]["detail"] == "未配置"
    assert statuses["mail_worker"]["control_allowed"] is False
    assert start["ok"] is False
    assert start["status"] == "running"
    assert start["detail"] == "由 Supervisor 托管"
    service.close()
    connection.close()


def test_ended_match_timeline_collapses_unchanged_final_transport(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "ended-timeline-match"
    first = NOW - timedelta(minutes=10)
    second = NOW - timedelta(minutes=5)
    payload = _raybet_match(
        match_id,
        status=3,
        scheduled_at=NOW - timedelta(hours=2),
    )
    payload["team"] = [
        {"team_id": 11, "pos": 1, "team_name": "Radiant Five"},
        {"team_id": 22, "pos": 2, "team_name": "Dire Five"},
    ]
    payload["odds"] = [
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
    ]
    store.upsert_raybet_match(payload, first)
    response_payload = {"result": payload}
    for index, observed_at in enumerate((first, second), start=1):
        snapshots = snapshots_from_payload(
            response_payload,
            received_at=observed_at,
        )
        store.store_odds_observation(
            source="direct",
            observation_key=f"ended-response-{index}",
            source_event_id=None,
            raybet_match_id=match_id,
            observed_at=observed_at,
            normalized_state_hash=normalized_state_hash(snapshots),
            snapshots=snapshots,
            raw_payload=response_payload,
        )

    timeline = winner_timeline(store.connection, match_id)
    summary = next(
        item
        for item in monitor_matches(store.connection, now=NOW)
        if item["raybet_match_id"] == match_id
    )
    detail = monitor_match_detail(store.connection, match_id, now=NOW)

    assert [point["observed_at"] for point in timeline] == [first.isoformat()]
    assert timeline[0]["prices"] == {"team_one": 1.8, "team_two": 2.0}
    assert summary["winner"]["observed_at"] == first.isoformat()
    assert detail is not None
    assert detail["winner"]["observed_at"] == first.isoformat()
    store.close()


def test_stale_prematch_leaves_live_view_after_four_hours(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    stale_at = NOW - timedelta(hours=5)
    store.upsert_raybet_match(
        _raybet_match(
            "stale-prematch",
            status=1,
            scheduled_at=stale_at,
        ),
        stale_at,
    )
    store.connection.commit()

    snapshot = build_monitor_snapshot(store.connection, now=NOW)
    match = next(
        item
        for item in snapshot["matches"]
        if item["raybet_match_id"] == "stale-prematch"
    )

    assert match["lifecycle"] == "degraded"
    assert match["history_eligible"] is True
    history_ids = {
        item["raybet_match_id"]
        for item in monitor_history_page(store.connection, now=NOW)["items"]
    }
    assert "stale-prematch" in history_ids
    store.close()


def test_unconfigured_mail_group_does_not_inflate_abnormal_count(
    postgres_engine,
) -> None:
    connection = PostgresSession(postgres_engine)
    baseline = build_monitor_snapshot(connection, now=NOW)["summary"][
        "unhealthy_components"
    ]
    record_health(
        connection,
        "mail",
        "unhealthy",
        heartbeat_at=NOW,
        error_at=NOW,
        error="heartbeat_expired",
    )
    record_health(
        connection,
        "mail_delivery",
        "degraded",
        heartbeat_at=NOW,
        error_at=NOW,
        error="configuration_missing",
        details={"smtp_configured": False},
    )

    summary = build_monitor_snapshot(connection, now=NOW)["summary"]

    assert summary["unhealthy_components"] == baseline
    connection.close()
