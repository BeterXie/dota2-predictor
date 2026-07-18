from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import live_betting.odds_legacy_compactor as odds_legacy_compactor
from live_betting.database_protocol import prepare_database
from live_betting.markets import (
    legacy_normalized_state_hash_v1,
    normalized_state_hash,
    snapshots_from_payload,
)
from live_betting.models import Market, OddsSnapshot
from live_betting.odds_legacy_compactor import compact_legacy_odds
from live_betting.odds_response_authority import (
    canonical_state_outcomes,
    response_artifact_identity,
    response_state_identity,
    snapshot_derived_payload,
)
from live_betting.storage import CURRENT_SCHEMA_VERSION, SCHEMA_SQL, LiveBettingStore
from shared.sqlite import connect, execute_script


NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)


def _prepare(path: Path) -> None:
    prepare_database(path, path.parent / "schema-backups", now=NOW)


def _outcome(
    odds_id: str,
    *,
    price: float,
    side: str,
    raw_variant: str = "stable",
    market_type: str = "winner",
) -> dict[str, object]:
    raw = {
        "id": odds_id,
        "odds": str(price),
        "status": 1,
        "last_update": "provider-stable",
        "variant": raw_variant,
        "padding": "x" * 8192,
    }
    return {
        "odds_id": odds_id,
        "odds_group_id": "winner",
        "price": price,
        "status": "1",
        "market_type": market_type,
        "period": "map_1",
        "side": side,
        "line": None,
        "outcome_key": side,
        "supported": 1,
        "last_update": "provider-stable",
        "raw": raw,
    }


def _state_hash(
    observed_at: datetime,
    outcomes: list[dict[str, object]],
) -> str:
    snapshots = [
        OddsSnapshot(
            raybet_match_id="1001",
            odds_id=str(row["odds_id"]),
            odds_group_id=str(row["odds_group_id"]),
            received_at=observed_at,
            price=float(row["price"]),
            status=row["status"],
            market=Market(
                str(row["market_type"]),
                str(row["period"]),
                str(row["side"]),
                None,
                str(row["outcome_key"]),
                True,
            ),
            last_update=str(row["last_update"]),
            raw=dict(row["raw"]),  # type: ignore[arg-type]
        )
        for row in outcomes
    ]
    return legacy_normalized_state_hash_v1(snapshots)


def _seed_legacy(
    database: Path,
    observations: list[tuple[str, datetime, list[dict[str, object]], str]],
) -> None:
    connection = connect(database)
    try:
        connection.execute(
            "DROP TRIGGER odds_transport_observations_require_v2_state"
        )
        connection.execute(
            "DROP TRIGGER odds_response_outcomes_legacy_insert_disabled"
        )
        for key, observed_at, outcomes, timing_status in observations:
            connection.execute(
                """INSERT INTO odds_transport_observations
                   (observation_key, source, source_event_id, raybet_match_id,
                    observed_at, normalized_state_hash, timing_status,
                    processing_status, normalized_change_count,
                    response_state_hash, response_artifact_hash)
                   VALUES (?, 'direct', NULL, '1001', ?, ?, ?, ?, 2, NULL, NULL)""",
                (
                    key,
                    observed_at.isoformat(),
                    _state_hash(observed_at, outcomes),
                    timing_status,
                    "audit_only" if timing_status == "late" else "processed",
                ),
            )
            for row in outcomes:
                connection.execute(
                    """INSERT INTO odds_response_outcomes
                       (observation_key, raybet_match_id, odds_id, odds_group_id,
                        received_at, price, status, market_type, period, side,
                        line, outcome_key, supported, last_update, raw_json)
                       VALUES (?, '1001', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        key,
                        row["odds_id"],
                        row["odds_group_id"],
                        observed_at.isoformat(),
                        row["price"],
                        row["status"],
                        row["market_type"],
                        row["period"],
                        row["side"],
                        row["line"],
                        row["outcome_key"],
                        row["supported"],
                        row["last_update"],
                        json.dumps(row["raw"], separators=(",", ":")),
                    ),
                )
        execute_script(connection, SCHEMA_SQL)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_authority_helper_matches_online_writer_bit_exact(tmp_path: Path) -> None:
    database = tmp_path / "authority.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
        outcomes = [
            _outcome("b", price=1.8, side="team_two"),
            _outcome("a", price=2.1, side="team_one"),
        ]
        snapshots = [
            OddsSnapshot(
                "1001",
                str(row["odds_id"]),
                str(row["odds_group_id"]),
                NOW,
                float(row["price"]),
                row["status"],
                Market(
                    str(row["market_type"]),
                    str(row["period"]),
                    str(row["side"]),
                    None,
                    str(row["outcome_key"]),
                    True,
                ),
                str(row["last_update"]),
                dict(row["raw"]),  # type: ignore[arg-type]
            )
            for row in outcomes
        ]
        normalized = normalized_state_hash(snapshots)
        writer_hash, writer_rows = store._response_state_identity(
            "1001", normalized, snapshots
        )
        helper_hash, helper_rows, _ = response_state_identity(
            "1001",
            normalized,
            [store._response_state_outcome_values(snapshot) for snapshot in snapshots],
        )
        assert helper_hash == writer_hash
        assert list(helper_rows) == writer_rows

        writer_artifact, writer_bytes, _ = store._response_artifact_identity(
            "1001", snapshots, raw_payload=None, raw_artifact=None
        )
        helper_artifact, helper_bytes, _ = response_artifact_identity(
            snapshot_derived_payload("1001", [snapshot.raw for snapshot in snapshots])
        )
        assert helper_artifact == writer_artifact
        assert helper_bytes == writer_bytes


def test_compactor_accepts_missing_raw_root_only_when_registry_is_empty(
    tmp_path: Path,
) -> None:
    database = tmp_path / "source.db"
    missing_raw_root = tmp_path / "missing-raw-v2"
    _prepare(database)

    result = compact_legacy_odds(
        database,
        missing_raw_root,
        tmp_path / "compaction",
    )

    assert result.artifact_count == 0
    assert result.raw_root.is_dir()


def test_compactor_rejects_missing_raw_root_with_registered_artifacts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "source.db"
    actual_raw_root = tmp_path / "actual-raw-v2"
    _prepare(database)
    payload: dict[str, object] = {
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
                    "last_update": "provider-stable",
                }
            ],
        }
    }
    snapshots = snapshots_from_payload(payload, received_at=NOW)
    with LiveBettingStore(database, raw_archive_root=actual_raw_root) as store:
        store.store_odds_observation(
            source="direct",
            observation_key="registered",
            source_event_id=None,
            raybet_match_id="1001",
            observed_at=NOW,
            normalized_state_hash=normalized_state_hash(snapshots),
            snapshots=snapshots,
            raw_payload=payload,
        )
        store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with pytest.raises(RuntimeError, match="raw artifact is missing"):
        compact_legacy_odds(
            database,
            tmp_path / "missing-raw-v2",
            tmp_path / "compaction",
        )


def test_compactor_deduplicates_state_and_raw_variance_without_changing_source(
    tmp_path: Path,
) -> None:
    database = tmp_path / "source.db"
    raw_root = tmp_path / "source-raw"
    raw_root.mkdir()
    _prepare(database)
    observations = []
    for index in range(300):
        observed_at = NOW + timedelta(seconds=index)
        variant = "changed-raw" if index == 299 else "stable"
        outcomes = [
            _outcome("winner-one", price=2.1, side="team_one", raw_variant=variant),
            _outcome("winner-two", price=1.8, side="team_two", raw_variant=variant),
        ]
        observations.append(
            (
                f"observation-{index:04d}",
                observed_at,
                outcomes,
                "late" if index == 298 else "on_time",
            )
        )
    _seed_legacy(database, observations)
    before_hash = _file_hash(database)
    before_size = database.stat().st_size

    result = compact_legacy_odds(database, raw_root, tmp_path / "compaction")

    assert _file_hash(database) == before_hash
    assert result.observation_count == 300
    assert result.outcome_count == 600
    assert result.state_count == 1
    assert result.artifact_count == 2
    assert result.output_bytes < before_size * 0.7
    connection = connect(result.output_database, read_only=True)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM odds_response_outcomes"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM odds_response_outcomes_effective"
        ).fetchone()[0] == 600
        late = connection.execute(
            """SELECT timing_status, processing_status
                 FROM odds_transport_observations
                WHERE observation_key='observation-0298'"""
        ).fetchone()
        assert tuple(late) == ("late", "audit_only")
        converted_hash = connection.execute(
            """SELECT normalized_state_hash, normalized_state_hash_version,
                      original_legacy_normalized_state_hash
                 FROM odds_transport_observations
                WHERE observation_key='observation-0000'"""
        ).fetchone()
        original_hash = _state_hash(NOW, observations[0][2])
        assert int(converted_hash[1]) == 2
        assert str(converted_hash[2]) == original_hash
        assert str(converted_hash[0]) != original_hash
    finally:
        connection.close()


def test_compactor_disables_unbounded_raw_archive_path_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "source.db"
    raw_root = tmp_path / "source-raw"
    raw_root.mkdir()
    _prepare(database)
    _seed_legacy(
        database,
        [
            (
                f"observation-{index:04d}",
                NOW + timedelta(seconds=index),
                [
                    _outcome(
                        "winner-one",
                        price=2.1,
                        side="team_one",
                        raw_variant=f"variant-{index}",
                    ),
                    _outcome(
                        "winner-two",
                        price=1.8,
                        side="team_two",
                        raw_variant=f"variant-{index}",
                    ),
                ],
                "on_time",
            )
            for index in range(64)
        ],
    )
    original_archive = odds_legacy_compactor.RawArchive
    instances: list[object] = []
    max_cache_entries = 0

    class NoCacheArchive(original_archive):
        def __init__(self, *args: object, **kwargs: object) -> None:
            assert kwargs.get("cache_paths") is False
            super().__init__(*args, **kwargs)
            instances.append(self)

        def archive_json(self, **kwargs: object):
            nonlocal max_cache_entries
            receipt = super().archive_json(**kwargs)
            max_cache_entries = max(max_cache_entries, len(self._known_paths))
            return receipt

    monkeypatch.setattr(odds_legacy_compactor, "RawArchive", NoCacheArchive)

    result = compact_legacy_odds(database, raw_root, tmp_path / "compaction")

    assert result.observation_count == 64
    assert result.artifact_count == 64
    assert instances
    assert max_cache_entries == 0


def test_compactor_interruption_is_resumable_and_never_publishes_partial_output(
    tmp_path: Path,
) -> None:
    database = tmp_path / "source.db"
    raw_root = tmp_path / "source-raw"
    raw_root.mkdir()
    _prepare(database)
    _seed_legacy(
        database,
        [
            (
                f"observation-{index}",
                NOW + timedelta(seconds=index),
                [
                    _outcome("winner-one", price=2.1, side="team_one"),
                    _outcome("winner-two", price=1.8, side="team_two"),
                ],
                "on_time",
            )
            for index in range(3)
        ],
    )
    source_hash = _file_hash(database)
    destination = tmp_path / "compaction"

    with pytest.raises(RuntimeError, match="injected compaction interruption"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _fail_after_observations=1,
        )
    assert not (destination / "dota2-compacted.db").exists()
    assert _file_hash(database) == source_hash
    manifest_path = destination / "compaction-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["live_schema_version"] == CURRENT_SCHEMA_VERSION

    result = compact_legacy_odds(database, raw_root, destination, resume=True)
    assert result.observation_count == 3
    assert result.outcome_count == 6
    assert _file_hash(database) == source_hash

    manifest["live_schema_version"] = CURRENT_SCHEMA_VERSION - 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="schema version"):
        compact_legacy_odds(database, raw_root, destination, resume=True)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_ready_resume_revalidates_every_registered_raw_artifact(
    tmp_path: Path,
    damage: str,
) -> None:
    database = tmp_path / "source.db"
    source_raw_root = tmp_path / "source-raw"
    source_raw_root.mkdir()
    _prepare(database)
    _seed_legacy(
        database,
        [
            (
                "observation",
                NOW,
                [
                    _outcome("winner-one", price=2.1, side="team_one"),
                    _outcome("winner-two", price=1.8, side="team_two"),
                ],
                "on_time",
            )
        ],
    )
    destination = tmp_path / "compaction"
    result = compact_legacy_odds(database, source_raw_root, destination)
    artifact = next(result.raw_root.rglob("*.json.gz"))
    if damage == "missing":
        artifact.unlink()
    else:
        artifact.write_bytes(b"corrupt")

    with pytest.raises(RuntimeError, match="raw artifact"):
        compact_legacy_odds(
            database,
            source_raw_root,
            destination,
            resume=True,
        )


def test_compactor_splits_v1_subset_collision_into_distinct_v2_states(
    tmp_path: Path,
) -> None:
    database = tmp_path / "source.db"
    raw_root = tmp_path / "source-raw"
    raw_root.mkdir()
    _prepare(database)
    first = [_outcome("winner-one", price=2.1, side="team_one")]
    conflicting = [
        _outcome(
            "winner-one",
            price=2.1,
            side="team_one",
            market_type="not-the-same-market",
        )
    ]
    assert _state_hash(NOW, first) == _state_hash(
        NOW + timedelta(seconds=1), conflicting
    )
    _seed_legacy(
        database,
        [
            ("first", NOW, first, "on_time"),
            ("second", NOW + timedelta(seconds=1), conflicting, "on_time"),
        ],
    )
    source_hash = _file_hash(database)

    result = compact_legacy_odds(database, raw_root, tmp_path / "compaction")
    assert _file_hash(database) == source_hash
    connection = connect(result.output_database, read_only=True)
    try:
        row = connection.execute(
            """SELECT COUNT(DISTINCT normalized_state_hash),
                      COUNT(DISTINCT original_legacy_normalized_state_hash),
                      MIN(normalized_state_hash_version)
                 FROM odds_transport_observations"""
        ).fetchone()
        assert tuple(row) == (2, 1, 2)
    finally:
        connection.close()


def test_compactor_rejects_true_v2_hash_collision_with_different_manifest(
    tmp_path: Path,
) -> None:
    database = tmp_path / "source.db"
    raw_root = tmp_path / "source-raw"
    raw_root.mkdir()
    _prepare(database)
    _seed_legacy(
        database,
        [
            ("first", NOW, [_outcome("winner-one", price=2.1, side="team_one")], "on_time"),
            (
                "second",
                NOW + timedelta(seconds=1),
                [_outcome("winner-one", price=2.2, side="team_one")],
                "on_time",
            ),
        ],
    )

    def forced_collision(outcomes):
        ordered = canonical_state_outcomes(outcomes)
        return "e" * 64, ordered, b"forced-normalized-collision"

    with (
        patch(
            "live_betting.markets.normalized_state_identity",
            side_effect=forced_collision,
        ),
        patch(
            "live_betting.odds_response_authority.normalized_state_identity",
            side_effect=forced_collision,
        ),
        pytest.raises(RuntimeError, match="different response manifest"),
    ):
        compact_legacy_odds(database, raw_root, tmp_path / "compaction")


def test_compactor_rejects_missing_or_mismatched_raw_member(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    raw_root = tmp_path / "source-raw"
    raw_root.mkdir()
    _prepare(database)
    _seed_legacy(
        database,
        [
            (
                "bad",
                NOW,
                [_outcome("winner-one", price=2.1, side="team_one")],
                "on_time",
            )
        ],
    )
    connection = connect(database)
    try:
        connection.execute("DROP TRIGGER odds_response_outcomes_immutable_update")
        raw = json.loads(
            connection.execute(
                "SELECT raw_json FROM odds_response_outcomes"
            ).fetchone()[0]
        )
        raw["id"] = "another-outcome"
        connection.execute(
            "UPDATE odds_response_outcomes SET raw_json=?",
            (json.dumps(raw, separators=(",", ":")),),
        )
        execute_script(connection, SCHEMA_SQL)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="raw (?:outcome|member) id"):
        compact_legacy_odds(database, raw_root, tmp_path / "compaction")
