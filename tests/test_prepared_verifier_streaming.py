from __future__ import annotations
# ruff: noqa: E402

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

database_protocol = pytest.importorskip(
    "live_betting.database_protocol",
    reason="prepared SQLite verification was replaced by Alembic contract tests",
)
import live_betting.odds_response_verifier as odds_verifier
from live_betting.database_protocol import prepare_database, verify_prepared_database
from live_betting.markets import normalized_state_hash, snapshots_from_payload
from live_betting.odds_response_authority import legacy_normalized_state_identity_v1
from live_betting.storage import LiveBettingStore
from live_betting.vision_frame_registry import (
    publish_vision_frame_bytes,
    register_vision_frame_artifact,
    verify_vision_frame_registry,
)
from shared.sqlite import connect


NOW = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)


class _NoFetchAllCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    def fetchall(self) -> list[Any]:
        raise AssertionError("prepared verification must stream, not fetchall")

    def __iter__(self) -> _NoFetchAllCursor:
        return self

    def __next__(self) -> Any:
        return next(self._cursor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _NoFetchAllConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> _NoFetchAllCursor:
        return _NoFetchAllCursor(self._connection.execute(sql, parameters))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _authority_outcomes() -> tuple[tuple[Any, ...], ...]:
    return (
        (
            "winner-one",
            "winner",
            2.1,
            "1",
            "winner",
            "map_1",
            "team_one",
            None,
            "team_one",
            1,
            "provider-state",
        ),
        (
            "winner-two",
            "winner",
            1.8,
            "1",
            "winner",
            "map_1",
            "team_two",
            None,
            "team_two",
            1,
            "provider-state",
        ),
    )


def _seed_legacy_observations(
    connection: sqlite3.Connection,
    count: int,
    *,
    invalid: str | None = None,
) -> None:
    outcomes = _authority_outcomes()
    normalized_hash, _, _ = legacy_normalized_state_identity_v1(outcomes)
    connection.execute("DROP TRIGGER odds_transport_observations_require_v2_state")
    connection.execute("DROP TRIGGER odds_response_outcomes_legacy_insert_disabled")

    transport_rows: list[tuple[Any, ...]] = []
    outcome_rows: list[tuple[Any, ...]] = []
    for index in range(count):
        observation_key = f"legacy-{index:06d}"
        observed_at = (NOW + timedelta(seconds=index)).isoformat()
        stored_hash = "0" * 64 if invalid == "normalized_hash" else normalized_hash
        transport_rows.append((observation_key, observed_at, stored_hash))
        for outcome in outcomes:
            raw: str
            if invalid == "raw_json" and index == 0:
                raw = "{"
            else:
                raw_id = "wrong-id" if invalid == "raw_id" and index == 0 else outcome[0]
                raw = json.dumps(
                    {
                        "id": raw_id,
                        "odds": str(outcome[2]),
                        "status": 1,
                        "last_update": outcome[10],
                    },
                    separators=(",", ":"),
                )
            received_at = (
                (NOW + timedelta(days=1)).isoformat()
                if invalid == "binding" and index == 0
                else observed_at
            )
            outcome_rows.append(
                (
                    observation_key,
                    received_at,
                    *outcome,
                    raw,
                )
            )

    connection.executemany(
        """INSERT INTO odds_transport_observations
           (observation_key, source, source_event_id, raybet_match_id,
            observed_at, normalized_state_hash,
            normalized_state_hash_version,
            original_legacy_normalized_state_hash,
            response_state_hash, response_artifact_hash, timing_status,
            processing_status, normalized_change_count)
           VALUES (?, 'direct', NULL, '1001', ?, ?, 1, NULL, NULL, NULL,
                   'on_time', 'processed', 2)""",
        transport_rows,
    )
    connection.executemany(
        """INSERT INTO odds_response_outcomes
           (observation_key, raybet_match_id, received_at, odds_id,
            odds_group_id, price, status, market_type, period, side, line,
            outcome_key, supported, last_update, raw_json)
           VALUES (?, '1001', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        outcome_rows,
    )
    connection.commit()


def test_legacy_transport_query_keeps_only_one_observation_group(
    tmp_path: Path,
) -> None:
    database = tmp_path / "streaming.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
        _seed_legacy_observations(store.connection, 4096)
        groups = odds_verifier._transport_groups(
            _NoFetchAllConnection(store.connection)
        )

        group_count = 0
        max_group_size = 0
        for _, legacy_rows in groups:
            group_count += 1
            max_group_size = max(max_group_size, len(legacy_rows))

        assert group_count == 4096
        assert max_group_size == 2


def test_odds_authority_streams_large_legacy_fixture_without_fetchall(
    tmp_path: Path,
) -> None:
    database = tmp_path / "authority.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
        _seed_legacy_observations(store.connection, 4096)

        verified = odds_verifier.verify_odds_response_authority(
            _NoFetchAllConnection(store.connection),
            tmp_path / "missing-empty-raw-root",
        )

        assert verified.state_count == 0
        assert verified.transport_count == 4096
        assert verified.artifact_count == 0
        assert verified.legacy_transport_count == 4096


def test_odds_authority_streams_repeated_v2_artifact_state_bindings(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v2-authority.db"
    raw_root = tmp_path / "raw-v2"
    payload = {
        "result": {
            "id": "1001",
            "team": [
                {"team_id": 10, "team_name": "One", "pos": 1},
                {"team_id": 20, "team_name": "Two", "pos": 2},
            ],
            "odds": [
                {
                    "id": "winner-one",
                    "odds_group_id": "winner",
                    "team_id": 10,
                    "match_stage": "r1",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "odds": "2.10",
                    "status": 1,
                    "last_update": "provider-state",
                },
                {
                    "id": "winner-two",
                    "odds_group_id": "winner",
                    "team_id": 20,
                    "match_stage": "r1",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "odds": "1.80",
                    "status": 1,
                    "last_update": "provider-state",
                },
            ],
        }
    }
    with LiveBettingStore(database, raw_archive_root=raw_root) as store:
        store.init_schema()
        for index in range(512):
            observed_at = NOW + timedelta(seconds=index)
            snapshots = snapshots_from_payload(payload, received_at=observed_at)
            store.store_odds_observation(
                source="direct",
                observation_key=f"v2-{index:06d}",
                source_event_id=None,
                raybet_match_id="1001",
                observed_at=observed_at,
                normalized_state_hash=normalized_state_hash(snapshots),
                snapshots=snapshots,
                raw_payload=payload,
            )

        verified = odds_verifier.verify_odds_response_authority(
            _NoFetchAllConnection(store.connection),
            raw_root,
        )

        assert verified.state_count == 1
        assert verified.transport_count == 512
        assert verified.artifact_count == 1
        assert verified.legacy_transport_count == 0


@pytest.mark.parametrize(
    ("invalid", "message"),
    (
        ("binding", "legacy odds transport outcome binding mismatch: legacy-000000"),
        ("raw_json", "legacy odds transport raw member is invalid: legacy-000000"),
        ("raw_id", "legacy odds transport raw member id mismatch: legacy-000000"),
        (
            "normalized_hash",
            "legacy odds transport normalized hash mismatch: legacy-000000",
        ),
    ),
)
def test_streaming_legacy_verification_preserves_fail_closed_errors(
    tmp_path: Path,
    invalid: str,
    message: str,
) -> None:
    database = tmp_path / f"{invalid}.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
        _seed_legacy_observations(store.connection, 2, invalid=invalid)

        with pytest.raises(RuntimeError, match=message):
            odds_verifier.verify_odds_response_authority(
                _NoFetchAllConnection(store.connection),
                tmp_path / "missing-empty-raw-root",
            )


def test_vision_registry_streams_registry_and_relocation_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "vision.db"
    evidence_root = tmp_path / "live_evidence"
    with LiveBettingStore(database) as store:
        store.init_schema()
        receipt = publish_vision_frame_bytes(evidence_root, b"streamed-frame")
        assert register_vision_frame_artifact(
            store.connection,
            receipt,
            registered_at=NOW,
        )
        store.connection.commit()

        assert verify_vision_frame_registry(
            _NoFetchAllConnection(store.connection)
        ) == 1


def test_complete_prepared_verifier_never_calls_fetchall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "prepared.db"
    prepare_database(database, tmp_path / "backups", now=NOW)
    real_connect = database_protocol._connect_query_only

    def guarded_connect(path: Path) -> _NoFetchAllConnection:
        return _NoFetchAllConnection(real_connect(path))

    monkeypatch.setattr(database_protocol, "_connect_query_only", guarded_connect)

    verified = verify_prepared_database(database)

    assert verified.database == database.resolve()


def test_no_fetchall_proxy_delegates_fetchone(tmp_path: Path) -> None:
    connection = connect(tmp_path / "proxy.db")
    try:
        assert _NoFetchAllConnection(connection).execute("SELECT 1").fetchone()[0] == 1
    finally:
        connection.close()
