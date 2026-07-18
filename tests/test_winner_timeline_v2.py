from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live_betting.alignment import OddsAlignment
from live_betting.direct_response_audit import record_direct_request_failure
from live_betting.markets import normalized_state_hash, snapshots_from_payload
from live_betting.storage import LiveBettingStore
from web.monitoring import winner_timeline


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
MATCH_ID = "timeline-match"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LiveBettingStore]:
    instance = LiveBettingStore(tmp_path / "winner-timeline.db")
    instance.init_schema()
    try:
        yield instance
    finally:
        instance.close()


def _response_payload(
    *,
    one: float | None,
    two: float | None,
    period: str = "map_1",
    one_group: str = "winner-map_1",
    two_group: str = "winner-map_1",
) -> dict[str, object]:
    stage = f"r{period.removeprefix('map_')}"
    odds: list[dict[str, object]] = []
    for odds_id, team_id, price, group in (
        ("winner-one", 1, one, one_group),
        ("winner-two", 2, two, two_group),
    ):
        if price is None:
            continue
        odds.append(
            {
                "id": odds_id,
                "odds_group_id": group,
                "team_id": team_id,
                "match_stage": stage,
                "group_short_name": "Winner",
                "tag": "win",
                "odds": price,
                "status": 1,
                "last_update": f"{price:.3f}",
            }
        )
    return {
        "result": {
            "id": MATCH_ID,
            "game_id": 151,
            "team": [
                {"team_id": 1, "pos": 1, "team_name": "One"},
                {"team_id": 2, "pos": 2, "team_name": "Two"},
            ],
            "odds": odds,
        }
    }


def _store_response(
    store: LiveBettingStore,
    *,
    observation_key: str,
    observed_at: datetime,
    one: float | None,
    two: float | None,
    one_group: str = "winner-map_1",
    two_group: str = "winner-map_1",
) -> tuple[str, int]:
    payload = _response_payload(
        one=one,
        two=two,
        one_group=one_group,
        two_group=two_group,
    )
    snapshots = snapshots_from_payload(payload, received_at=observed_at)
    return store.store_odds_observation(
        source="direct",
        observation_key=observation_key,
        source_event_id=None,
        raybet_match_id=MATCH_ID,
        observed_at=observed_at,
        normalized_state_hash=normalized_state_hash(snapshots),
        snapshots=snapshots,
        raw_payload=payload,
    )


def _snapshot_id(
    store: LiveBettingStore,
    *,
    odds_id: str,
    observed_at: datetime,
) -> int:
    row = store.connection.execute(
        """SELECT id FROM odds_snapshots
            WHERE raybet_match_id=? AND odds_id=? AND received_at=?""",
        (MATCH_ID, odds_id, observed_at.isoformat()),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_single_side_change_builds_complete_point_and_keeps_exact_alignment(
    store: LiveBettingStore,
) -> None:
    first = NOW - timedelta(seconds=10)
    second = NOW - timedelta(seconds=5)
    _store_response(
        store,
        observation_key="response-1",
        observed_at=first,
        one=2.0,
        two=2.0,
    )
    _store_response(
        store,
        observation_key="response-2",
        observed_at=second,
        one=2.2,
        two=2.0,
    )
    changed_snapshot_id = _snapshot_id(
        store,
        odds_id="winner-one",
        observed_at=second,
    )
    store.insert_alignment(
        OddsAlignment(
            changed_snapshot_id,
            MATCH_ID,
            1,
            125,
            second - timedelta(seconds=1),
            "vision_predecessor",
            1.0,
            True,
            "ok",
        )
    )

    timeline = winner_timeline(store.connection, MATCH_ID)

    assert store.connection.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 3
    assert [point["observed_at"] for point in timeline] == [
        first.isoformat(),
        second.isoformat(),
    ]
    assert timeline[1]["prices"] == {"team_one": 2.2, "team_two": 2.0}
    assert timeline[1]["probabilities"]["team_one"] == pytest.approx(
        (1 / 2.2) / ((1 / 2.2) + (1 / 2.0))
    )
    assert timeline[1]["map_number"] == 1
    assert timeline[1]["game_clock_seconds"] == 125
    assert timeline[1]["alignment"] == {
        "method": "vision_predecessor",
        "lag_seconds": 1.0,
    }


def test_unchanged_transport_is_retained_without_reusing_old_alignment(
    store: LiveBettingStore,
) -> None:
    first = NOW - timedelta(seconds=5)
    _store_response(
        store,
        observation_key="response-1",
        observed_at=first,
        one=2.0,
        two=2.0,
    )
    store.insert_alignment(
        OddsAlignment(
            _snapshot_id(store, odds_id="winner-one", observed_at=first),
            MATCH_ID,
            1,
            60,
            first - timedelta(seconds=1),
            "vision_predecessor",
            1.0,
            True,
            "ok",
        )
    )
    _store_response(
        store,
        observation_key="response-2",
        observed_at=NOW,
        one=2.0,
        two=2.0,
    )

    timeline = winner_timeline(store.connection, MATCH_ID)

    assert store.connection.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 2
    assert len(timeline) == 2
    assert timeline[0]["game_clock_seconds"] == 60
    assert timeline[1]["prices"] == {"team_one": 2.0, "team_two": 2.0}
    assert timeline[1]["map_number"] is None
    assert timeline[1]["game_clock_seconds"] is None
    assert timeline[1]["alignment"] is None


def test_late_audit_only_response_is_excluded(store: LiveBettingStore) -> None:
    _store_response(
        store,
        observation_key="on-time-response",
        observed_at=NOW,
        one=2.0,
        two=2.0,
    )
    timing_status, change_count = _store_response(
        store,
        observation_key="late-response",
        observed_at=NOW - timedelta(seconds=5),
        one=8.0,
        two=8.0,
    )

    timeline = winner_timeline(store.connection, MATCH_ID)
    late = store.connection.execute(
        """SELECT timing_status, processing_status
             FROM odds_transport_observations WHERE observation_key='late-response'"""
    ).fetchone()

    assert timing_status == "late"
    assert change_count == 0
    assert tuple(late) == ("late", "audit_only")
    assert store.connection.execute(
        """SELECT COUNT(*) FROM odds_response_outcomes_effective
            WHERE observation_key='late-response'"""
    ).fetchone()[0] == 2
    assert [point["observed_at"] for point in timeline] == [NOW.isoformat()]


def test_failed_direct_response_does_not_add_a_product_point(
    store: LiveBettingStore,
) -> None:
    _store_response(
        store,
        observation_key="valid-response",
        observed_at=NOW,
        one=2.0,
        two=2.0,
    )
    before = winner_timeline(store.connection, MATCH_ID)

    record_direct_request_failure(
        store,
        response_kind="live_odds",
        claimed_raybet_match_id=MATCH_ID,
        error=TimeoutError("provider timeout"),
        observed_at=NOW + timedelta(seconds=5),
    )

    assert winner_timeline(store.connection, MATCH_ID) == before
    assert store.connection.execute(
        """SELECT COUNT(*) FROM direct_response_audit
            WHERE claimed_raybet_match_id=? AND disposition='rejected'""",
        (MATCH_ID,),
    ).fetchone()[0] == 1


def test_responses_and_market_groups_are_never_cross_paired(
    store: LiveBettingStore,
) -> None:
    _store_response(
        store,
        observation_key="one-only",
        observed_at=NOW - timedelta(seconds=5),
        one=2.0,
        two=None,
    )
    _store_response(
        store,
        observation_key="two-only",
        observed_at=NOW - timedelta(seconds=5),
        one=None,
        two=2.0,
    )
    _store_response(
        store,
        observation_key="different-groups",
        observed_at=NOW,
        one=2.0,
        two=2.0,
        one_group="group-a",
        two_group="group-b",
    )

    assert winner_timeline(store.connection, MATCH_ID) == []


def test_v2_schema_never_falls_back_to_change_snapshots(
    store: LiveBettingStore,
) -> None:
    payload = _response_payload(one=2.0, two=2.0)
    snapshots = snapshots_from_payload(payload, received_at=NOW)
    for snapshot in snapshots:
        store.insert_odds(snapshot)
    store.connection.commit()

    assert winner_timeline(store.connection, MATCH_ID) == []


def test_legacy_only_schema_falls_back_without_alignment_table() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE odds_snapshots (
               id INTEGER PRIMARY KEY,
               raybet_match_id TEXT NOT NULL,
               odds_id TEXT NOT NULL,
               odds_group_id TEXT,
               received_at TEXT NOT NULL,
               price REAL NOT NULL,
               status TEXT,
               market_type TEXT NOT NULL,
               period TEXT NOT NULL,
               side TEXT,
               supported INTEGER NOT NULL
           )"""
    )
    rows = snapshots_from_payload(
        _response_payload(one=2.0, two=2.0),
        received_at=NOW,
    )
    connection.executemany(
        """INSERT INTO odds_snapshots
           (id, raybet_match_id, odds_id, odds_group_id, received_at, price,
            status, market_type, period, side, supported)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                index,
                row.raybet_match_id,
                row.odds_id,
                row.odds_group_id,
                row.received_at.isoformat(),
                row.price,
                str(row.status),
                row.market.market_type,
                row.market.period,
                row.market.side,
                int(row.market.supported),
            )
            for index, row in enumerate(rows, start=1)
        ],
    )

    timeline = winner_timeline(connection, MATCH_ID)

    assert len(timeline) == 1
    assert timeline[0]["probabilities"] == {"team_one": 0.5, "team_two": 0.5}
    assert timeline[0]["alignment"] is None

    connection.execute(
        """CREATE TABLE odds_transport_observations (
               observation_key TEXT PRIMARY KEY
           )"""
    )
    assert winner_timeline(connection, MATCH_ID) == []
    connection.close()
