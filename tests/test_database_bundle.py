from __future__ import annotations
# ruff: noqa: E402

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
database_bundle = pytest.importorskip(
    "live_betting.database_bundle",
    reason="SQLite file bundles were retired after the PostgreSQL migration",
)
import live_betting.hash_authority as hash_authority
import live_betting.odds_legacy_compactor as odds_legacy_compactor
import live_betting.service_coordination as service_coordination

from event_intelligence.raw_archive import RawArchive
from event_intelligence.raw_registry import relocate_raw_source_artifacts
from live_betting.database_bundle import (
    create_database_bundle,
    restore_database_bundle,
    verify_database_bundle,
)
from live_betting.database_protocol import (
    CUTOVER_SAFETY_MARGIN_BYTES,
    prepare_database,
    sqlite_sidecar_state,
)
from live_betting.markets import normalized_state_hash, snapshots_from_payload
from live_betting.runtime_schema import (
    CURRENT_RUNTIME_SCHEMA_VERSION,
    RUNTIME_SCHEMA_CONTRACT_DIGEST,
    verify_runtime_schema,
)
from live_betting.service_coordination import (
    SingleInstanceLock,
    WriterScanResult,
    database_authority_lock_paths,
    database_global_authority_lock_paths,
    database_global_service_lock_path,
    database_global_web_lock_path,
    database_offline_authority,
)
from live_betting.storage import LiveBettingStore
from live_betting.vision import VisionObservation
from live_betting.vision_frame_registry import (
    publish_vision_frame_bytes,
    verify_registered_vision_frame,
)
from shared.sqlite import connect


NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)
HEAD = database_bundle._git_commit()


@pytest.fixture(autouse=True)
def _isolate_git_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep bundle unit tests independent of the caller's worktree state."""

    monkeypatch.setattr(database_bundle, "_git_status_porcelain", lambda: ())


def _payload() -> dict[str, object]:
    return {
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
                }
            ],
        }
    }


@pytest.mark.skipif(os.name == "nt", reason="Windows prevents renaming an open file")
def test_single_fd_json_reader_rejects_aba_path_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "authority.json"
    displaced = tmp_path / "authority-original.json"
    malicious = tmp_path / "authority-malicious.json"
    captured = tmp_path / "authority-captured.json"
    path.write_text('{"trusted":true}\n', encoding="utf-8")
    malicious.write_text('{"trusted":false}\n', encoding="utf-8")
    real_open = os.open
    attacked = False

    def aba_open(target: object, flags: int, mode: int = 0o777) -> int:
        nonlocal attacked
        logical = Path(os.path.abspath(os.fspath(target)))
        if logical == path.absolute() and not attacked:
            attacked = True
            os.replace(path, displaced)
            os.replace(malicious, path)
            descriptor = real_open(target, flags, mode)
            os.replace(path, captured)
            os.replace(displaced, path)
            return descriptor
        return real_open(target, flags, mode)

    monkeypatch.setattr(service_coordination.os, "open", aba_open)

    with pytest.raises(RuntimeError, match="file changed"):
        database_bundle._read_json_with_authority(path, label="ABA JSON")

    assert attacked
    assert json.loads(path.read_text(encoding="utf-8")) == {"trusted": True}
    assert json.loads(captured.read_text(encoding="utf-8")) == {"trusted": False}


@pytest.mark.skipif(os.name == "nt", reason="Windows prevents renaming an open file")
def test_bundle_verify_rejects_manifest_aba_during_single_fd_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    manifest = bundle / "manifest.json"
    displaced = tmp_path / "manifest-original.json"
    malicious = tmp_path / "manifest-malicious.json"
    captured = tmp_path / "manifest-captured.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["created_at"] = "attacker-manifest"
    malicious.write_text(json.dumps(payload), encoding="utf-8")
    real_open = os.open
    attacked = False

    def aba_open(target: object, flags: int, mode: int = 0o777) -> int:
        nonlocal attacked
        logical = Path(os.path.abspath(os.fspath(target)))
        if logical == manifest.absolute() and not attacked:
            attacked = True
            os.replace(manifest, displaced)
            os.replace(malicious, manifest)
            descriptor = real_open(target, flags, mode)
            os.replace(manifest, captured)
            os.replace(displaced, manifest)
            return descriptor
        return real_open(target, flags, mode)

    monkeypatch.setattr(service_coordination.os, "open", aba_open)
    with pytest.raises(RuntimeError, match="file changed"):
        verify_database_bundle(bundle)
    assert attacked


@pytest.mark.skipif(os.name == "nt", reason="Windows prevents renaming an open file")
def test_bundle_verify_rejects_gzip_artifact_aba_during_single_fd_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    manifest_payload = json.loads(
        (bundle / "manifest.json").read_text(encoding="utf-8")
    )
    record = next(
        item
        for item in manifest_payload["artifacts"]
        if item["registry"] != "vision_frame_artifacts"
    )
    artifact = bundle / str(record["bundle_path"])
    displaced = tmp_path / "artifact-original.gz"
    malicious = tmp_path / "artifact-malicious.gz"
    captured = tmp_path / "artifact-captured.gz"
    shutil.copy2(artifact, malicious)
    real_open = os.open
    attacked = False

    def aba_open(target: object, flags: int, mode: int = 0o777) -> int:
        nonlocal attacked
        logical = Path(os.path.abspath(os.fspath(target)))
        if logical == artifact.absolute() and not attacked:
            attacked = True
            os.replace(artifact, displaced)
            os.replace(malicious, artifact)
            descriptor = real_open(target, flags, mode)
            os.replace(artifact, captured)
            os.replace(displaced, artifact)
            return descriptor
        return real_open(target, flags, mode)

    monkeypatch.setattr(service_coordination.os, "open", aba_open)
    with pytest.raises(RuntimeError, match="file changed"):
        verify_database_bundle(bundle)
    assert attacked


@pytest.mark.skipif(os.name == "nt", reason="Windows prevents renaming locked roots")
def test_global_authority_lock_survives_database_root_swap(tmp_path: Path) -> None:
    root = tmp_path / "restore"
    root.mkdir()
    database = root / "dota2.db"
    database.write_bytes(b"original")
    displaced_root = tmp_path / "restore-displaced"
    global_locks = database_global_authority_lock_paths(database)

    with pytest.raises(RuntimeError, match="directory identity changed"):
        with database_offline_authority(
            database,
            writer_scanner=lambda _: WriterScanResult((), ()),
        ):
            os.replace(root, displaced_root)
            root.mkdir()
            replacement = root / "dota2.db"
            replacement.write_bytes(b"replacement")
            assert database_global_authority_lock_paths(replacement) == global_locks
            for global_lock in global_locks:
                with pytest.raises(RuntimeError, match="already held|collision"):
                    with SingleInstanceLock(global_lock):
                        pass


def test_atomic_json_fsync_failure_leaves_resumable_published_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "checkpoint.json"
    real_fsync = database_bundle.fsync_directory

    def fail_fsync(_: Path) -> None:
        raise RuntimeError("injected directory fsync failure")

    monkeypatch.setattr(database_bundle, "fsync_directory", fail_fsync)
    with pytest.raises(RuntimeError, match="directory fsync failure"):
        database_bundle._write_json(path, {"generation": 1})

    assert json.loads(path.read_text(encoding="utf-8")) == {"generation": 1}
    monkeypatch.setattr(database_bundle, "fsync_directory", real_fsync)
    database_bundle._write_json(path, {"generation": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"generation": 2}


@pytest.mark.parametrize("implementation", ["bundle", "compactor"])
def test_sidecar_cleanup_quarantines_and_retains_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    implementation: str,
) -> None:
    module = (
        database_bundle
        if implementation == "bundle"
        else odds_legacy_compactor
    )
    database = tmp_path / f"{implementation}.db"
    database.write_bytes(b"database")
    sidecar = Path(f"{database}-shm")
    sidecar.touch()
    displaced = tmp_path / f"{implementation}-authorized-shm"
    replacement_bytes = b"replacement-sidecar-must-not-be-deleted"
    real_replace = module._replace_and_fsync
    attacked = False

    def replace_at_quarantine(source: Path, target: Path) -> None:
        nonlocal attacked
        if source == sidecar and ".quarantine." in target.name and not attacked:
            attacked = True
            os.replace(sidecar, displaced)
            sidecar.write_bytes(replacement_bytes)
        real_replace(source, target)

    monkeypatch.setattr(module, "_replace_and_fsync", replace_at_quarantine)
    with pytest.raises(RuntimeError, match="quarantined.*authority changed"):
        if implementation == "bundle":
            module._clear_quiescent_sqlite_sidecars(database)
        else:
            module._clear_quiescent_sqlite_sidecars(database, label="test database")

    quarantined = list(tmp_path.glob(f".{sidecar.name}.quarantine.*"))
    assert attacked
    assert displaced.is_file()
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == replacement_bytes


def _database_with_artifacts(
    root: Path,
    *,
    immutable_source_artifacts: bool = False,
) -> tuple[Path, Path, Path, dict[str, object], str]:
    root.mkdir()
    database = root / "dota2.db"
    odds_root = root / "live_betting" / "raw-v2"
    source_root = root / "source-raw"
    odds_root.mkdir(parents=True)
    source_root.mkdir()
    prepare_database(database, root / "schema-backups", now=NOW)

    payload = _payload()
    snapshots = snapshots_from_payload(payload, received_at=NOW)
    with LiveBettingStore(database, raw_archive_root=odds_root) as store:
        store.store_odds_observation(
            source="direct",
            observation_key="observation-1",
            source_event_id=None,
            raybet_match_id="1001",
            observed_at=NOW,
            normalized_state_hash=normalized_state_hash(snapshots),
            snapshots=snapshots,
            raw_payload=payload,
        )

    source_payload = {"match_id": 9001, "radiant_win": True}
    receipt = RawArchive(source_root).archive_json(
        source="opendota",
        endpoint="https://api.opendota.com/api/matches/9001",
        request_identity="https://api.opendota.com/api/matches/9001",
        payload_bytes=json.dumps(source_payload).encode(),
        observed_at=NOW,
        match_id=9001,
        status_code=200,
    )
    artifact_id = f"opendota:{receipt.content_sha256}"
    connection = connect(database)
    try:
        connection.execute(
            """INSERT INTO raw_source_artifacts
               (artifact_id, content_hash, source, artifact_use, endpoint,
                sanitized_request_identity, storage_path, uncompressed_bytes,
                compressed_bytes, source_at, received_at, first_usable_at,
                schema_fingerprint, event_id, match_id, created_at)
               VALUES (?, ?, 'opendota', 'primary', ?, ?, ?, ?, ?, NULL,
                       ?, NULL, ?, NULL, 9001, ?)""",
            (
                artifact_id,
                receipt.content_sha256,
                receipt.endpoint,
                receipt.request_identity,
                str(receipt.path.resolve()),
                receipt.byte_count,
                receipt.compressed_byte_count,
                NOW.isoformat(),
                receipt.schema_fingerprint,
                NOW.isoformat(),
            ),
        )
        if immutable_source_artifacts:
            connection.execute(
                """CREATE TRIGGER raw_source_artifacts_test_immutable
                   BEFORE UPDATE ON raw_source_artifacts
                   BEGIN
                     SELECT RAISE(ABORT, 'raw source artifact is immutable');
                   END"""
            )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    return database, odds_root, source_root, payload, artifact_id


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle_database_path(bundle: Path) -> Path:
    return bundle / database_bundle._BUNDLE_DATABASE_PATH


def _restore_staging_database_path(target: Path) -> Path:
    return (
        database_bundle._restore_staging_directory(target)
        / database_bundle._DATABASE_DIRECTORY
        / "dota2.db"
    )


def _track_physical_database_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[Path, str]]:
    calls: list[tuple[Path, str]] = []
    real_read = hash_authority.read_stable_regular_file

    def tracked_read(
        path: Path,
        *,
        label: str,
        include_payload: bool = True,
    ):
        logical = Path(path).resolve()
        if logical.suffix in {".db", ".sqlite"}:
            calls.append((logical, label))
        return real_read(path, label=label, include_payload=include_payload)

    monkeypatch.setattr(hash_authority, "read_stable_regular_file", tracked_read)
    return calls


def _isolate_managed_writer_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service_coordination,
        "scan_managed_writers",
        lambda *_args, **_kwargs: WriterScanResult((), ()),
    )


def _empty_bundle_source(root: Path) -> tuple[Path, Path]:
    root.mkdir()
    database = root / "dota2.db"
    odds_root = root / "live_betting" / "raw-v2"
    prepare_database(database, root / "schema-backups", now=NOW)
    return database, odds_root


def _database_hash_trace(calls: list[tuple[Path, str]]) -> str:
    return "\n".join(
        f"{index}: {path} [{label}]"
        for index, (path, label) in enumerate(calls, start=1)
    )


@pytest.mark.parametrize(
    "attack",
    [
        "status",
        "wrong-head",
        "nonancestor",
        "target",
        "source-database",
        "odds-root",
        "allowed-roots",
        "already-adopted",
    ],
)
def test_snapshot_pending_provenance_adoption_rejects_invalid_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    old_head = "1" * 40
    current_head = "2" * 40
    old_provenance = {
        "source_tree_clean": True,
        "source_tree_policy_version": database_bundle._SOURCE_TREE_POLICY_VERSION,
        "source_tree_head": old_head,
        "source_tree_runtime_dirty_paths": [],
    }
    current_provenance = {
        **old_provenance,
        "source_tree_head": current_head,
    }
    binding = {
        "target": str(tmp_path / "bundle"),
        "source_database": str(tmp_path / "source.db"),
        "odds_raw_root": str(tmp_path / "raw"),
        "allowed_source_roots": [str(tmp_path)],
        "git_commit": current_head,
        **current_provenance,
    }
    checkpoint: dict[str, object] = {
        "format": database_bundle._STAGING_FORMAT,
        "status": "snapshot_pending",
        "git_commit": old_head,
        **old_provenance,
        **{
            key: value
            for key, value in binding.items()
            if key
            in {
                "target",
                "source_database",
                "odds_raw_root",
                "allowed_source_roots",
            }
        },
    }
    confirmed = old_head
    if attack == "status":
        checkpoint["status"] = "copying"
    elif attack == "wrong-head":
        confirmed = "3" * 40
    elif attack == "target":
        checkpoint["target"] = str(tmp_path / "other-bundle")
    elif attack == "source-database":
        checkpoint["source_database"] = str(tmp_path / "other.db")
    elif attack == "odds-root":
        checkpoint["odds_raw_root"] = str(tmp_path / "other-raw")
    elif attack == "allowed-roots":
        checkpoint["allowed_source_roots"] = [str(tmp_path / "other-root")]
    elif attack == "already-adopted":
        checkpoint["provenance_recovery"] = {}
    staging = tmp_path / "staging"
    staging.mkdir()
    checkpoint_path = staging / database_bundle._STAGING_MANIFEST_FILE
    database_bundle._write_json(checkpoint_path, checkpoint)
    original = checkpoint_path.read_bytes()

    if attack == "nonancestor":
        def reject_ancestor(_old: str, _current: str) -> None:
            raise RuntimeError("checkpoint git commit is not an ancestor")

        monkeypatch.setattr(
            database_bundle,
            "_require_git_commit_ancestor",
            reject_ancestor,
        )
    else:
        monkeypatch.setattr(
            database_bundle,
            "_require_git_commit_ancestor",
            lambda _old, _current: None,
        )

    with pytest.raises(RuntimeError):
        database_bundle._adopt_snapshot_pending_provenance(
            staging,
            checkpoint,
            binding,
            confirmed,
        )
    assert checkpoint_path.read_bytes() == original


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_runtime_prepare_commits_without_transactional_sidecars(
    tmp_path: Path,
    journal_mode: str,
) -> None:
    database = tmp_path / f"runtime-{journal_mode.casefold()}.db"
    connection = connect(database)
    try:
        assert connection.execute(
            f"PRAGMA journal_mode={journal_mode}"
        ).fetchone()[0] == journal_mode.casefold()
        connection.execute("CREATE TABLE application_state(value INTEGER)")
        connection.execute("INSERT INTO application_state VALUES (42)")
        connection.commit()
    finally:
        connection.close()

    database_bundle._prepare_runtime_database(database)

    sidecars = sqlite_sidecar_state(database)
    assert int(sidecars["wal"]["bytes"]) == 0
    assert int(sidecars["journal"]["bytes"]) == 0
    prepared = connect(database)
    try:
        assert prepared.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert prepared.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
            0,
            -1,
            -1,
        )
        assert prepared.execute("SELECT value FROM application_state").fetchone()[
            0
        ] == 42
        status = verify_runtime_schema(prepared)
        assert status.version == CURRENT_RUNTIME_SCHEMA_VERSION
        assert status.contract_digest == RUNTIME_SCHEMA_CONTRACT_DIGEST
    finally:
        prepared.close()


def test_runtime_prepare_rejects_busy_wal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "busy-wal.db"
    setup = connect(database)
    try:
        assert setup.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        setup.execute("CREATE TABLE application_state(value INTEGER)")
        setup.execute("INSERT INTO application_state VALUES (42)")
        setup.commit()
    finally:
        setup.close()
    reader = connect(database, read_only=True)
    reader.execute("BEGIN")
    assert reader.execute("SELECT value FROM application_state").fetchone()[0] == 42
    real_connect = connect

    def short_timeout_connect(path: Path):
        return real_connect(path, busy_timeout_ms=10)

    monkeypatch.setattr(database_bundle, "connect", short_timeout_connect)
    try:
        with pytest.raises(RuntimeError, match="left an unsafe WAL"):
            database_bundle._prepare_runtime_database(database)
        assert int(sqlite_sidecar_state(database)["wal"]["bytes"]) > 0
    finally:
        reader.close()


def test_runtime_prepare_rejects_nonempty_rollback_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "persistent-journal.db"
    connection = connect(database)
    try:
        connection.execute("CREATE TABLE application_state(value INTEGER)")
        connection.commit()
    finally:
        connection.close()
    real_connect = connect

    def persistent_journal_connect(path: Path):
        prepared = real_connect(path)
        assert prepared.execute("PRAGMA journal_mode=PERSIST").fetchone()[0] == (
            "persist"
        )
        return prepared

    monkeypatch.setattr(database_bundle, "connect", persistent_journal_connect)

    with pytest.raises(RuntimeError, match="non-empty SQLite sidecars"):
        database_bundle._prepare_runtime_database(database)
    assert int(sqlite_sidecar_state(database)["journal"]["bytes"]) > 0


def test_staging_database_paths_preserve_legacy_checkpoint_layout(
    tmp_path: Path,
) -> None:
    bundle_staging = tmp_path / "bundle-staging"
    restore_staging = tmp_path / "restore-staging"

    assert database_bundle._bundle_staging_database_path(
        bundle_staging,
        {},
    ) == (bundle_staging / "database.sqlite").resolve()
    assert database_bundle._bundle_staging_database_path(
        bundle_staging,
        {"database_path": "database/database.sqlite"},
    ) == (bundle_staging / "database" / "database.sqlite").resolve()
    assert database_bundle._restore_staging_database_path(
        restore_staging,
        {},
        "dota2.db",
    ) == (restore_staging / "dota2.db").resolve()
    assert database_bundle._restore_staging_database_path(
        restore_staging,
        {"staging_database_path": "database/dota2.db"},
        "dota2.db",
    ) == (restore_staging / "database" / "dota2.db").resolve()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_path", "other/database.sqlite"),
        ("staging_database_path", "other/dota2.db"),
    ],
)
def test_staging_database_paths_reject_uncontrolled_checkpoint_layout(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    if field == "database_path":
        with pytest.raises(RuntimeError, match="not controlled"):
            database_bundle._bundle_staging_database_path(
                tmp_path,
                {field: value},
            )
    else:
        with pytest.raises(RuntimeError, match="not controlled"):
            database_bundle._restore_staging_database_path(
                tmp_path,
                {field: value},
                "dota2.db",
            )


def test_create_database_bundle_hashes_databases_at_most_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_managed_writer_scan(monkeypatch)
    database, odds_root = _empty_bundle_source(tmp_path / "source")
    calls = _track_physical_database_hashes(monkeypatch)

    create_database_bundle(database, odds_root, tmp_path / "bundle")

    assert len(calls) <= 2, _database_hash_trace(calls)
    paths = [path for path, _label in calls]
    assert [path.suffix for path in paths].count(".db") == 1
    assert [path.suffix for path in paths].count(".sqlite") == 1


def test_standalone_bundle_verify_rehashes_once_per_public_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_managed_writer_scan(monkeypatch)
    database, odds_root = _empty_bundle_source(tmp_path / "source")
    bundle = tmp_path / "bundle"
    create_database_bundle(database, odds_root, bundle)
    calls = _track_physical_database_hashes(monkeypatch)
    bundle_database = _bundle_database_path(bundle).resolve()

    verify_database_bundle(bundle)
    first = tuple(calls)
    calls.clear()
    verify_database_bundle(bundle)
    second = tuple(calls)

    assert [path for path, _label in first] == [bundle_database]
    assert [path for path, _label in second] == [bundle_database]


def test_bundle_verify_does_not_reuse_unrelated_hash_authority_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_managed_writer_scan(monkeypatch)
    database, odds_root = _empty_bundle_source(tmp_path / "source")
    bundle = tmp_path / "bundle"
    create_database_bundle(database, odds_root, bundle)
    unrelated_lock = tmp_path / "unrelated.operation.lock"
    bundle_lock = database_bundle._operation_lock_path(bundle.resolve())

    with (
        SingleInstanceLock(unrelated_lock),
        hash_authority.file_hash_authority_scope(required_locks=(unrelated_lock,)),
        SingleInstanceLock(bundle_lock),
    ):
        with pytest.raises(RuntimeError, match="lock is already held"):
            verify_database_bundle(bundle)


def test_restore_database_bundle_hashes_databases_at_most_three_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_managed_writer_scan(monkeypatch)
    database, odds_root = _empty_bundle_source(tmp_path / "source")
    bundle = tmp_path / "bundle"
    create_database_bundle(database, odds_root, bundle)
    calls = _track_physical_database_hashes(monkeypatch)

    result = restore_database_bundle(bundle, tmp_path / "restore")

    assert result.database.is_file()
    assert len(calls) <= 3, _database_hash_trace(calls)
    paths = [path for path, _label in calls]
    assert _bundle_database_path(bundle).resolve() in paths
    assert any(path.name == "dota2.db" for path in paths)


def test_bundle_verify_rejects_same_size_in_place_database_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_managed_writer_scan(monkeypatch)
    database, odds_root = _empty_bundle_source(tmp_path / "source")
    bundle = tmp_path / "bundle"
    create_database_bundle(database, odds_root, bundle)
    bundle_database = _bundle_database_path(bundle).resolve()
    original_size = bundle_database.stat().st_size
    real_require = database_bundle._require_database_file_authority
    attacked = False

    def change_before_final_authority(
        path: Path,
        expected: object,
        **kwargs: object,
    ):
        nonlocal attacked
        if Path(path).resolve() == bundle_database and not attacked:
            attacked = True
            with bundle_database.open("r+b") as handle:
                handle.seek(-1, os.SEEK_END)
                value = handle.read(1)
                handle.seek(-1, os.SEEK_END)
                handle.write(bytes((value[0] ^ 0x01,)))
                handle.flush()
                os.fsync(handle.fileno())
            assert bundle_database.stat().st_size == original_size
        return real_require(path, expected, **kwargs)

    monkeypatch.setattr(
        database_bundle,
        "_require_database_file_authority",
        change_before_final_authority,
    )
    with pytest.raises(RuntimeError, match="file authority changed"):
        verify_database_bundle(bundle)
    assert attacked


def test_bundle_verify_rehashes_after_external_database_rename_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_managed_writer_scan(monkeypatch)
    database, odds_root = _empty_bundle_source(tmp_path / "source")
    bundle = tmp_path / "bundle"
    create_database_bundle(database, odds_root, bundle)
    bundle_database = _bundle_database_path(bundle).resolve()
    displaced = tmp_path / "bundle-original.sqlite"
    replacement = tmp_path / "bundle-replacement.sqlite"
    captured = tmp_path / "bundle-captured.sqlite"
    shutil.copy2(bundle_database, replacement)
    calls = _track_physical_database_hashes(monkeypatch)
    real_require = database_bundle._require_database_file_authority
    attacked = False

    def rename_aba_before_final_authority(
        path: Path,
        expected: object,
        **kwargs: object,
    ):
        nonlocal attacked
        if Path(path).resolve() == bundle_database and not attacked:
            attacked = True
            os.replace(bundle_database, displaced)
            os.replace(replacement, bundle_database)
            os.replace(bundle_database, captured)
            os.replace(displaced, bundle_database)
        return real_require(path, expected, **kwargs)

    monkeypatch.setattr(
        database_bundle,
        "_require_database_file_authority",
        rename_aba_before_final_authority,
    )
    verify_database_bundle(bundle)

    assert attacked
    assert [path for path, _label in calls] == [bundle_database, bundle_database]


def test_bundle_verify_rehashes_when_database_sidecar_state_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_managed_writer_scan(monkeypatch)
    database, odds_root = _empty_bundle_source(tmp_path / "source")
    bundle = tmp_path / "bundle"
    create_database_bundle(database, odds_root, bundle)
    bundle_database = _bundle_database_path(bundle).resolve()
    sidecar = Path(f"{bundle_database}-shm")
    calls = _track_physical_database_hashes(monkeypatch)
    real_require = database_bundle._require_database_file_authority
    attacked = False

    def add_sidecar_before_final_authority(
        path: Path,
        expected: object,
        **kwargs: object,
    ):
        nonlocal attacked
        if Path(path).resolve() == bundle_database and not attacked:
            attacked = True
            sidecar.touch()
        return real_require(path, expected, **kwargs)

    monkeypatch.setattr(
        database_bundle,
        "_require_database_file_authority",
        add_sidecar_before_final_authority,
    )
    try:
        verify_database_bundle(bundle)
    finally:
        sidecar.unlink(missing_ok=True)

    assert attacked
    assert [path for path, _label in calls] == [bundle_database, bundle_database]


def _open_wal_only_writer(database: Path, table: str):
    writer = connect(database, wal=True)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    before = _hash(database)
    assert _hash(database) == before
    writer.execute(f"CREATE TABLE {table}(value INTEGER NOT NULL)")
    writer.execute(f"INSERT INTO {table} VALUES (1)")
    writer.commit()
    assert _hash(database) == before
    assert Path(f"{database}-wal").stat().st_size > 0
    return writer


def _add_vision_frame(database: Path, root: Path):
    receipt = publish_vision_frame_bytes(
        root / "live_betting" / "live_evidence",
        b"bundle-vision-frame-pixels",
    )
    with LiveBettingStore(database) as store:
        assert store.insert_vision_observation(
            VisionObservation(
                "vision-match",
                1,
                NOW,
                600,
                False,
                (1, 2, 3, 4, 5),
                (6, 7, 8, 9, 10),
                0.95,
                0.95,
                receipt.frame_ref,
                "game",
                "team_one",
                source_frame_sha256=receipt.content_sha256,
                source_frame_bytes=receipt.byte_length,
                source_frame_path=str(receipt.storage_path),
            )
        )
    return receipt


def test_bundle_restores_database_and_both_raw_registries_relocatably(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, payload, artifact_id = _database_with_artifacts(
        tmp_path / "source"
    )
    source_hash = _hash(database)
    bundle = tmp_path / "backup-bundle"

    result = create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        git_commit=HEAD,
    )

    assert result.artifact_count == 2
    assert _hash(database) == source_hash
    manifest = verify_database_bundle(bundle)
    assert manifest["git_commit"] == HEAD
    assert manifest["artifact_count"] == 2
    assert manifest["source_tree_clean"] is True
    assert (
        manifest["source_tree_policy_version"]
        == database_bundle._SOURCE_TREE_POLICY_VERSION
    )
    assert manifest["source_tree_head"] == database_bundle._git_commit()
    assert manifest["source_tree_runtime_dirty_paths"] == []
    assert manifest["schema"]["versions"]["runtime_schema_version"] == (
        CURRENT_RUNTIME_SCHEMA_VERSION
    )

    restored = restore_database_bundle(bundle, tmp_path / "relocated")
    with LiveBettingStore(restored.database) as store:
        assert store.response_raw_payload("observation-1") == payload
    connection = connect(restored.database, read_only=True)
    try:
        source_path = Path(
            str(
                connection.execute(
                    "SELECT storage_path FROM raw_source_artifacts WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()[0]
            )
        )
    finally:
        connection.close()
    assert source_path.is_relative_to(restored.source_raw_root)
    assert json.loads(gzip.decompress(source_path.read_bytes())) == {
        "match_id": 9001,
        "radiant_win": True,
    }


@pytest.mark.parametrize(
    "status_line",
    [
        " M live_betting/storage.py",
        "?? tests/untracked_bundle_fixture.py",
    ],
)
def test_bundle_rejects_non_runtime_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_line: str,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    monkeypatch.setattr(
        database_bundle, "_git_status_porcelain", lambda: (status_line,)
    )

    with pytest.raises(RuntimeError, match="non-runtime changes"):
        create_database_bundle(
            database,
            odds_root,
            tmp_path / "backup-bundle",
            allowed_source_roots=[source_root],
            git_commit=HEAD,
        )


def test_bundle_rejects_hardlinked_source_database(tmp_path: Path) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    alias = tmp_path / "database-alias.db"
    os.link(database, alias)
    bundle = tmp_path / "backup-bundle"

    with pytest.raises(RuntimeError, match="exactly one hard link"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
            git_commit=HEAD,
        )

    assert not bundle.exists()
    assert not (tmp_path / ".backup-bundle.staging").exists()


def test_bundle_rejects_source_hardlink_created_during_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source-runtime-hardlink"
    )
    alias = tmp_path / "runtime-database-alias.db"
    bundle = tmp_path / "runtime-hardlink-bundle"
    original_backup = database_bundle.online_backup

    def backup_and_link(
        source: Path,
        destination: Path,
        **kwargs: object,
    ) -> None:
        original_backup(source, destination, **kwargs)
        os.link(source, alias)

    monkeypatch.setattr(database_bundle, "online_backup", backup_and_link)
    try:
        with pytest.raises(RuntimeError, match="exactly one hard link"):
            create_database_bundle(
                database,
                odds_root,
                bundle,
                allowed_source_roots=[source_root],
                git_commit=HEAD,
            )
    finally:
        alias.unlink(missing_ok=True)

    assert not bundle.exists()
    assert (tmp_path / ".runtime-hardlink-bundle.staging").is_dir()


@pytest.mark.parametrize(
    "lock_index",
    [0, 1, 2, 3],
    ids=["global-service", "global-web", "service", "web"],
)
def test_bundle_requires_all_source_authority_locks(
    tmp_path: Path,
    lock_index: int,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "backup-bundle"
    lock_path = database_authority_lock_paths(database)[lock_index]

    with database_bundle.SingleInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="lock is already held"):
            create_database_bundle(
                database,
                odds_root,
                bundle,
                allowed_source_roots=[source_root],
            )

    assert not bundle.exists()
    assert not (tmp_path / ".backup-bundle.staging").exists()


def test_bundle_checks_both_sides_of_a_rename_for_source_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        database_bundle,
        "_git_status_porcelain",
        lambda: ("R  live_betting/storage.py -> data/storage.py",),
    )

    with pytest.raises(RuntimeError, match="live_betting/storage.py"):
        database_bundle._source_tree_provenance()


def test_bundle_allows_runtime_only_changes_and_records_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    statuses = (
        " M data/dota2.db",
        "?? dogfood-output/audit.log",
        "?? .pytest_cache/v/cache/lastfailed",
        " M web/frontend/tsconfig.app.tsbuildinfo",
    )
    monkeypatch.setattr(database_bundle, "_git_status_porcelain", lambda: statuses)

    bundle = tmp_path / "backup-bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        git_commit=HEAD,
    )
    manifest = verify_database_bundle(bundle)
    assert manifest["source_tree_clean"] is True
    assert manifest["source_tree_policy_version"] == "runtime-only-v1"
    assert manifest["source_tree_runtime_dirty_paths"] == sorted(
        (
            "data/dota2.db",
            "dogfood-output/audit.log",
            ".pytest_cache/v/cache/lastfailed",
            "web/frontend/tsconfig.app.tsbuildinfo",
        )
    )


def test_bundle_fails_closed_when_git_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git")

    monkeypatch.setattr(database_bundle.subprocess, "run", unavailable)
    with pytest.raises(RuntimeError, match="cannot determine the source git commit"):
        database_bundle._source_tree_provenance()


def test_bundle_fails_closed_when_git_head_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_head(
        args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="not-a-commit\n", stderr="")

    monkeypatch.setattr(database_bundle.subprocess, "run", invalid_head)
    with pytest.raises(RuntimeError, match="source git commit is invalid"):
        database_bundle._git_commit()


def test_bundle_accepts_missing_raw_root_when_registry_is_empty(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    database = root / "dota2.db"
    odds_root = root / "live_betting" / "raw-v2"
    prepare_database(database, root / "schema-backups", now=NOW)

    bundle = tmp_path / "backup-bundle"
    result = create_database_bundle(
        database,
        odds_root,
        bundle,
        git_commit=HEAD,
    )

    assert result.artifact_count == 0
    assert not odds_root.exists()
    assert verify_database_bundle(bundle)["artifact_count"] == 0


def test_bundle_rejects_missing_raw_root_when_registry_has_artifacts(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    shutil.rmtree(odds_root)

    with pytest.raises(FileNotFoundError, match="registered raw artifacts"):
        create_database_bundle(
            database,
            odds_root,
            tmp_path / "backup-bundle",
            allowed_source_roots=[source_root],
            git_commit=HEAD,
        )


def test_cutover_safety_margin_is_shared_and_512_mib() -> None:
    expected = 512 * 1024 * 1024
    assert CUTOVER_SAFETY_MARGIN_BYTES == expected
    assert database_bundle._SPACE_MARGIN_BYTES == expected
    assert odds_legacy_compactor._SAFETY_MARGIN_BYTES == expected


def test_bundle_roundtrip_relocates_and_reverifies_vision_frame(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    database, odds_root, raw_source_root, _, _ = _database_with_artifacts(source_root)
    original = _add_vision_frame(database, source_root)
    bundle = tmp_path / "backup-bundle"
    created = create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[raw_source_root],
        git_commit=HEAD,
    )
    assert created.artifact_count == 3
    manifest = verify_database_bundle(bundle)
    vision = next(
        item
        for item in manifest["artifacts"]
        if item["registry"] == "vision_frame_artifacts"
    )
    assert vision["content_sha256"] == original.content_sha256
    bundled_frame = bundle / vision["bundle_path"]
    assert bundled_frame.stat().st_nlink == 1
    assert not os.path.samefile(original.storage_path, bundled_frame)

    restored = restore_database_bundle(bundle, tmp_path / "restored")
    connection = connect(restored.database, read_only=True)
    try:
        receipt = verify_registered_vision_frame(
            connection,
            original.frame_ref,
            expected_sha256=original.content_sha256,
            expected_bytes=original.byte_length,
        )
        relocation = connection.execute(
            """SELECT reason, actor
                 FROM vision_frame_artifact_relocations
                WHERE frame_ref=? ORDER BY relocation_sequence DESC LIMIT 1""",
            (original.frame_ref,),
        ).fetchone()
        observation_ref = connection.execute(
            """SELECT source_frame_ref FROM vision_observations
                WHERE raybet_match_id='vision-match'"""
        ).fetchone()[0]
    finally:
        connection.close()
    assert receipt.storage_path.is_relative_to(restored.vision_frame_root)
    assert receipt.storage_path.stat().st_nlink == 1
    assert not os.path.samefile(bundled_frame, receipt.storage_path)
    assert observation_ref == original.frame_ref
    assert tuple(relocation) == (
        "database bundle restore",
        "live_betting.database_bundle",
    )


def test_bundle_rejects_tampered_registered_vision_frame(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    database, odds_root, raw_source_root, _, _ = _database_with_artifacts(source_root)
    _add_vision_frame(database, source_root)
    bundle = tmp_path / "backup-bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[raw_source_root],
        git_commit=HEAD,
    )
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    vision = next(
        item
        for item in manifest["artifacts"]
        if item["registry"] == "vision_frame_artifacts"
    )
    (bundle / vision["bundle_path"]).write_bytes(b"tampered")

    with pytest.raises(RuntimeError, match="artifact|vision"):
        verify_database_bundle(bundle)
    with pytest.raises(RuntimeError, match="artifact|vision"):
        restore_database_bundle(bundle, tmp_path / "must-not-restore")
    failed_target = tmp_path / "must-not-restore"
    assert {path.name for path in failed_target.iterdir()} == {
        "dota2.service.lock",
        "dota2.web.lock",
    }
    assert not (failed_target / "dota2.db").exists()


def test_bundle_rejects_registered_vision_frame_with_hardlink(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    database, odds_root, raw_source_root, _, _ = _database_with_artifacts(source_root)
    receipt = _add_vision_frame(database, source_root)
    alias = receipt.storage_path.with_name("alias.jpg")
    try:
        os.link(receipt.storage_path, alias)
    except OSError:
        pytest.skip("filesystem does not support hardlinks")

    bundle = tmp_path / "must-not-bundle"
    with pytest.raises(RuntimeError, match="vision frame"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[raw_source_root],
            git_commit=HEAD,
        )
    assert not bundle.exists()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_bundle_verification_and_restore_reject_missing_or_corrupt_artifact(
    tmp_path: Path,
    damage: str,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "backup-bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        git_commit=HEAD,
    )
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    artifact = bundle / manifest["artifacts"][0]["bundle_path"]
    if damage == "missing":
        artifact.unlink()
    else:
        artifact.write_bytes(b"not-a-valid-gzip-artifact")

    with pytest.raises(RuntimeError, match="artifact"):
        verify_database_bundle(bundle)
    target = tmp_path / "must-not-publish"
    with pytest.raises(RuntimeError, match="artifact"):
        restore_database_bundle(bundle, target)
    assert {path.name for path in target.iterdir()} == {
        "dota2.service.lock",
        "dota2.web.lock",
    }
    assert not (target / "dota2.db").exists()


def test_restore_records_audited_relocation_and_preserves_registry_guard(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, artifact_id = _database_with_artifacts(
        tmp_path / "source",
    )
    bundle = tmp_path / "backup-bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        git_commit=HEAD,
    )

    restored = restore_database_bundle(bundle, tmp_path / "relocated")
    connection = connect(restored.database, read_only=True)
    try:
        path = Path(
            str(
                connection.execute(
                    "SELECT storage_path FROM raw_source_artifacts WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()[0]
            )
        )
        trigger = connection.execute(
            """SELECT 1 FROM sqlite_master WHERE type='trigger'
                AND name='raw_source_artifacts_relocation_required'"""
        ).fetchone()
        relocation = connection.execute(
            """SELECT reason, actor FROM raw_source_artifact_relocations
                WHERE artifact_id=?""",
            (artifact_id,),
        ).fetchone()
    finally:
        connection.close()
    assert path.is_relative_to(restored.source_raw_root)
    assert path.is_file()
    assert trigger is not None
    assert tuple(relocation) == (
        "database bundle restore",
        "live_betting.database_bundle",
    )


def test_bundle_artifacts_are_independent_copies(tmp_path: Path) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "backup-bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        git_commit=HEAD,
    )
    manifest = verify_database_bundle(bundle)
    for record in manifest["artifacts"]:
        if record["registry"] == "odds_raw_artifacts":
            source = odds_root / record["database_storage_path"]
        else:
            source = Path(record["database_storage_path"])
        copied = bundle / record["bundle_path"]
        assert not os.path.samefile(source, copied)


def test_bundle_staging_is_target_bound_fail_closed_and_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "backup-bundle"
    original_copy = database_bundle._copy_artifact
    calls = 0

    def interrupt_after_copy(source: Path, destination: Path) -> None:
        nonlocal calls
        original_copy(source, destination)
        calls += 1
        if calls == 1:
            raise RuntimeError("injected bundle interruption")

    monkeypatch.setattr(database_bundle, "_copy_artifact", interrupt_after_copy)
    with pytest.raises(RuntimeError, match="injected bundle interruption"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
            git_commit=HEAD,
        )
    staging = tmp_path / ".backup-bundle.staging"
    assert staging.is_dir()
    assert not bundle.exists()

    with pytest.raises(FileExistsError, match="resume=True"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
            git_commit=HEAD,
        )
    with pytest.raises(RuntimeError, match="binding mismatch"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root, tmp_path / "other-source-root"],
            git_commit=HEAD,
            resume=True,
        )

    monkeypatch.setattr(database_bundle, "_copy_artifact", original_copy)
    result = create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        git_commit=HEAD,
        resume=True,
    )
    assert result.bundle_directory == bundle
    assert bundle.is_dir()
    assert not staging.exists()
    verify_database_bundle(bundle)


def test_bundle_space_preflight_counts_database_and_raw_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "backup-bundle"
    operation_lock = database_bundle._operation_lock_path(bundle.resolve())
    immutable_locks = (
        *database_authority_lock_paths(database.resolve()),
        operation_lock,
    )
    with (
        database_offline_authority(database, lock_factory=SingleInstanceLock),
        SingleInstanceLock(operation_lock),
    ):
        required = database_bundle._required_bundle_bytes(
            database,
            immutable_locks=immutable_locks,
        )
    connection = connect(database, read_only=True)
    try:
        raw_bytes = sum(
            int(row[0])
            for row in connection.execute(
                """SELECT compressed_bytes FROM odds_raw_artifacts
                   UNION ALL
                   SELECT compressed_bytes FROM raw_source_artifacts"""
            )
        )
    finally:
        connection.close()
    assert required >= database.stat().st_size + raw_bytes
    monkeypatch.setattr(
        database_bundle.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=required - 1),
    )

    with pytest.raises(RuntimeError, match="insufficient free space"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
            git_commit=HEAD,
        )
    assert not bundle.exists()
    assert not (tmp_path / ".backup-bundle.staging").exists()


def test_restore_staging_is_target_bound_fail_closed_and_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "backup-bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        git_commit=HEAD,
    )
    target = tmp_path / "restored"
    original_copy = database_bundle._copy_artifact
    interrupted = False

    def interrupt_after_database(source: Path, destination: Path) -> None:
        nonlocal interrupted
        original_copy(source, destination)
        if not interrupted:
            interrupted = True
            raise RuntimeError("injected restore interruption")

    monkeypatch.setattr(database_bundle, "_copy_artifact", interrupt_after_database)
    with pytest.raises(RuntimeError, match="injected restore interruption"):
        restore_database_bundle(bundle, target)
    staging = tmp_path / ".restored.restore-staging"
    assert staging.is_dir()
    assert {path.name for path in target.iterdir()} == {
        "dota2.service.lock",
        "dota2.web.lock",
    }
    assert not (target / "dota2.db").exists()

    with pytest.raises(FileExistsError, match="resume=True"):
        restore_database_bundle(bundle, target)
    with pytest.raises(RuntimeError, match="binding mismatch"):
        restore_database_bundle(
            bundle,
            target,
            database_name="other.db",
            resume=True,
        )

    monkeypatch.setattr(database_bundle, "_copy_artifact", original_copy)
    restored = restore_database_bundle(bundle, target, resume=True)
    assert restored.database.is_file()
    assert target.is_dir()
    assert not staging.exists()
    assert not (target / "staging-manifest.json").exists()


def test_restore_resume_recovers_commit_before_checkpoint_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, artifact_id = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "backup-bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        git_commit=HEAD,
    )
    target = tmp_path / "restored"
    original_write = database_bundle._write_json
    interrupted = False

    def interrupt_verifying_checkpoint(path: Path, value: dict[str, object]) -> None:
        nonlocal interrupted
        if (
            not interrupted
            and path.name == "staging-manifest.json"
            and value.get("format") == "dota2-database-bundle-restore-staging-v1"
            and value.get("status") == "verifying"
        ):
            interrupted = True
            raise RuntimeError("injected relocation checkpoint interruption")
        original_write(path, value)

    monkeypatch.setattr(database_bundle, "_write_json", interrupt_verifying_checkpoint)
    with pytest.raises(RuntimeError, match="relocation checkpoint interruption"):
        restore_database_bundle(bundle, target)
    staging = tmp_path / ".restored.restore-staging"
    checkpoint = json.loads(
        (staging / "staging-manifest.json").read_text(encoding="utf-8")
    )
    assert checkpoint["status"] == "relocating"
    staged = connect(_restore_staging_database_path(target), read_only=True)
    try:
        assert (
            staged.execute(
                "SELECT COUNT(*) FROM raw_source_artifact_relocations WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()[0]
            == 1
        )
    finally:
        staged.close()

    monkeypatch.setattr(database_bundle, "_write_json", original_write)
    restored = restore_database_bundle(bundle, target, resume=True)
    connection = connect(restored.database, read_only=True)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM raw_source_artifact_relocations WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_restore_resume_rejects_unknown_or_tampered_staging(tmp_path: Path) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "backup-bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        git_commit=HEAD,
    )
    target = tmp_path / "restored"
    staging = tmp_path / ".restored.restore-staging"
    staging.mkdir()
    with pytest.raises(
        RuntimeError,
        match="restore staging manifest file is missing",
    ):
        restore_database_bundle(bundle, target, resume=True)

    staging_manifest = {
        "format": "dota2-database-bundle-restore-staging-v1",
        "status": "copying",
        "bundle_root": str(bundle.resolve()),
        "bundle_manifest_sha256": "0" * 64,
        "target": str(target.resolve()),
        "database_name": "dota2.db",
    }
    (staging / "staging-manifest.json").write_text(
        json.dumps(staging_manifest), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="binding mismatch"):
        restore_database_bundle(bundle, target, resume=True)


def test_bundle_does_not_scan_or_copy_unregistered_raw_files(tmp_path: Path) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    unregistered = source_root / "unregistered.json.gz"
    unregistered.write_bytes(gzip.compress(b"{}", mtime=0))
    bundle = tmp_path / "backup-bundle"

    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        git_commit=HEAD,
    )

    assert all(path.name != unregistered.name for path in bundle.rglob("*"))
    raw_files = [path for path in (bundle / "raw").rglob("*") if path.is_file()]
    assert len(raw_files) == 2


def test_bundle_rejects_source_artifact_outside_allowed_roots(tmp_path: Path) -> None:
    database, odds_root, _, _, artifact_id = _database_with_artifacts(
        tmp_path / "source"
    )
    connection = connect(database)
    outside = tmp_path / "outside.json.gz"
    try:
        original = Path(
            str(
                connection.execute(
                    "SELECT storage_path FROM raw_source_artifacts"
                ).fetchone()[0]
            )
        )
        shutil.copy2(original, outside)
        relocate_raw_source_artifacts(
            connection,
            {artifact_id: outside},
            allowed_new_roots=[tmp_path],
            reason="test controlled relocation",
            actor="test_database_bundle",
            relocated_at=NOW,
        )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="allowed roots"):
        create_database_bundle(
            database,
            odds_root,
            tmp_path / "backup-bundle",
            git_commit=HEAD,
        )


@pytest.mark.parametrize("resume", [False, True])
def test_bundle_create_and_resume_reject_nonempty_source_wal_before_target_mutation(
    tmp_path: Path,
    resume: bool,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "backup-bundle"
    staging = tmp_path / ".backup-bundle.staging"
    if resume:
        staging.mkdir()
        (staging / "sentinel").write_text("unchanged", encoding="ascii")
    writer = connect(database)
    try:
        writer.execute("CREATE TABLE bundle_wal_gate(value INTEGER)")
        writer.commit()
        assert Path(f"{database}-wal").stat().st_size > 0

        with pytest.raises(RuntimeError, match="non-empty SQLite sidecars"):
            create_database_bundle(
                database,
                odds_root,
                bundle,
                allowed_source_roots=[source_root],
                resume=resume,
            )
    finally:
        writer.close()

    assert not bundle.exists()
    assert not database_bundle._operation_lock_path(bundle).exists()
    if resume:
        assert (staging / "sentinel").read_text(encoding="ascii") == "unchanged"
    else:
        assert not staging.exists()


def test_bundle_verify_and_restore_reject_committed_wal_only_data(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    bundle_database = _bundle_database_path(bundle)
    writer = _open_wal_only_writer(bundle_database, "wal_only_bundle_tamper")
    try:
        with pytest.raises(RuntimeError, match="non-empty SQLite sidecars"):
            verify_database_bundle(bundle)
        with pytest.raises(RuntimeError, match="non-empty SQLite sidecars"):
            restore_database_bundle(bundle, tmp_path / "restore")
    finally:
        writer.close()


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_bundle_verify_rejects_nonempty_transaction_sidecar(
    tmp_path: Path,
    suffix: str,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    sidecar = Path(f"{_bundle_database_path(bundle)}{suffix}")
    sidecar.write_bytes(b"unpublished-transaction-state")

    with pytest.raises(RuntimeError, match="non-empty SQLite sidecars"):
        verify_database_bundle(bundle)

    assert sidecar.read_bytes() == b"unpublished-transaction-state"


def test_bundle_verify_ignores_shm_content_as_non_durable_state(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    shm = Path(f"{_bundle_database_path(bundle)}-shm")
    shm.write_bytes(b"coordination-only-state")

    manifest = verify_database_bundle(bundle)

    assert manifest["database"]["sha256"] == _hash(_bundle_database_path(bundle))


def test_bundle_rejects_claimed_git_commit_that_is_not_head(tmp_path: Path) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    claimed = "0" * len(HEAD) if HEAD != "0" * len(HEAD) else "1" * len(HEAD)

    with pytest.raises(ValueError, match="must equal the current source HEAD"):
        create_database_bundle(
            database,
            odds_root,
            tmp_path / "must-not-publish",
            allowed_source_roots=[source_root],
            git_commit=claimed,
        )


def test_bundle_resume_rejects_in_place_source_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    original_copy = database_bundle._copy_artifact
    interrupted = False

    def interrupt(source: Path, destination: Path) -> None:
        nonlocal interrupted
        original_copy(source, destination)
        if not interrupted:
            interrupted = True
            raise RuntimeError("interrupt bundle")

    monkeypatch.setattr(database_bundle, "_copy_artifact", interrupt)
    with pytest.raises(RuntimeError, match="interrupt bundle"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
        )
    monkeypatch.setattr(database_bundle, "_copy_artifact", original_copy)
    with database.open("r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        original = handle.read(1)
        handle.seek(-1, os.SEEK_END)
        handle.write(bytes([original[0] ^ 0x01]))

    with pytest.raises(RuntimeError, match="source database file authority changed"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
            resume=True,
        )


def test_bundle_resume_rejects_same_path_source_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    original_copy = database_bundle._copy_artifact

    def interrupt(source: Path, destination: Path) -> None:
        original_copy(source, destination)
        raise RuntimeError("interrupt bundle")

    monkeypatch.setattr(database_bundle, "_copy_artifact", interrupt)
    with pytest.raises(RuntimeError, match="interrupt bundle"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
        )
    monkeypatch.setattr(database_bundle, "_copy_artifact", original_copy)
    replacement = database.with_name("replacement.db")
    shutil.copy2(database, replacement)
    os.replace(replacement, database)

    with pytest.raises(RuntimeError, match="source database file authority changed"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
            resume=True,
        )


def test_bundle_resume_rejects_runtime_dirty_set_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    original_copy = database_bundle._copy_artifact

    def interrupt(source: Path, destination: Path) -> None:
        original_copy(source, destination)
        raise RuntimeError("interrupt bundle")

    monkeypatch.setattr(database_bundle, "_copy_artifact", interrupt)
    with pytest.raises(RuntimeError, match="interrupt bundle"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
        )
    monkeypatch.setattr(database_bundle, "_copy_artifact", original_copy)
    monkeypatch.setattr(
        database_bundle,
        "_git_status_porcelain",
        lambda: ("?? data/new-runtime-output.log",),
    )

    with pytest.raises(RuntimeError, match="checkpoint binding mismatch"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
            resume=True,
        )


def test_bundle_rechecks_provenance_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    clean = {
        "source_tree_head": HEAD,
        "source_tree_clean": True,
        "source_tree_policy_version": database_bundle._SOURCE_TREE_POLICY_VERSION,
        "source_tree_runtime_dirty_paths": [],
    }
    changed = {
        **clean,
        "source_tree_runtime_dirty_paths": ["data/changed-during-create.log"],
    }
    calls = 0

    def changing_provenance() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return clean if calls == 1 else changed

    monkeypatch.setattr(
        database_bundle,
        "_source_tree_provenance",
        changing_provenance,
    )
    with pytest.raises(RuntimeError, match="provenance changed"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
        )
    checkpoint = json.loads(
        (tmp_path / ".bundle.staging" / "staging-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint["status"] == "ready"
    assert not bundle.exists()


def test_bundle_resume_recovers_runtime_prepare_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_managed_writer_scan(monkeypatch)
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    connection = connect(database)
    try:
        connection.execute("DROP TABLE runtime_schema_version")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == (
            "delete"
        )
    finally:
        connection.close()
    source_hash = _hash(database)
    calls = _track_physical_database_hashes(monkeypatch)
    bundle = tmp_path / "bundle"
    original_prepare = database_bundle._prepare_runtime_database
    interrupted = False

    def prepare_then_crash(snapshot: Path) -> None:
        nonlocal interrupted
        original_prepare(snapshot)
        if not interrupted:
            interrupted = True
            raise RuntimeError("crash after runtime prepare")

    monkeypatch.setattr(
        database_bundle,
        "_prepare_runtime_database",
        prepare_then_crash,
    )
    with pytest.raises(RuntimeError, match="crash after runtime prepare"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
        )
    staging = tmp_path / ".bundle.staging"
    checkpoint = json.loads(
        (staging / "staging-manifest.json").read_text(encoding="utf-8")
    )
    assert checkpoint["status"] == "snapshot_pending"
    staging_database = _bundle_database_path(staging)
    assert staging_database.is_file()
    sidecars = sqlite_sidecar_state(staging_database)
    assert int(sidecars["wal"]["bytes"]) == 0
    assert int(sidecars["journal"]["bytes"]) == 0
    prepared = connect(staging_database, read_only=True)
    try:
        status = verify_runtime_schema(prepared)
        assert status.contract_digest == RUNTIME_SCHEMA_CONTRACT_DIGEST
    finally:
        prepared.close()

    monkeypatch.setattr(
        database_bundle,
        "_prepare_runtime_database",
        original_prepare,
    )
    calls.clear()
    resumed = create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        resume=True,
    )
    assert resumed.bundle_directory == bundle
    assert _hash(database) == source_hash
    assert len(calls) <= 2, _database_hash_trace(calls)
    verify_database_bundle(bundle)


def test_bundle_resume_adopts_ancestor_revision_only_from_snapshot_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_managed_writer_scan(monkeypatch)
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    current_provenance = database_bundle._source_tree_provenance()
    old_head = subprocess.run(
        ["git", "rev-parse", f"{HEAD}^"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    assert old_head != HEAD
    old_provenance = {
        **current_provenance,
        "source_tree_head": old_head,
    }
    original_provenance = database_bundle._source_tree_provenance
    original_prepare = database_bundle._prepare_runtime_database

    def prepare_then_crash(snapshot: Path) -> None:
        original_prepare(snapshot)
        raise RuntimeError("crash before snapshot checkpoint")

    monkeypatch.setattr(
        database_bundle,
        "_source_tree_provenance",
        lambda: dict(old_provenance),
    )
    monkeypatch.setattr(
        database_bundle,
        "_prepare_runtime_database",
        prepare_then_crash,
    )
    with pytest.raises(RuntimeError, match="crash before snapshot checkpoint"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
        )
    staging = tmp_path / ".bundle.staging"
    checkpoint_path = staging / database_bundle._STAGING_MANIFEST_FILE
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["status"] == "snapshot_pending"
    assert checkpoint["git_commit"] == old_head
    original_checkpoint = checkpoint_path.read_bytes()

    monkeypatch.setattr(
        database_bundle,
        "_source_tree_provenance",
        original_provenance,
    )
    monkeypatch.setattr(
        database_bundle,
        "_prepare_runtime_database",
        original_prepare,
    )
    with pytest.raises(RuntimeError, match="checkpoint binding mismatch"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
            resume=True,
        )
    with pytest.raises(RuntimeError, match="differs from checkpoint"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
            resume=True,
            adopt_resume_from_git_commit="0" * len(old_head),
        )

    real_ancestor_check = database_bundle._require_git_commit_ancestor

    def reject_ancestor(_old: str, _current: str) -> None:
        raise RuntimeError("checkpoint git commit is not an ancestor")

    monkeypatch.setattr(
        database_bundle,
        "_require_git_commit_ancestor",
        reject_ancestor,
    )
    with pytest.raises(RuntimeError, match="not an ancestor"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
            resume=True,
            adopt_resume_from_git_commit=old_head,
        )
    monkeypatch.setattr(
        database_bundle,
        "_require_git_commit_ancestor",
        real_ancestor_check,
    )

    with pytest.raises(RuntimeError, match="checkpoint binding mismatch"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root, tmp_path / "changed-root"],
            resume=True,
            adopt_resume_from_git_commit=old_head,
        )

    displaced = tmp_path / "original-source.db"
    os.replace(database, displaced)
    shutil.copy2(displaced, database)
    try:
        with pytest.raises(RuntimeError, match="source database file authority changed"):
            create_database_bundle(
                database,
                odds_root,
                bundle,
                allowed_source_roots=[source_root],
                resume=True,
                adopt_resume_from_git_commit=old_head,
            )
    finally:
        database.unlink()
        os.replace(displaced, database)
    assert checkpoint_path.read_bytes() == original_checkpoint

    calls = _track_physical_database_hashes(monkeypatch)
    backup_calls: list[Path] = []
    original_backup = database_bundle.online_backup

    def tracked_backup(
        source: Path,
        destination: Path,
        **kwargs: object,
    ) -> None:
        backup_calls.append(destination.resolve())
        original_backup(source, destination, **kwargs)

    monkeypatch.setattr(database_bundle, "online_backup", tracked_backup)
    resumed = create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        resume=True,
        adopt_resume_from_git_commit=old_head,
    )

    assert resumed.bundle_directory == bundle
    assert backup_calls == [_bundle_database_path(staging).resolve()]
    assert len(calls) <= 2, _database_hash_trace(calls)
    manifest = verify_database_bundle(bundle)
    recovery = manifest["provenance_recovery"]
    assert recovery["from"] == {
        "git_commit": old_head,
        **old_provenance,
    }
    assert recovery["to"] == {
        "git_commit": HEAD,
        **current_provenance,
    }
    assert datetime.fromisoformat(recovery["adopted_at"]).utcoffset() is not None

    tampered = dict(manifest)
    tampered_recovery = dict(recovery)
    tampered_to = dict(tampered_recovery["to"])
    tampered_to["git_commit"] = old_head
    tampered_recovery["to"] = tampered_to
    tampered["provenance_recovery"] = tampered_recovery
    (bundle / "manifest.json").write_text(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="provenance recovery audit is invalid"):
        verify_database_bundle(bundle)


def test_bundle_resume_completes_after_rename_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    original_publish = database_bundle._publish_directory

    def publish_then_crash(staging: Path, target: Path) -> None:
        original_publish(staging, target)
        raise RuntimeError("crash after rename")

    monkeypatch.setattr(database_bundle, "_publish_directory", publish_then_crash)
    with pytest.raises(RuntimeError, match="crash after rename"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
        )
    assert bundle.is_dir()
    assert not (tmp_path / ".bundle.staging").exists()
    checkpoint = json.loads(
        (bundle / "staging-manifest.json").read_text(encoding="utf-8")
    )
    assert checkpoint["status"] == "publishing"
    with pytest.raises(RuntimeError, match="publication is incomplete"):
        verify_database_bundle(bundle)

    monkeypatch.setattr(database_bundle, "_publish_directory", original_publish)
    resumed = create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        resume=True,
    )
    assert resumed.bundle_directory == bundle
    assert not (bundle / "staging-manifest.json").exists()
    verify_database_bundle(bundle)


def test_completed_bundle_resume_is_idempotent(tmp_path: Path) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    created = create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )

    resumed = create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        resume=True,
    )
    assert resumed.database_sha256 == created.database_sha256


def test_concurrent_create_resume_is_rejected_by_target_lock(tmp_path: Path) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"

    with database_bundle.SingleInstanceLock(
        database_bundle._operation_lock_path(bundle.resolve())
    ):
        with pytest.raises(RuntimeError, match="lock is already held"):
            create_database_bundle(
                database,
                odds_root,
                bundle,
                allowed_source_roots=[source_root],
                resume=True,
            )
    assert not bundle.exists()


def test_bundle_database_hardlink_is_rejected(tmp_path: Path) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    alias = tmp_path / "bundle-database-alias.db"
    try:
        os.link(_bundle_database_path(bundle), alias)
    except OSError:
        pytest.skip("filesystem does not support hardlinks")

    with pytest.raises(RuntimeError, match="backup bundle database file is unsafe"):
        verify_database_bundle(bundle)


def test_restore_resume_rejects_same_path_bundle_database_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    original_copy = database_bundle._copy_artifact

    def interrupt(source: Path, destination: Path) -> None:
        original_copy(source, destination)
        raise RuntimeError("interrupt restore")

    monkeypatch.setattr(database_bundle, "_copy_artifact", interrupt)
    with pytest.raises(RuntimeError, match="interrupt restore"):
        restore_database_bundle(bundle, target)
    monkeypatch.setattr(database_bundle, "_copy_artifact", original_copy)
    bundle_database = _bundle_database_path(bundle)
    replacement = bundle / "replacement.sqlite"
    shutil.copy2(bundle_database, replacement)
    os.replace(replacement, bundle_database)

    with pytest.raises(RuntimeError, match="checkpoint binding mismatch"):
        restore_database_bundle(bundle, target, resume=True)


def test_restore_rejects_manifest_replacement_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    original_verify = database_bundle.verify_database_bundle
    replaced = False

    def verify_then_replace(
        bundle_directory: str | Path,
        *,
        _allow_staging_checkpoint: bool = False,
    ) -> dict[str, object]:
        nonlocal replaced
        manifest = original_verify(
            bundle_directory,
            _allow_staging_checkpoint=_allow_staging_checkpoint,
        )
        if not replaced:
            replaced = True
            replacement = dict(manifest)
            replacement["created_at"] = "2026-07-19T00:00:00+00:00"
            database_bundle._write_json(
                bundle / database_bundle._MANIFEST_FILE,
                replacement,
            )
        return manifest

    monkeypatch.setattr(
        database_bundle,
        "verify_database_bundle",
        verify_then_replace,
    )
    with pytest.raises(RuntimeError, match="manifest changed after verification"):
        restore_database_bundle(bundle, target)
    assert {path.name for path in target.iterdir()} == {
        "dota2.service.lock",
        "dota2.web.lock",
    }
    assert not (target / "dota2.db").exists()
    assert not (tmp_path / ".restore.restore-staging").exists()


def test_restore_resume_rejects_artifact_change_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    original_clear = database_bundle._clear_quiescent_sqlite_sidecars

    def interrupt_before_staging_clear(restored_database: Path) -> None:
        if ".restore.restore-staging" in restored_database.parts:
            raise RuntimeError("interrupt before restore verification")
        original_clear(restored_database)

    monkeypatch.setattr(
        database_bundle,
        "_clear_quiescent_sqlite_sidecars",
        interrupt_before_staging_clear,
    )
    with pytest.raises(RuntimeError, match="interrupt before restore verification"):
        restore_database_bundle(bundle, target)
    staging = tmp_path / ".restore.restore-staging"
    checkpoint = json.loads(
        (staging / "staging-manifest.json").read_text(encoding="utf-8")
    )
    assert checkpoint["status"] == "preparing"

    monkeypatch.setattr(
        database_bundle,
        "_clear_quiescent_sqlite_sidecars",
        original_clear,
    )
    original_verify = database_bundle.verify_database_bundle
    changed = False

    def verify_then_change_artifact(
        bundle_directory: str | Path,
        *,
        _allow_staging_checkpoint: bool = False,
    ) -> dict[str, object]:
        nonlocal changed
        manifest = original_verify(
            bundle_directory,
            _allow_staging_checkpoint=_allow_staging_checkpoint,
        )
        if not changed:
            changed = True
            artifact = bundle / str(manifest["artifacts"][0]["bundle_path"])
            payload = bytearray(artifact.read_bytes())
            payload[-1] ^= 0x01
            artifact.write_bytes(payload)
        return manifest

    monkeypatch.setattr(
        database_bundle,
        "verify_database_bundle",
        verify_then_change_artifact,
    )
    with pytest.raises(RuntimeError, match="source artifact differs from manifest"):
        restore_database_bundle(bundle, target, resume=True)
    assert {path.name for path in target.iterdir()} == {
        "dota2.service.lock",
        "dota2.web.lock",
    }
    assert not (target / "dota2.db").exists()
    assert staging.is_dir()


def test_restore_resume_recovers_prepared_verification_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    original_verify = database_bundle.verify_prepared_database
    interrupted = False

    def verify_then_crash(restored_database: Path, **kwargs: object) -> object:
        nonlocal interrupted
        result = original_verify(restored_database, **kwargs)
        if (
            ".restore.restore-staging" in restored_database.parts
            and not interrupted
        ):
            interrupted = True
            raise RuntimeError("crash after restore prepared verification")
        return result

    monkeypatch.setattr(
        database_bundle,
        "verify_prepared_database",
        verify_then_crash,
    )
    with pytest.raises(RuntimeError, match="crash after restore prepared verification"):
        restore_database_bundle(bundle, target)
    staging = tmp_path / ".restore.restore-staging"
    checkpoint = json.loads(
        (staging / "staging-manifest.json").read_text(encoding="utf-8")
    )
    assert checkpoint["status"] == "preparing"

    monkeypatch.setattr(
        database_bundle,
        "verify_prepared_database",
        original_verify,
    )
    restored = restore_database_bundle(bundle, target, resume=True)
    assert restored.database.is_file()
    assert not staging.exists()


def test_restore_resume_completes_after_rename_crash(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"

    def publish_then_crash(phase: str) -> None:
        if phase == "published:database:renamed":
            raise RuntimeError("crash after restore rename")

    with pytest.raises(RuntimeError, match="crash after restore rename"):
        restore_database_bundle(bundle, target, _phase_hook=publish_then_crash)
    staging = tmp_path / ".restore.restore-staging"
    checkpoint = json.loads(
        (staging / "staging-manifest.json").read_text(encoding="utf-8")
    )
    assert checkpoint["status"] == "publishing"
    assert checkpoint["database_published"] is False
    assert (target / "dota2.db").is_file()

    restored = restore_database_bundle(bundle, target, resume=True)
    assert restored.database.is_file()
    assert not staging.exists()


def test_restore_resume_rejects_committed_wal_only_staging_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    original_publish = database_bundle._publish_restore_items

    def interrupt_before_publish(*_: object, **__: object) -> None:
        raise RuntimeError("interrupt before restore publication")

    monkeypatch.setattr(
        database_bundle,
        "_publish_restore_items",
        interrupt_before_publish,
    )
    with pytest.raises(RuntimeError, match="interrupt before restore publication"):
        restore_database_bundle(bundle, target)
    monkeypatch.setattr(
        database_bundle,
        "_publish_restore_items",
        original_publish,
    )

    staging_database = _restore_staging_database_path(target)
    writer = _open_wal_only_writer(
        staging_database,
        "wal_only_restore_tamper",
    )
    try:
        with pytest.raises(RuntimeError, match="non-empty SQLite sidecars"):
            restore_database_bundle(bundle, target, resume=True)
    finally:
        writer.close()


def test_final_database_managed_writer_is_excluded_through_final_review(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    probe_result: subprocess.CompletedProcess[str] | None = None
    probe = "\n".join(
        (
            "import sqlite3, sys",
            "from pathlib import Path",
            "from live_betting.service_coordination import database_writer_authority",
            "database = Path(sys.argv[1])",
            "with database_writer_authority(database):",
            "    connection = sqlite3.connect(database)",
            "    connection.execute('CREATE TABLE forbidden_racing_writer(value)')",
            "    connection.commit()",
            "    connection.close()",
        )
    )

    def race_after_rename(phase: str) -> None:
        nonlocal probe_result
        if phase != "published:database:renamed":
            return
        probe_result = subprocess.run(
            [sys.executable, "-c", probe, str(target / "dota2.db")],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    restored = restore_database_bundle(
        bundle,
        target,
        _phase_hook=race_after_rename,
    )

    assert probe_result is not None
    assert probe_result.returncode != 0
    assert "lock is already held" in probe_result.stderr
    connection = connect(restored.database, read_only=True)
    try:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='forbidden_racing_writer'"
        ).fetchone() is None
    finally:
        connection.close()


def test_completed_restore_resume_is_idempotent(tmp_path: Path) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    first = restore_database_bundle(bundle, target)

    resumed = restore_database_bundle(bundle, target, resume=True)
    assert resumed.restored_database_sha256 == first.restored_database_sha256


def test_restore_rejects_target_database_hardlink_after_publish(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    alias = tmp_path / "target-database-alias.db"

    def publish_and_link(phase: str) -> None:
        if phase == "published:database:renamed":
            os.link(target / "dota2.db", alias)

    try:
        with pytest.raises(RuntimeError, match="exactly one hard link"):
            restore_database_bundle(bundle, target, _phase_hook=publish_and_link)
    except OSError:
        pytest.skip("filesystem does not support hardlinks")
    finally:
        alias.unlink(missing_ok=True)

    restored = restore_database_bundle(bundle, target, resume=True)
    assert restored.database.is_file()


def test_restore_final_review_rejects_replacement_after_checkpoint_cleanup(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    final_database = target / "dota2.db"
    displaced_database = tmp_path / "verified-restore.db"
    replacement = tmp_path / "replacement.db"
    replacement_bytes = b"not-a-sqlite-database"
    replacement.write_bytes(replacement_bytes)

    def replace_after_checkpoint_cleanup(phase: str) -> None:
        if phase == "published:checkpoint-cleared":
            os.replace(final_database, displaced_database)
            os.replace(replacement, final_database)

    with pytest.raises(
        RuntimeError,
        match="final restored database file authority|identity changed",
    ):
        restore_database_bundle(
            bundle,
            target,
            _phase_hook=replace_after_checkpoint_cleanup,
        )

    assert final_database.read_bytes() == replacement_bytes
    assert displaced_database.is_file()
    assert (target / "restore-manifest.json").is_file()


def test_restore_final_review_rejects_partially_forged_completed_manifest(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    completed_manifest = target / "restore-manifest.json"

    def forge_after_checkpoint_cleanup(phase: str) -> None:
        if phase != "published:checkpoint-cleared":
            return
        payload = json.loads(completed_manifest.read_text(encoding="utf-8"))
        payload["artifact_count"] = int(payload["artifact_count"]) + 1
        completed_manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="completed restore manifest"):
        restore_database_bundle(
            bundle,
            target,
            _phase_hook=forge_after_checkpoint_cleanup,
        )

    forged = json.loads(completed_manifest.read_text(encoding="utf-8"))
    assert forged["artifact_count"] == 3
    assert (target / "dota2.db").is_file()


def test_restore_lock_exit_rejects_database_replacement_after_final_review(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    final_database = target / "dota2.db"
    displaced = tmp_path / "reviewed-restore.db"
    replacement = tmp_path / "replacement-restore.db"

    def replace_after_final_review(phase: str) -> None:
        if phase != "completed:authority-reviewed":
            return
        os.replace(final_database, displaced)
        shutil.copy2(displaced, replacement)
        os.replace(replacement, final_database)

    with pytest.raises(RuntimeError, match="identity changed"):
        restore_database_bundle(
            bundle,
            target,
            _phase_hook=replace_after_final_review,
        )

    assert final_database.is_file()
    assert displaced.is_file()
    assert _hash(final_database) == _hash(displaced)
    assert final_database.stat().st_ino != displaced.stat().st_ino


def test_restore_lock_exit_rejects_replacement_at_database_publish_boundary(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    final_database = target / "dota2.db"
    displaced = tmp_path / "published-restore.db"
    replacement = tmp_path / "replacement-restore.db"

    def replace_at_publish(phase: str) -> None:
        if phase != "published:database:renamed":
            return
        os.replace(final_database, displaced)
        shutil.copy2(displaced, replacement)
        os.replace(replacement, final_database)
        raise RuntimeError("failure after database publication")

    with pytest.raises(RuntimeError, match="identity changed"):
        restore_database_bundle(
            bundle,
            target,
            _phase_hook=replace_at_publish,
        )

    assert final_database.is_file()
    assert displaced.is_file()
    assert _hash(final_database) == _hash(displaced)
    assert final_database.stat().st_ino != displaced.stat().st_ino


def test_restore_binds_published_identity_before_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    final_database = target / "dota2.db"
    displaced = tmp_path / "published-before-fsync.db"
    replacement = tmp_path / "replacement-before-fsync.db"
    replacement.write_bytes(b"replacement-after-publish-before-fsync")
    real_fsync = database_bundle.fsync_directory
    injected = False

    def replace_then_fail_fsync(path: Path) -> None:
        nonlocal injected
        if final_database.exists() and not injected:
            injected = True
            os.replace(final_database, displaced)
            os.replace(replacement, final_database)
            raise RuntimeError("injected database publish fsync failure")
        real_fsync(path)

    monkeypatch.setattr(
        database_bundle,
        "fsync_directory",
        replace_then_fail_fsync,
    )
    with pytest.raises(RuntimeError, match="identity changed"):
        restore_database_bundle(bundle, target)

    assert injected
    assert displaced.is_file()
    assert final_database.read_bytes() == b"replacement-after-publish-before-fsync"


@pytest.mark.parametrize("attack", ["replace", "hardlink", "symlink"])
def test_restore_rejects_completed_manifest_authority_change_after_review(
    tmp_path: Path,
    attack: str,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    manifest_path = target / "restore-manifest.json"
    displaced = tmp_path / "reviewed-restore-manifest.json"
    alias = tmp_path / "restore-manifest-alias.json"

    def attack_after_final_review(phase: str) -> None:
        if phase != "completed:authority-reviewed":
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
            restore_database_bundle(
                bundle,
                target,
                _phase_hook=attack_after_final_review,
            )
    finally:
        alias.unlink(missing_ok=True)


def test_restore_rejects_manifest_replacement_after_publication_rename(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    manifest_path = target / "restore-manifest.json"
    displaced = tmp_path / "published-restore-manifest.json"

    def replace_after_rename(phase: str) -> None:
        if phase != "published:restore-manifest.json:renamed":
            return
        os.replace(manifest_path, displaced)
        shutil.copy2(displaced, manifest_path)

    with pytest.raises(RuntimeError, match="file authority changed"):
        restore_database_bundle(
            bundle,
            target,
            _phase_hook=replace_after_rename,
        )

    assert manifest_path.is_file()
    assert displaced.is_file()
    assert _hash(manifest_path) == _hash(displaced)
    assert manifest_path.stat().st_ino != displaced.stat().st_ino


def test_concurrent_restore_resume_is_rejected_by_target_lock(tmp_path: Path) -> None:
    target = tmp_path / "restore"
    with database_bundle.SingleInstanceLock(
        database_bundle._operation_lock_path(target.resolve())
    ):
        with pytest.raises(RuntimeError, match="lock is already held"):
            restore_database_bundle(tmp_path / "bundle", target, resume=True)


def test_restore_publishes_dependencies_before_database_visibility(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    phases: list[str] = []

    def observe(phase: str) -> None:
        phases.append(phase)
        for lock_path in (
            target / "dota2.service.lock",
            target / "dota2.web.lock",
            database_bundle._operation_lock_path(target.resolve()),
        ):
            with pytest.raises(RuntimeError, match="lock is already held"):
                with SingleInstanceLock(lock_path):
                    pass
        if phase == "published:database:renamed":
            assert (target / "live_betting").is_dir()
            assert (target / "raw-sources").is_dir()
            assert (target / "restore-manifest.json").is_file()
            assert (target / "dota2.db").is_file()
        elif phase.endswith(":renamed"):
            assert not (target / "dota2.db").exists()

    restored = restore_database_bundle(bundle, target, _phase_hook=observe)

    assert restored.database.is_file()
    assert phases[-4:] == [
        "published:database:renamed",
        "published:ready",
        "published:checkpoint-cleared",
        "completed:authority-reviewed",
    ]


def test_restore_recovers_each_publication_move_boundary(tmp_path: Path) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    phases = (
        "published:live_betting:renamed",
        "published:raw-sources:renamed",
        "published:restore-manifest.json:renamed",
        "published:database:renamed",
    )
    for index, crash_phase in enumerate(phases):
        target = tmp_path / f"restore-{index}"

        def crash(phase: str, expected: str = crash_phase) -> None:
            if phase == expected:
                raise RuntimeError(f"injected {expected}")

        with pytest.raises(RuntimeError, match="injected published"):
            restore_database_bundle(bundle, target, _phase_hook=crash)
        if crash_phase == "published:database:renamed":
            assert (target / "dota2.db").is_file()
        else:
            assert not (target / "dota2.db").exists()

        restored = restore_database_bundle(bundle, target, resume=True)
        assert restored.database.is_file()
        assert not (tmp_path / f".restore-{index}.restore-staging").exists()


def test_restore_accepts_preexisting_root_with_only_canonical_lock_files(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"
    target.mkdir()
    (target / "dota2.service.lock").write_bytes(b"stale")
    (target / "dota2.web.lock").write_bytes(b"stale")

    restored = restore_database_bundle(bundle, target)

    assert restored.database.is_file()


def test_restore_resume_recovers_empty_staging_after_checkpoint_clear(
    tmp_path: Path,
) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    bundle = tmp_path / "bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )
    target = tmp_path / "restore"

    def crash(phase: str) -> None:
        if phase == "published:checkpoint-cleared":
            raise RuntimeError("injected checkpoint cleanup crash")

    with pytest.raises(RuntimeError, match="checkpoint cleanup crash"):
        restore_database_bundle(bundle, target, _phase_hook=crash)
    staging = tmp_path / ".restore.restore-staging"
    assert staging.is_dir()
    assert list(staging.iterdir()) == []
    assert (target / "dota2.db").is_file()

    restored = restore_database_bundle(bundle, target, resume=True)

    assert restored.database.is_file()
    assert not staging.exists()


@pytest.mark.parametrize(
    "lock_kind",
    ["global-service", "global-web", "service", "web"],
)
def test_restore_is_blocked_by_each_final_database_lock(
    tmp_path: Path,
    lock_kind: str,
) -> None:
    target = tmp_path / "restore"
    target.mkdir()
    final_database = target / "dota2.db"

    lock_path = {
        "global-service": database_global_service_lock_path(final_database),
        "global-web": database_global_web_lock_path(final_database),
        "service": final_database.with_suffix(".service.lock"),
        "web": final_database.with_suffix(".web.lock"),
    }[lock_kind]
    with SingleInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="lock is already held"):
            restore_database_bundle(tmp_path / "missing-bundle", target)

    assert not final_database.exists()


def test_core_only_source_produces_runtime_ready_bundle(tmp_path: Path) -> None:
    database, odds_root, source_root, _, _ = _database_with_artifacts(
        tmp_path / "source"
    )
    connection = connect(database)
    try:
        connection.execute("DROP TABLE runtime_schema_version")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    source_hash = _hash(database)
    bundle = tmp_path / "bundle"

    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
    )

    assert _hash(database) == source_hash
    verify_database_bundle(bundle)
    bundled = connect(_bundle_database_path(bundle), read_only=True)
    try:
        assert (
            bundled.execute(
                "SELECT MAX(version) FROM runtime_schema_version"
            ).fetchone()[0]
            == 1
        )
    finally:
        bundled.close()
