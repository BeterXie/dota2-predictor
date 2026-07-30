from __future__ import annotations
# ruff: noqa: E402

import gzip
import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

hash_authority = pytest.importorskip(
    "live_betting.hash_authority",
    reason="legacy SQLite odds compaction is no longer a runtime feature",
)
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
from live_betting.service_coordination import (
    ProcessIdentity,
    SingleInstanceLock,
    WriterScanResult,
    database_authority_lock_paths,
)
from live_betting.storage import CURRENT_SCHEMA_VERSION, SCHEMA_SQL, LiveBettingStore
from shared.sqlite import connect, execute_script


NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)


def _safe_writer_scan(_: Path) -> WriterScanResult:
    return WriterScanResult((), ())


def _work_database_path(destination: Path) -> Path:
    return destination / odds_legacy_compactor._WORK_DATABASE_PATH


def _initializing_work_database_path(destination: Path) -> Path:
    return destination / odds_legacy_compactor._INITIALIZING_WORK_DATABASE_PATH


def _prepare(path: Path) -> None:
    prepare_database(path, path.parent / "schema-backups", now=NOW)


def test_artifact_verification_uses_stable_descriptor_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"trusted": True, "values": [1, 2, 3]}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    compressed = gzip.compress(canonical, mtime=0)
    artifact = tmp_path / "artifact.json.gz"
    artifact.write_bytes(compressed)

    def forbidden_read_bytes(_: Path) -> bytes:
        raise AssertionError("artifact verification reopened the pathname")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)

    verified = odds_legacy_compactor._verify_artifact_file(
        artifact,
        content_hash=hashlib.sha256(canonical).hexdigest(),
        uncompressed_bytes=len(canonical),
        compressed_bytes=len(compressed),
        fingerprint=odds_legacy_compactor.schema_fingerprint(payload),
    )

    assert verified == canonical


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


def _logical_database_bytes(path: Path) -> int:
    connection = connect(path, read_only=True)
    try:
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        return page_count * page_size
    finally:
        connection.close()


def _register_raw_artifact(
    database: Path,
    artifact_hash: str,
    storage_path: str,
    compressed_bytes: int,
) -> None:
    connection = connect(database)
    try:
        connection.execute(
            """INSERT INTO odds_raw_artifacts
               (artifact_hash, source, storage_path, uncompressed_bytes,
                compressed_bytes, schema_fingerprint)
               VALUES (?, 'raybet', ?, 2, ?, ?)""",
            (artifact_hash, storage_path, compressed_bytes, "f" * 64),
        )
        connection.commit()
    finally:
        connection.close()


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
            _writer_scanner=_safe_writer_scan,
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

    result = compact_legacy_odds(
        database,
        raw_root,
        tmp_path / "compaction",
        _writer_scanner=_safe_writer_scan,
    )

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
    monkeypatch.setattr(odds_legacy_compactor, "_COMMIT_BATCH_SIZE", 1)

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
    work = _work_database_path(destination)
    authority = manifest["work_database_authority"]
    metadata = work.stat()
    assert authority["resolved_path"] == str(work.resolve())
    assert authority["device"] == metadata.st_dev
    assert authority["inode"] == metadata.st_ino
    assert authority["bytes"] == metadata.st_size
    assert authority["sha256"] == _file_hash(work)
    assert authority["hash_phase"] == "failed:converting"
    manifest["completed_observations"] = 999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = compact_legacy_odds(database, raw_root, destination, resume=True)
    assert result.observation_count == 3
    assert result.outcome_count == 6
    assert _file_hash(database) == source_hash
    ready_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert ready_manifest["completed_observations"] == 3

    ready_manifest["live_schema_version"] = CURRENT_SCHEMA_VERSION - 1
    manifest_path.write_text(json.dumps(ready_manifest), encoding="utf-8")
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

    result = compact_legacy_odds(
        database,
        raw_root,
        tmp_path / "compaction",
        _writer_scanner=_safe_writer_scan,
    )
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


def _single_legacy_source(root: Path) -> tuple[Path, Path]:
    database = root / "source.db"
    raw_root = root / "source-raw"
    raw_root.mkdir()
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
    return database, raw_root


def test_fresh_space_preflight_reserves_conversion_vacuum_and_generated_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "source.db"
    _prepare(database)
    _register_raw_artifact(database, "a" * 64, "raybet/aa/source.json.gz", 37)
    destination = tmp_path / "compaction"
    destination.mkdir()
    work_database = destination / "compaction-work.db"
    output_database = destination / "dota2-compacted.db"
    raw_root = destination / "live_betting" / "raw-v2"
    margin = 11
    logical_bytes = _logical_database_bytes(database)
    required = 5 * logical_bytes + 37 + margin
    available = required - 1
    monkeypatch.setattr(odds_legacy_compactor, "_SAFETY_MARGIN_BYTES", margin)
    monkeypatch.setattr(
        odds_legacy_compactor.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(free=available),
    )

    with pytest.raises(
        RuntimeError,
        match=f"required_bytes={required}, available_bytes={available}",
    ):
        odds_legacy_compactor._preflight_space(
            database,
            destination,
            work_database=work_database,
            output_database=output_database,
            raw_root=raw_root,
        )

    monkeypatch.setattr(
        odds_legacy_compactor.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(free=required),
    )
    odds_legacy_compactor._preflight_space(
        database,
        destination,
        work_database=work_database,
        output_database=output_database,
        raw_root=raw_root,
    )


def test_resume_space_preflight_counts_only_missing_and_unallocated_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "source.db"
    _prepare(database)
    source_paths = (
        ("a" * 64, "raybet/aa/source-a.json.gz", 40),
        ("b" * 64, "raybet/bb/source-b.json.gz", 60),
    )
    for artifact_hash, storage_path, compressed_bytes in source_paths:
        _register_raw_artifact(
            database,
            artifact_hash,
            storage_path,
            compressed_bytes,
        )

    destination = tmp_path / "compaction"
    destination.mkdir()
    work_database = destination / "compaction-work.db"
    output_database = destination / "dota2-compacted.db"
    raw_root = destination / "live_betting" / "raw-v2"
    odds_legacy_compactor.online_backup(database, work_database)
    generated_path = "raybet/cc/generated.json.gz"
    _register_raw_artifact(work_database, "c" * 64, generated_path, 25)
    present_source = raw_root / source_paths[0][1]
    present_source.parent.mkdir(parents=True)
    present_source.write_bytes(b"s" * source_paths[0][2])
    present_generated = raw_root / generated_path
    present_generated.parent.mkdir(parents=True)
    present_generated.write_bytes(b"g" * 25)

    margin = 11
    work_allocated = max(
        work_database.stat().st_size,
        _logical_database_bytes(database),
    )
    missing_source_raw = source_paths[1][2]
    required = work_allocated + missing_source_raw + margin
    available = required - 1
    monkeypatch.setattr(odds_legacy_compactor, "_SAFETY_MARGIN_BYTES", margin)
    monkeypatch.setattr(
        odds_legacy_compactor.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(free=available),
    )

    with pytest.raises(
        RuntimeError,
        match=f"required_bytes={required}, available_bytes={available}",
    ):
        odds_legacy_compactor._preflight_space(
            database,
            destination,
            work_database=work_database,
            output_database=output_database,
            raw_root=raw_root,
        )

    monkeypatch.setattr(
        odds_legacy_compactor.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(free=required),
    )
    odds_legacy_compactor._preflight_space(
        database,
        destination,
        work_database=work_database,
        output_database=output_database,
        raw_root=raw_root,
    )


def test_source_hardlink_is_rejected_before_destination_initialization(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    alias = tmp_path / "source-alias.db"
    try:
        os.link(database, alias)
    except OSError as error:
        pytest.skip(f"filesystem does not support hardlinks: {error}")
    destination = tmp_path / "compaction"
    try:
        with pytest.raises(RuntimeError, match="exactly one hard link"):
            compact_legacy_odds(
                database,
                raw_root,
                destination,
                _writer_scanner=_safe_writer_scan,
            )
    finally:
        alias.unlink(missing_ok=True)

    assert not destination.exists()


def test_source_identity_is_revalidated_after_service_lock(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    probe = tmp_path / "hardlink-probe.db"
    try:
        os.link(database, probe)
    except OSError as error:
        pytest.skip(f"filesystem does not support hardlinks: {error}")
    probe.unlink()
    alias = tmp_path / "source-alias.db"
    destination = tmp_path / "compaction"

    class LinkAfterServiceLock:
        def __init__(self, path: Path) -> None:
            self.lock = SingleInstanceLock(path)

        def __enter__(self) -> LinkAfterServiceLock:
            self.lock.__enter__()
            if self.lock.path == database.with_suffix(".service.lock"):
                os.link(database, alias)
            return self

        def __exit__(self, *args: object) -> None:
            alias.unlink(missing_ok=True)
            self.lock.__exit__(*args)

    with pytest.raises(RuntimeError, match="exactly one hard link"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _writer_scanner=_safe_writer_scan,
            _lock_factory=LinkAfterServiceLock,
        )

    assert not destination.exists()


def test_destination_lock_rejects_a_concurrent_compactor(tmp_path: Path) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    destination.mkdir()

    with SingleInstanceLock(destination / ".compaction.lock"):
        with pytest.raises(RuntimeError, match="already held"):
            compact_legacy_odds(
                database,
                raw_root,
                destination,
                _writer_scanner=_safe_writer_scan,
            )

    assert not (destination / "compaction-manifest.json").exists()
    assert not _work_database_path(destination).exists()


def test_compactor_holds_source_output_and_destination_authority_for_lifetime(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    output = destination / "dota2-compacted.db"
    expected_locks = (
        *database_authority_lock_paths(database),
        *database_authority_lock_paths(output),
        destination / ".compaction.lock",
    )
    checked: list[Path] = []

    def check_locks(phase: str) -> None:
        if phase != "initializing_manifest_written":
            return
        for lock_path in expected_locks:
            with pytest.raises(RuntimeError, match="already held"):
                with SingleInstanceLock(lock_path):
                    pass
            checked.append(lock_path)

    compact_legacy_odds(
        database,
        raw_root,
        destination,
        _phase_hook=check_locks,
        _writer_scanner=_safe_writer_scan,
    )

    assert checked == list(expected_locks)
    for lock_path in expected_locks:
        with SingleInstanceLock(lock_path):
            pass


def test_destination_cannot_be_nested_under_the_source_raw_tree(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = raw_root / "compaction"

    with pytest.raises(ValueError, match="outside source paths"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _writer_scanner=_safe_writer_scan,
        )

    assert not destination.exists()


def test_stale_lock_files_do_not_block_compaction(tmp_path: Path) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    destination.mkdir()
    (destination / ".compaction.lock").write_text("dead-pid", encoding="ascii")
    database.with_suffix(".service.lock").write_text("dead-pid", encoding="ascii")

    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        _writer_scanner=_safe_writer_scan,
    )

    assert result.observation_count == 1


def test_source_service_lock_and_writer_scan_fail_before_initialization(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    locked_destination = tmp_path / "locked-compaction"
    with SingleInstanceLock(database.with_suffix(".service.lock")):
        with pytest.raises(RuntimeError, match="already held"):
            compact_legacy_odds(
                database,
                raw_root,
                locked_destination,
                _writer_scanner=_safe_writer_scan,
            )
    assert not (locked_destination / "compaction-manifest.json").exists()

    conflict_destination = tmp_path / "conflict-compaction"
    conflict = WriterScanResult((ProcessIdentity(4321, 1_700_000_000.0),), ())
    with pytest.raises(RuntimeError, match="4321"):
        compact_legacy_odds(
            database,
            raw_root,
            conflict_destination,
            _writer_scanner=lambda _: conflict,
        )
    assert not (conflict_destination / "compaction-manifest.json").exists()

    opaque_destination = tmp_path / "opaque-compaction"
    opaque = WriterScanResult((), (9876,))
    with pytest.raises(RuntimeError, match="9876"):
        compact_legacy_odds(
            database,
            raw_root,
            opaque_destination,
            _writer_scanner=lambda _: opaque,
        )
    assert not (opaque_destination / "compaction-manifest.json").exists()


def test_initializing_manifest_precedes_backup_and_resume_rebuilds_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"

    def fail_backup(_: Path, temporary: Path, **__: object) -> None:
        manifest = json.loads(
            (destination / "compaction-manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["status"] == "initializing"
        assert manifest["phase"] == "initializing_backup"
        temporary.write_bytes(b"partial backup")
        raise RuntimeError("injected backup failure")

    with monkeypatch.context() as context:
        context.setattr(odds_legacy_compactor, "online_backup", fail_backup)
        with pytest.raises(RuntimeError, match="injected backup failure"):
            compact_legacy_odds(
                database,
                raw_root,
                destination,
                _writer_scanner=_safe_writer_scan,
            )

    failed = json.loads(
        (destination / "compaction-manifest.json").read_text(encoding="utf-8")
    )
    assert failed["status"] == "failed"
    assert failed["phase"] == "initializing_backup"
    assert not _work_database_path(destination).exists()
    assert _initializing_work_database_path(destination).is_file()

    with monkeypatch.context() as context:
        context.setattr(
            odds_legacy_compactor.shutil,
            "disk_usage",
            lambda _: SimpleNamespace(free=0),
        )

        def unexpected_backup(_: Path, __: Path, **___: object) -> None:
            raise AssertionError("resume backup ran before its space preflight")

        context.setattr(odds_legacy_compactor, "online_backup", unexpected_backup)
        with pytest.raises(RuntimeError, match="insufficient free space"):
            compact_legacy_odds(
                database,
                raw_root,
                destination,
                resume=True,
                _writer_scanner=_safe_writer_scan,
            )

    assert not _initializing_work_database_path(destination).exists()
    assert not _work_database_path(destination).exists()

    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        resume=True,
        _writer_scanner=_safe_writer_scan,
    )
    assert result.observation_count == 1
    assert not _initializing_work_database_path(destination).exists()


@pytest.mark.parametrize(
    "failure_phase",
    [
        "work_database_authority_checkpointed",
        "work_database_replaced",
        "validated_manifest_written",
        "final_schema_committed",
        "vacuum_completed",
        "publishing_manifest_written",
        "output_replaced",
    ],
)
def test_compactor_resumes_every_durable_publication_boundary(
    tmp_path: Path,
    failure_phase: str,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    source_hash = _file_hash(database)

    def interrupt(phase: str) -> None:
        if phase == failure_phase:
            raise RuntimeError(f"injected {phase} interruption")

    with pytest.raises(RuntimeError, match=failure_phase):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _phase_hook=interrupt,
            _writer_scanner=_safe_writer_scan,
        )

    failed_manifest = json.loads(
        (destination / "compaction-manifest.json").read_text(encoding="utf-8")
    )
    work = _work_database_path(destination)
    authority_file = (
        _initializing_work_database_path(destination)
        if failure_phase == "work_database_authority_checkpointed"
        else work
    )
    authority = failed_manifest["work_database_authority"]
    metadata = authority_file.stat()
    assert authority["resolved_path"] == str(work.resolve())
    assert authority["device"] == metadata.st_dev
    assert authority["inode"] == metadata.st_ino
    assert authority["bytes"] == metadata.st_size
    assert authority["sha256"] == _file_hash(authority_file)
    if failure_phase in {
        "work_database_authority_checkpointed",
        "work_database_replaced",
    }:
        assert authority["hash_phase"] == "work_publish_pending"
    else:
        assert str(authority["hash_phase"]).startswith("failed:")

    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        resume=True,
        _writer_scanner=_safe_writer_scan,
    )
    manifest = json.loads(
        (destination / "compaction-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "ready"
    assert manifest["phase"] == "ready"
    assert _file_hash(database) == source_hash
    assert _file_hash(result.output_database) == result.output_sha256
    assert not work.exists()
    assert not _initializing_work_database_path(destination).exists()


@pytest.mark.parametrize(
    ("failure_phase", "authority_name"),
    [
        (
            "work_database_authority_checkpointed",
            ".compaction-work.db.initializing",
        ),
        ("work_database_replaced", "compaction-work.db"),
    ],
)
def test_pending_work_publication_rejects_same_bytes_with_another_identity(
    tmp_path: Path,
    failure_phase: str,
    authority_name: str,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"

    def interrupt(phase: str) -> None:
        if phase == failure_phase:
            raise RuntimeError(f"injected {failure_phase} interruption")

    with pytest.raises(RuntimeError, match=failure_phase):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _phase_hook=interrupt,
            _writer_scanner=_safe_writer_scan,
        )

    authority_file = destination / odds_legacy_compactor._WORK_ROOT / authority_name
    displaced = tmp_path / f"displaced-{authority_name.lstrip('.')}"
    replacement = tmp_path / f"replacement-{authority_name.lstrip('.')}"
    os.replace(authority_file, displaced)
    shutil.copy2(displaced, replacement)
    os.replace(replacement, authority_file)
    assert _file_hash(authority_file) == _file_hash(displaced)
    assert authority_file.stat().st_ino != displaced.stat().st_ino

    with pytest.raises(RuntimeError, match="identity changed"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    assert _file_hash(displaced) == _file_hash(authority_file)
    assert not (destination / "dota2-compacted.db").exists()


def _interrupt_at_validation(
    database: Path,
    raw_root: Path,
    destination: Path,
) -> None:
    def interrupt(phase: str) -> None:
        if phase == "validation_started":
            raise RuntimeError("stable validation interruption")

    with pytest.raises(RuntimeError, match="stable validation interruption"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _phase_hook=interrupt,
            _writer_scanner=_safe_writer_scan,
        )


def test_stable_resume_rejects_in_place_work_database_tamper(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    _interrupt_at_validation(database, raw_root, destination)
    work = _work_database_path(destination)
    with work.open("r+b") as handle:
        handle.seek(min(4096, work.stat().st_size - 1))
        original = handle.read(1)
        handle.seek(-1, os.SEEK_CUR)
        handle.write(bytes([original[0] ^ 0x01]))
    tampered = _file_hash(work)

    with pytest.raises(RuntimeError, match="hash differs from checkpoint"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    assert _file_hash(work) == tampered
    assert not (destination / "dota2-compacted.db").exists()


def test_stable_resume_rejects_nonempty_work_transaction_sidecar(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    _interrupt_at_validation(database, raw_root, destination)
    work = _work_database_path(destination)
    wal = Path(f"{work}-wal")
    wal.write_bytes(b"uncheckpointed-work")

    with pytest.raises(RuntimeError, match="transactional sidecars"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    assert wal.read_bytes() == b"uncheckpointed-work"
    assert not (destination / "dota2-compacted.db").exists()


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_compactor_rejects_nonempty_source_transaction_sidecar(
    tmp_path: Path,
    suffix: str,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    sidecar = Path(f"{database}{suffix}")
    sidecar.write_bytes(b"uncheckpointed-source")
    destination = tmp_path / "compaction"

    with pytest.raises(RuntimeError, match="transactional sidecars"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _writer_scanner=_safe_writer_scan,
        )

    assert sidecar.read_bytes() == b"uncheckpointed-source"
    assert not (destination / "compaction-manifest.json").exists()


def test_compactor_rejects_committed_wal_only_output_candidate(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"

    def inject_wal_only_data(phase: str) -> None:
        if phase != "output_verified":
            return
        candidate = destination / ".dota2-compacted.db.vacuuming"
        writer = connect(candidate, wal=True)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        main_bytes = candidate.read_bytes()
        writer.execute("CREATE TABLE wal_only_output_tamper(value INTEGER)")
        writer.execute("INSERT INTO wal_only_output_tamper VALUES (1)")
        writer.commit()
        assert candidate.read_bytes() == main_bytes
        wal = Path(f"{candidate}-wal")
        wal_bytes = wal.read_bytes()
        assert wal_bytes
        writer.close()
        candidate.write_bytes(main_bytes)
        wal.write_bytes(wal_bytes)

    with pytest.raises(RuntimeError, match="transactional sidecars"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _phase_hook=inject_wal_only_data,
            _writer_scanner=_safe_writer_scan,
        )

    assert not (destination / "dota2-compacted.db").exists()


@pytest.mark.parametrize(
    ("phase", "database_name"),
    [
        ("output_verified", ".dota2-compacted.db.vacuuming"),
        ("publishing_manifest_written", ".dota2-compacted.db.vacuuming"),
        ("output_replaced", "dota2-compacted.db"),
        ("published_output_verified", "dota2-compacted.db"),
    ],
)
def test_compactor_rechecks_transaction_sidecars_at_publication_boundaries(
    tmp_path: Path,
    phase: str,
    database_name: str,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"

    def inject_journal(current: str) -> None:
        if current == phase:
            Path(f"{destination / database_name}-journal").write_bytes(
                b"unpublished-transaction"
            )

    with pytest.raises(RuntimeError, match="transactional sidecars"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _phase_hook=inject_journal,
            _writer_scanner=_safe_writer_scan,
        )

    manifest = json.loads(
        (destination / "compaction-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "failed"
    assert manifest["phase"] != "ready"


def test_ready_compactor_ignores_and_clears_shm_content(tmp_path: Path) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        _writer_scanner=_safe_writer_scan,
    )
    shm = Path(f"{result.output_database}-shm")
    shm.write_bytes(b"coordination-only-state")

    resumed = compact_legacy_odds(
        database,
        raw_root,
        destination,
        resume=True,
        _writer_scanner=_safe_writer_scan,
    )

    assert resumed.output_sha256 == result.output_sha256
    assert not shm.exists()


def test_ready_completion_removes_work_database_and_resumes_without_it(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"

    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        _writer_scanner=_safe_writer_scan,
    )

    work = _work_database_path(destination)
    for suffix in ("", "-wal", "-shm", "-journal"):
        assert not Path(f"{work}{suffix}").exists()

    resumed = compact_legacy_odds(
        database,
        raw_root,
        destination,
        resume=True,
        _writer_scanner=_safe_writer_scan,
    )

    assert resumed == result
    for suffix in ("", "-wal", "-shm", "-journal"):
        assert not Path(f"{work}{suffix}").exists()


@pytest.mark.parametrize("sidecar_bytes", [b"", b"later-sidecar-state"])
def test_ready_resume_preserves_sidecar_when_authorized_work_is_missing(
    tmp_path: Path,
    sidecar_bytes: bytes,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        _writer_scanner=_safe_writer_scan,
    )
    work = _work_database_path(destination)
    assert not work.exists()
    sidecar = Path(f"{work}-wal")
    sidecar.write_bytes(sidecar_bytes)

    with pytest.raises(RuntimeError, match="missing while sidecars remain"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    assert result.output_database.is_file()
    assert sidecar.read_bytes() == sidecar_bytes


@pytest.mark.parametrize("tampered_authority", ["output", "source"])
def test_ready_resume_rejects_authority_tamper_after_work_cleanup(
    tmp_path: Path,
    tampered_authority: str,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        _writer_scanner=_safe_writer_scan,
    )
    work = _work_database_path(destination)
    assert not work.exists()

    tampered = result.output_database if tampered_authority == "output" else database
    connection = connect(tampered)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("CREATE TABLE ready_authority_tamper(value INTEGER)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    assert not work.exists()


def test_legacy_ready_manifest_reuses_output_and_preserves_unbound_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    real_cleanup = odds_legacy_compactor._remove_ready_work_database

    def interrupt_cleanup(_: Path, __: object) -> None:
        raise RuntimeError("injected ready cleanup interruption")

    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        interrupt_cleanup,
    )
    with pytest.raises(RuntimeError, match="ready cleanup interruption"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _writer_scanner=_safe_writer_scan,
        )

    manifest_path = destination / "compaction-manifest.json"
    current = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert current["status"] == "ready"
    work = _work_database_path(destination)
    assert work.is_file()
    legacy_fields = (
        "format",
        "status",
        "source_database",
        "source_raw_root",
        "source_sha256",
        "source_bytes",
        "live_schema_version",
        "work_database",
        "raw_root",
        "output_database",
        "completed_observations",
        "observation_count",
        "outcome_count",
        "equivalence_sha256",
        "state_count",
        "artifact_count",
        "output_sha256",
        "output_bytes",
    )
    legacy_ready = {field: current[field] for field in legacy_fields}
    manifest_path.write_text(json.dumps(legacy_ready), encoding="utf-8")
    Path(f"{work}-wal").touch()
    Path(f"{work}-shm").write_bytes(b"coordination-only-state")
    Path(f"{work}-journal").touch()
    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        real_cleanup,
    )

    def unexpected_recompaction(phase: str) -> None:
        raise AssertionError(f"legacy ready resume attempted phase {phase}")

    resumed = compact_legacy_odds(
        database,
        raw_root,
        destination,
        resume=True,
        _phase_hook=unexpected_recompaction,
        _writer_scanner=_safe_writer_scan,
    )

    assert resumed.output_database == destination / "dota2-compacted.db"
    assert _file_hash(resumed.output_database) == resumed.output_sha256
    pending_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert pending_manifest["cleanup_pending"] is True
    assert pending_manifest["cleanup_pending_reason"] == (
        "work_database_authority_missing"
    )
    for suffix in ("", "-wal", "-shm", "-journal"):
        assert Path(f"{work}{suffix}").exists()


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
def test_ready_cleanup_rejects_nonempty_work_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    real_cleanup = odds_legacy_compactor._remove_ready_work_database

    def interrupt_cleanup(_: Path, __: object) -> None:
        raise RuntimeError("retain ready work")

    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        interrupt_cleanup,
    )
    with pytest.raises(RuntimeError, match="retain ready work"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _writer_scanner=_safe_writer_scan,
        )
    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        real_cleanup,
    )

    work = _work_database_path(destination)
    work_hash = _file_hash(work)
    sidecar = Path(f"{work}{suffix}")
    sidecar.write_bytes(b"uncheckpointed-ready-state")

    with pytest.raises(RuntimeError, match="sidecar|authority"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    assert _file_hash(work) == work_hash
    assert sidecar.read_bytes() == b"uncheckpointed-ready-state"


@pytest.mark.parametrize("attack", ["nonempty-replacement", "hardlink"])
def test_ready_cleanup_rechecks_sidecar_authority_immediately_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    real_cleanup = odds_legacy_compactor._remove_ready_work_database

    def interrupt_cleanup(_: Path, __: object) -> None:
        raise RuntimeError("retain ready work")

    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        interrupt_cleanup,
    )
    with pytest.raises(RuntimeError, match="retain ready work"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _writer_scanner=_safe_writer_scan,
        )
    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        real_cleanup,
    )

    work = _work_database_path(destination)
    work_hash = _file_hash(work)
    sidecar = Path(f"{work}-wal")
    sidecar.touch()
    displaced = tmp_path / "snapshotted-sidecar"
    alias = tmp_path / "sidecar-alias"
    original_require = odds_legacy_compactor._require_recorded_sidecar_authority
    injected = False

    def race_after_snapshot(record):
        nonlocal injected
        path = Path(str(record["path"]))
        if not injected and path == sidecar:
            injected = True
            if attack == "nonempty-replacement":
                os.replace(sidecar, displaced)
                sidecar.write_bytes(b"replacement-transaction")
            else:
                try:
                    os.link(sidecar, alias)
                except OSError as error:
                    pytest.skip(f"filesystem does not support hardlinks: {error}")
        return original_require(record)

    monkeypatch.setattr(
        odds_legacy_compactor,
        "_require_recorded_sidecar_authority",
        race_after_snapshot,
    )
    try:
        with pytest.raises(RuntimeError, match="sidecar authority"):
            compact_legacy_odds(
                database,
                raw_root,
                destination,
                resume=True,
                _writer_scanner=_safe_writer_scan,
            )
    finally:
        alias.unlink(missing_ok=True)

    assert _file_hash(work) == work_hash
    assert sidecar.exists()
    if attack == "nonempty-replacement":
        assert sidecar.read_bytes() == b"replacement-transaction"
        assert displaced.is_file()


def test_ready_cleanup_quarantines_but_never_deletes_replacement_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    real_cleanup = odds_legacy_compactor._remove_ready_work_database

    def interrupt_cleanup(_: Path, __: object) -> None:
        raise RuntimeError("retain ready work")

    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        interrupt_cleanup,
    )
    with pytest.raises(RuntimeError, match="retain ready work"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _writer_scanner=_safe_writer_scan,
        )
    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        real_cleanup,
    )

    work = _work_database_path(destination)
    original_hash = _file_hash(work)
    displaced = tmp_path / "authorized-work.db"
    replacement_bytes = b"replacement-that-must-remain-quarantined"
    real_replace = odds_legacy_compactor._replace_and_fsync
    attacked = False

    def replace_at_quarantine(source: Path, target: Path) -> None:
        nonlocal attacked
        if source == work and ".quarantine." in target.name and not attacked:
            attacked = True
            os.replace(work, displaced)
            work.write_bytes(replacement_bytes)
        real_replace(source, target)

    monkeypatch.setattr(
        odds_legacy_compactor,
        "_replace_and_fsync",
        replace_at_quarantine,
    )

    with pytest.raises(RuntimeError, match="quarantined.*authority changed"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    quarantined = list(
        _work_database_path(destination).parent.glob(
            ".compaction-work.db.quarantine.*"
        )
    )
    assert attacked
    assert _file_hash(displaced) == original_hash
    assert not work.exists()
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == replacement_bytes


def test_legacy_ready_preserves_replacement_work_without_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    real_cleanup = odds_legacy_compactor._remove_ready_work_database

    def interrupt_cleanup(_: Path, __: object) -> None:
        raise RuntimeError("retain ready work")

    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        interrupt_cleanup,
    )
    with pytest.raises(RuntimeError, match="retain ready work"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _writer_scanner=_safe_writer_scan,
        )
    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        real_cleanup,
    )

    manifest_path = destination / "compaction-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("work_database_authority")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    work = _work_database_path(destination)
    displaced = tmp_path / "displaced-work.db"
    os.replace(work, displaced)
    replacement_bytes = b"ordinary-file-that-must-not-be-deleted"
    work.write_bytes(replacement_bytes)

    resumed = compact_legacy_odds(
        database,
        raw_root,
        destination,
        resume=True,
        _writer_scanner=_safe_writer_scan,
    )

    assert resumed.output_database.is_file()
    assert work.read_bytes() == replacement_bytes
    assert displaced.is_file()
    pending = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert pending["cleanup_pending"] is True
    assert pending["cleanup_pending_reason"] == "work_database_authority_missing"


def test_legacy_ready_preserves_sidecars_when_unbound_work_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    real_cleanup = odds_legacy_compactor._remove_ready_work_database

    def interrupt_cleanup(_: Path, __: object) -> None:
        raise RuntimeError("retain ready work")

    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        interrupt_cleanup,
    )
    with pytest.raises(RuntimeError, match="retain ready work"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _writer_scanner=_safe_writer_scan,
        )
    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        real_cleanup,
    )

    manifest_path = destination / "compaction-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("work_database_authority")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    work = _work_database_path(destination)
    displaced = tmp_path / "legacy-unbound-work.db"
    os.replace(work, displaced)
    sidecar = Path(f"{work}-wal")
    sidecar.touch()

    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        resume=True,
        _writer_scanner=_safe_writer_scan,
    )

    assert result.output_database.is_file()
    assert not work.exists()
    assert sidecar.exists()
    assert displaced.is_file()
    pending = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert pending["cleanup_pending"] is True


def test_legacy_ready_source_mismatch_fails_before_cleanup_pending_or_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    real_cleanup = odds_legacy_compactor._remove_ready_work_database

    def interrupt_cleanup(_: Path, __: object) -> None:
        raise RuntimeError("retain ready work")

    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        interrupt_cleanup,
    )
    with pytest.raises(RuntimeError, match="retain ready work"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _writer_scanner=_safe_writer_scan,
        )
    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        real_cleanup,
    )

    manifest_path = destination / "compaction-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("work_database_authority")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    work = _work_database_path(destination)
    work_hash = _file_hash(work)
    connection = connect(database)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("CREATE TABLE source_changed_after_ready(value INTEGER)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="belongs to another source"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    assert _file_hash(work) == work_hash
    unchanged_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "cleanup_pending" not in unchanged_manifest


@pytest.mark.parametrize("attack", ["replace", "hardlink", "symlink"])
def test_compactor_rejects_manifest_authority_change_after_checkpoint(
    tmp_path: Path,
    attack: str,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    manifest_path = destination / "compaction-manifest.json"
    displaced = tmp_path / "displaced-manifest.json"
    alias = tmp_path / "manifest-alias.json"

    def attack_manifest(phase: str) -> None:
        if phase != "initializing_manifest_written":
            return
        if attack == "replace":
            os.replace(manifest_path, displaced)
            shutil.copy2(displaced, manifest_path)
        elif attack == "hardlink":
            try:
                os.link(manifest_path, alias)
            except OSError as error:
                pytest.skip(f"filesystem does not support hardlinks: {error}")
        else:
            os.replace(manifest_path, displaced)
            try:
                os.symlink(displaced, manifest_path)
            except OSError as error:
                os.replace(displaced, manifest_path)
                pytest.skip(f"filesystem does not support symlinks: {error}")

    try:
        with pytest.raises(RuntimeError):
            compact_legacy_odds(
                database,
                raw_root,
                destination,
                _phase_hook=attack_manifest,
                _writer_scanner=_safe_writer_scan,
            )
    finally:
        alias.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "field",
    [
        "work_database",
        "raw_root",
        "output_database",
        "observation_count",
        "outcome_count",
        "state_count",
        "artifact_count",
        "equivalence_sha256",
    ],
)
def test_ready_manifest_revalidates_layout_and_summary_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    real_cleanup = odds_legacy_compactor._remove_ready_work_database

    def interrupt_cleanup(_: Path, __: object) -> None:
        raise RuntimeError("retain ready work")

    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        interrupt_cleanup,
    )
    with pytest.raises(RuntimeError, match="retain ready work"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _writer_scanner=_safe_writer_scan,
        )

    manifest_path = destination / "compaction-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if field in {"work_database", "raw_root", "output_database"}:
        manifest[field] = "tampered-path"
    elif field == "equivalence_sha256":
        manifest[field] = "0" * 64
    else:
        manifest[field] = int(manifest[field]) + 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        real_cleanup,
    )

    with pytest.raises(RuntimeError):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    assert _work_database_path(destination).is_file()


def test_ready_cleanup_rejects_work_symlink_without_touching_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    real_cleanup = odds_legacy_compactor._remove_ready_work_database

    def interrupt_cleanup(_: Path, __: object) -> None:
        raise RuntimeError("retain ready work")

    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        interrupt_cleanup,
    )
    with pytest.raises(RuntimeError, match="retain ready work"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _writer_scanner=_safe_writer_scan,
        )
    monkeypatch.setattr(
        odds_legacy_compactor,
        "_remove_ready_work_database",
        real_cleanup,
    )

    work = _work_database_path(destination)
    external = tmp_path / "external-work.db"
    os.replace(work, external)
    external_sidecar = Path(f"{external}-journal")
    external_sidecar.write_bytes(b"must remain")
    try:
        os.symlink(external, work)
    except OSError as error:
        os.replace(external, work)
        pytest.skip(f"filesystem does not support symlinks: {error}")

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    assert work.is_symlink()
    assert external.read_bytes()
    assert external_sidecar.read_bytes() == b"must remain"


def test_resume_rejects_external_work_database_hardlink_without_writing_it(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    _interrupt_at_validation(database, raw_root, destination)
    work = _work_database_path(destination)
    alias = tmp_path / "external-work-alias.db"
    try:
        os.link(work, alias)
    except OSError as error:
        pytest.skip(f"filesystem does not support hardlinks: {error}")
    before = _file_hash(alias)
    try:
        with pytest.raises(RuntimeError, match="exactly one hard link"):
            compact_legacy_odds(
                database,
                raw_root,
                destination,
                resume=True,
                _writer_scanner=_safe_writer_scan,
            )
        assert _file_hash(alias) == before
    finally:
        alias.unlink(missing_ok=True)

    assert not (destination / "dota2-compacted.db").exists()


def test_resume_rejects_replaced_work_without_writing_displaced_database(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    _interrupt_at_validation(database, raw_root, destination)
    work = _work_database_path(destination)
    displaced = tmp_path / "external-displaced-work.db"
    replacement = tmp_path / "replacement-work.db"
    os.replace(work, displaced)
    shutil.copy2(displaced, replacement)
    os.replace(replacement, work)
    before = _file_hash(displaced)

    with pytest.raises(RuntimeError, match="identity changed"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    assert _file_hash(displaced) == before
    assert not (destination / "dota2-compacted.db").exists()


def test_processing_resume_accepts_commit_before_manifest_checkpoint(
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
                f"observation-{index}",
                NOW + timedelta(seconds=index),
                [
                    _outcome(
                        f"winner-{index}",
                        price=2.0 + index / 10,
                        side="team_one",
                    )
                ],
                "on_time",
            )
            for index in range(3)
        ],
    )
    destination = tmp_path / "compaction"
    monkeypatch.setattr(odds_legacy_compactor, "_COMMIT_BATCH_SIZE", 1)
    with pytest.raises(RuntimeError, match="injected compaction interruption"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _fail_after_observations=1,
            _writer_scanner=_safe_writer_scan,
        )
    manifest_path = destination / "compaction-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "processing"
    manifest["phase"] = "converting"
    manifest["completed_observations"] = 0
    manifest["work_database_authority"]["sha256"] = None
    manifest["work_database_authority"]["hash_phase"] = None
    manifest.pop("error", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        resume=True,
        _writer_scanner=_safe_writer_scan,
    )

    assert result.observation_count == 3
    assert result.outcome_count == 3


def test_final_schema_resume_accepts_commit_before_authority_checkpoint(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"

    def interrupt(phase: str) -> None:
        if phase == "final_schema_committed":
            raise RuntimeError("final schema committed")

    with pytest.raises(RuntimeError, match="final schema committed"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _phase_hook=interrupt,
            _writer_scanner=_safe_writer_scan,
        )
    manifest_path = destination / "compaction-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "validated"
    manifest["phase"] = "final_schema"
    manifest["work_database_authority"]["sha256"] = None
    manifest["work_database_authority"]["hash_phase"] = None
    manifest.pop("error", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        resume=True,
        _writer_scanner=_safe_writer_scan,
    )

    assert result.observation_count == 1
    assert _file_hash(result.output_database) == result.output_sha256


def test_processing_resume_rejects_semantic_tamper_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    monkeypatch.setattr(odds_legacy_compactor, "_COMMIT_BATCH_SIZE", 1)
    with pytest.raises(RuntimeError, match="injected compaction interruption"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _fail_after_observations=1,
            _writer_scanner=_safe_writer_scan,
        )
    manifest_path = destination / "compaction-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "processing"
    manifest["phase"] = "converting"
    manifest["work_database_authority"]["sha256"] = None
    manifest["work_database_authority"]["hash_phase"] = None
    manifest.pop("error", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    work = _work_database_path(destination)
    connection = connect(work)
    try:
        connection.execute(
            "UPDATE live_schema_version SET applied_at='semantic-tamper'"
        )
        connection.commit()
    finally:
        connection.close()
    tampered = _file_hash(work)

    with pytest.raises(RuntimeError, match="changed preserved table"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    assert _file_hash(work) == tampered
    assert not (destination / "dota2-compacted.db").exists()


def test_processing_resume_rejects_converted_equivalence_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    monkeypatch.setattr(odds_legacy_compactor, "_COMMIT_BATCH_SIZE", 1)
    with pytest.raises(RuntimeError, match="injected compaction interruption"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _fail_after_observations=1,
            _writer_scanner=_safe_writer_scan,
        )
    manifest_path = destination / "compaction-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "processing"
    manifest["phase"] = "converting"
    manifest["work_database_authority"]["sha256"] = None
    manifest["work_database_authority"]["hash_phase"] = None
    manifest.pop("error", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    work = _work_database_path(destination)
    connection = connect(work)
    try:
        connection.execute(
            "UPDATE odds_transport_observations "
            "SET normalized_state_hash=? WHERE response_state_hash IS NOT NULL",
            ("0" * 64,),
        )
        connection.commit()
    finally:
        connection.close()
    tampered = _file_hash(work)

    with pytest.raises(
        RuntimeError,
        match="converted processing transport differs from legacy authority",
    ):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            resume=True,
            _writer_scanner=_safe_writer_scan,
        )

    assert _file_hash(work) == tampered
    assert not (destination / "dota2-compacted.db").exists()


def test_recovery_verifies_published_output_then_source_before_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"

    def interrupt_after_replace(phase: str) -> None:
        if phase == "output_replaced":
            raise RuntimeError("output_replaced")

    with pytest.raises(RuntimeError, match="output_replaced"):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _phase_hook=interrupt_after_replace,
            _writer_scanner=_safe_writer_scan,
        )

    original_verify = odds_legacy_compactor._verify_published_output
    original_hash = odds_legacy_compactor._sha256_file
    original_checkpoint = odds_legacy_compactor._checkpoint_manifest
    events: list[str] = []

    def verify(*args, **kwargs):
        result = original_verify(*args, **kwargs)
        events.append("output_verified")
        return result

    def hash_file(path: Path) -> str:
        result = original_hash(path)
        if path.resolve() == database.resolve():
            events.append("source_hashed")
        return result

    def checkpoint(*args, **kwargs):
        if kwargs.get("status") == "ready":
            events.append("ready")
        return original_checkpoint(*args, **kwargs)

    monkeypatch.setattr(odds_legacy_compactor, "_verify_published_output", verify)
    monkeypatch.setattr(odds_legacy_compactor, "_sha256_file", hash_file)
    monkeypatch.setattr(odds_legacy_compactor, "_checkpoint_manifest", checkpoint)

    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        resume=True,
        _writer_scanner=_safe_writer_scan,
    )

    assert events[-3:] == ["output_verified", "source_hashed", "ready"]
    assert _file_hash(result.output_database) == result.output_sha256


def test_runtime_hardlink_added_at_publication_boundary_prevents_output(
    tmp_path: Path,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    probe = tmp_path / "hardlink-probe.db"
    try:
        os.link(database, probe)
    except OSError as error:
        pytest.skip(f"filesystem does not support hardlinks: {error}")
    probe.unlink()
    alias = tmp_path / "source-alias.db"
    destination = tmp_path / "compaction"

    def link_before_publish(phase: str) -> None:
        if phase == "publishing_manifest_written":
            os.link(database, alias)

    try:
        with pytest.raises(RuntimeError, match="exactly one hard link"):
            compact_legacy_odds(
                database,
                raw_root,
                destination,
                _phase_hook=link_before_publish,
                _writer_scanner=_safe_writer_scan,
            )
    finally:
        alias.unlink(missing_ok=True)

    manifest = json.loads(
        (destination / "compaction-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "failed"
    assert manifest["phase"] == "publishing"
    assert not (destination / "dota2-compacted.db").exists()


def test_validation_streams_groups_deduplicates_checks_and_reports_progress(
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
                    _outcome("winner-one", price=2.1, side="team_one"),
                    _outcome("winner-two", price=1.8, side="team_two"),
                ],
                "on_time",
            )
            for index in range(8)
        ],
    )
    destination = tmp_path / "compaction"
    source_hash = _file_hash(database)
    original_iter = odds_legacy_compactor._iter_legacy_groups
    original_state_rows = odds_legacy_compactor._state_rows
    original_verify_artifact = odds_legacy_compactor._verify_artifact_file
    original_write_manifest = odds_legacy_compactor._write_manifest
    iter_calls = 0
    state_row_calls = 0
    artifact_verify_calls = 0
    checkpoints: list[dict[str, object]] = []

    def counted_iter(connection):
        nonlocal iter_calls
        iter_calls += 1
        yield from original_iter(connection)

    def counted_state_rows(connection, state_hash):
        nonlocal state_row_calls
        state_row_calls += 1
        return original_state_rows(connection, state_hash)

    def counted_artifact(*args, **kwargs):
        nonlocal artifact_verify_calls
        artifact_verify_calls += 1
        return original_verify_artifact(*args, **kwargs)

    def record_manifest(path, manifest):
        checkpoints.append(dict(manifest))
        original_write_manifest(path, manifest)

    monkeypatch.setattr(odds_legacy_compactor, "_PROGRESS_BATCH_SIZE", 1)
    monkeypatch.setattr(odds_legacy_compactor, "_iter_legacy_groups", counted_iter)
    monkeypatch.setattr(odds_legacy_compactor, "_state_rows", counted_state_rows)
    monkeypatch.setattr(
        odds_legacy_compactor,
        "_verify_artifact_file",
        counted_artifact,
    )
    monkeypatch.setattr(odds_legacy_compactor, "_write_manifest", record_manifest)

    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        _writer_scanner=_safe_writer_scan,
    )

    validating = [
        int(item["validated_observations"])
        for item in checkpoints
        if item.get("status") == "processing"
        and item.get("phase") == "validating"
    ]
    assert iter_calls == 2
    assert state_row_calls <= 3
    assert artifact_verify_calls <= 3
    assert validating == sorted(validating)
    assert list(dict.fromkeys(validating)) == list(range(9))
    assert checkpoints[-1]["heartbeat_at"]
    assert result.observation_count == 8
    assert _file_hash(database) == source_hash


def _count_compaction_database_hash_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Path]:
    reads: list[Path] = []
    real_read = hash_authority.read_stable_regular_file

    def counted(path: Path, **kwargs: object):
        if kwargs.get("label") == "compaction database":
            reads.append(Path(path).absolute())
        return real_read(path, **kwargs)

    monkeypatch.setattr(hash_authority, "read_stable_regular_file", counted)
    return reads


def _compaction_database_hash_read_counts(
    reads: list[Path],
    *,
    source: Path,
    destination: Path,
) -> tuple[int, int, int]:
    source_path = source.absolute()
    work_names = {
        odds_legacy_compactor._INITIALIZING_WORK_DATABASE,
        odds_legacy_compactor._WORK_DATABASE,
    }
    output_names = {
        odds_legacy_compactor._OUTPUT_DATABASE,
        f".{odds_legacy_compactor._OUTPUT_DATABASE}.vacuuming",
    }
    source_reads = sum(path == source_path for path in reads)
    work_parent = _work_database_path(destination).parent.absolute()
    work_reads = sum(
        path.parent == work_parent and path.name in work_names for path in reads
    )
    output_reads = sum(
        path.parent == destination.absolute() and path.name in output_names
        for path in reads
    )
    assert source_reads + work_reads + output_reads == len(reads)
    return source_reads, work_reads, output_reads


def test_successful_compaction_bounds_full_database_hash_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    reads = _count_compaction_database_hash_reads(monkeypatch)

    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        _writer_scanner=_safe_writer_scan,
    )

    source_reads, work_reads, output_reads = _compaction_database_hash_read_counts(
        reads,
        source=database,
        destination=destination,
    )
    assert (source_reads, work_reads, output_reads, len(reads)) == (1, 3, 1, 5)
    assert result.output_database.is_file()


def test_hash_scope_rejects_same_size_source_mutation_at_real_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    reads = _count_compaction_database_hash_reads(monkeypatch)
    mutated = False

    def mutate_after_output_verification(phase: str) -> None:
        nonlocal mutated
        if phase != "output_verified" or mutated:
            return
        before_size = database.stat().st_size
        with database.open("r+b") as handle:
            payload = handle.read()
            needle = b'"variant":"stable"'
            offset = payload.find(needle)
            assert offset >= 0
            handle.seek(offset + needle.index(b"stable"))
            handle.write(b"tamper")
            handle.flush()
            os.fsync(handle.fileno())
        assert database.stat().st_size == before_size
        mutated = True

    with pytest.raises(
        RuntimeError,
        match="source database changed during offline compaction",
    ):
        compact_legacy_odds(
            database,
            raw_root,
            destination,
            _phase_hook=mutate_after_output_verification,
            _writer_scanner=_safe_writer_scan,
        )

    source_reads, _, _ = _compaction_database_hash_read_counts(
        reads,
        source=database,
        destination=destination,
    )
    assert mutated
    assert source_reads == 2
    assert not (destination / odds_legacy_compactor._OUTPUT_DATABASE).exists()


def test_hash_scope_rehashes_source_after_external_rename_aba_at_real_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    displaced = tmp_path / "source.aba.db"
    reads = _count_compaction_database_hash_reads(monkeypatch)
    attacked = False

    def rename_aba_after_output_verification(phase: str) -> None:
        nonlocal attacked
        if phase != "output_verified" or attacked:
            return
        os.replace(database, displaced)
        os.replace(displaced, database)
        attacked = True

    result = compact_legacy_odds(
        database,
        raw_root,
        destination,
        _phase_hook=rename_aba_after_output_verification,
        _writer_scanner=_safe_writer_scan,
    )

    source_reads, _, _ = _compaction_database_hash_read_counts(
        reads,
        source=database,
        destination=destination,
    )
    assert attacked
    assert source_reads == 2
    assert result.output_database.is_file()
    assert not displaced.exists()


def test_hash_scope_rehashes_source_after_sidecar_change_at_real_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_root = _single_legacy_source(tmp_path)
    destination = tmp_path / "compaction"
    sidecar = Path(f"{database}-shm")
    reads = _count_compaction_database_hash_reads(monkeypatch)
    changed = False

    def add_sidecar_after_output_verification(phase: str) -> None:
        nonlocal changed
        if phase != "output_verified" or changed:
            return
        sidecar.touch()
        changed = True

    try:
        result = compact_legacy_odds(
            database,
            raw_root,
            destination,
            _phase_hook=add_sidecar_after_output_verification,
            _writer_scanner=_safe_writer_scan,
        )
    finally:
        sidecar.unlink(missing_ok=True)

    source_reads, _, _ = _compaction_database_hash_read_counts(
        reads,
        source=database,
        destination=destination,
    )
    assert changed
    assert source_reads == 2
    assert result.output_database.is_file()
