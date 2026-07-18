from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import live_betting.database_bundle as database_bundle
import live_betting.odds_legacy_compactor as odds_legacy_compactor

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
)
from live_betting.markets import normalized_state_hash, snapshots_from_payload
from live_betting.storage import LiveBettingStore
from live_betting.vision import VisionObservation
from live_betting.vision_frame_registry import (
    publish_vision_frame_bytes,
    verify_registered_vision_frame,
)
from shared.sqlite import connect


NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)


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
        git_commit="a" * 40,
    )

    assert result.artifact_count == 2
    assert _hash(database) == source_hash
    manifest = verify_database_bundle(bundle)
    assert manifest["git_commit"] == "a" * 40
    assert manifest["artifact_count"] == 2
    assert manifest["source_tree_clean"] is True
    assert (
        manifest["source_tree_policy_version"]
        == database_bundle._SOURCE_TREE_POLICY_VERSION
    )
    assert manifest["source_tree_head"] == database_bundle._git_commit()
    assert manifest["source_tree_runtime_dirty_paths"] == []

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
            git_commit="b" * 40,
        )


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
        git_commit="c" * 40,
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
    def unavailable(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
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
        git_commit="d" * 40,
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
            git_commit="e" * 40,
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
    database, odds_root, raw_source_root, _, _ = _database_with_artifacts(
        source_root
    )
    original = _add_vision_frame(database, source_root)
    bundle = tmp_path / "backup-bundle"
    created = create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[raw_source_root],
        git_commit="e" * 40,
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
    database, odds_root, raw_source_root, _, _ = _database_with_artifacts(
        source_root
    )
    _add_vision_frame(database, source_root)
    bundle = tmp_path / "backup-bundle"
    create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[raw_source_root],
        git_commit="d" * 40,
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
    assert not (tmp_path / "must-not-restore").exists()


def test_bundle_rejects_registered_vision_frame_with_hardlink(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    database, odds_root, raw_source_root, _, _ = _database_with_artifacts(
        source_root
    )
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
            git_commit="1" * 40,
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
        git_commit="b" * 40,
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
    assert not target.exists()


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
        git_commit="c" * 40,
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
        git_commit="f" * 40,
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
            git_commit="1" * 40,
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
            git_commit="1" * 40,
        )
    with pytest.raises(RuntimeError, match="binding mismatch"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
            git_commit="2" * 40,
            resume=True,
        )

    monkeypatch.setattr(database_bundle, "_copy_artifact", original_copy)
    result = create_database_bundle(
        database,
        odds_root,
        bundle,
        allowed_source_roots=[source_root],
        git_commit="1" * 40,
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
    required = database_bundle._required_bundle_bytes(database)
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

    bundle = tmp_path / "backup-bundle"
    with pytest.raises(RuntimeError, match="insufficient free space"):
        create_database_bundle(
            database,
            odds_root,
            bundle,
            allowed_source_roots=[source_root],
            git_commit="3" * 40,
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
        git_commit="4" * 40,
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
    assert not target.exists()

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
        git_commit="5" * 40,
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
    staged = connect(staging / "dota2.db", read_only=True)
    try:
        assert staged.execute(
            "SELECT COUNT(*) FROM raw_source_artifact_relocations WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()[0] == 1
    finally:
        staged.close()

    monkeypatch.setattr(database_bundle, "_write_json", original_write)
    restored = restore_database_bundle(bundle, target, resume=True)
    connection = connect(restored.database, read_only=True)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM raw_source_artifact_relocations WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()[0] == 1
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
        git_commit="6" * 40,
    )
    target = tmp_path / "restored"
    staging = tmp_path / ".restored.restore-staging"
    staging.mkdir()
    with pytest.raises(RuntimeError, match="missing or invalid"):
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
        git_commit="d" * 40,
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
            str(connection.execute("SELECT storage_path FROM raw_source_artifacts").fetchone()[0])
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
            git_commit="e" * 40,
        )
