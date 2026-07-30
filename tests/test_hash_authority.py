from __future__ import annotations
# ruff: noqa: E402

import os
from pathlib import Path

import pytest

hash_authority = pytest.importorskip(
    "live_betting.hash_authority",
    reason="file hash authority was retired with SQLite file operations",
)
from live_betting.database_bundle import _publish_directory, _replace_and_fsync
from live_betting.hash_authority import (
    file_hash_authority_scope,
    hash_authority_scope_covers,
    read_hashed_file,
)
from live_betting.service_coordination import SingleInstanceLock


def _count_hash_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Path]:
    reads: list[Path] = []
    real_read = hash_authority.read_stable_regular_file

    def counted(path: Path, **kwargs: object):
        reads.append(Path(path).absolute())
        return real_read(path, **kwargs)

    monkeypatch.setattr(hash_authority, "read_stable_regular_file", counted)
    return reads


def test_hash_scope_requires_a_current_process_lock(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="current-process lock is not held"):
        with file_hash_authority_scope(required_locks=(tmp_path / "missing.lock",)):
            pytest.fail("unlocked hash authority reached the body")


def test_direct_hash_cache_read_cannot_bypass_scope_locks(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"stable")

    with pytest.raises(RuntimeError, match="scope has no current-process locks"):
        hash_authority.FileHashAuthorityCache().read(
            path,
            label="payload",
            include_payload=False,
            database=False,
        )


def test_hash_scope_exception_clears_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"stable")
    lock_path = tmp_path / "authority.lock"
    reads = _count_hash_reads(monkeypatch)

    with SingleInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="stop"):
            with file_hash_authority_scope(required_locks=(lock_path,)):
                read_hashed_file(path, label="payload")
                read_hashed_file(path, label="payload")
                raise RuntimeError("stop")
        with file_hash_authority_scope(required_locks=(lock_path,)):
            read_hashed_file(path, label="payload")

    assert reads == [path.absolute(), path.absolute()]


def test_hash_scope_revalidates_lock_before_cache_reuse(
    tmp_path: Path,
) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"stable")
    lock_path = tmp_path / "authority.lock"
    lock = SingleInstanceLock(lock_path)
    lock.__enter__()
    try:
        with file_hash_authority_scope(required_locks=(lock_path,)):
            read_hashed_file(path, label="payload")
            lock.__exit__()
            with pytest.raises(
                RuntimeError,
                match="current-process lock is not held",
            ):
                read_hashed_file(path, label="payload")
    finally:
        lock.__exit__()


def test_nested_additional_lock_isolates_hash_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"stable")
    first_lock_path = tmp_path / "first" / "authority.lock"
    second_lock_path = tmp_path / "second" / "authority.lock"
    reads = _count_hash_reads(monkeypatch)

    with SingleInstanceLock(first_lock_path):
        with file_hash_authority_scope(required_locks=(first_lock_path,)):
            assert hash_authority_scope_covers((first_lock_path,))
            assert not hash_authority_scope_covers((second_lock_path,))
            read_hashed_file(path, label="payload")
            with SingleInstanceLock(second_lock_path):
                with file_hash_authority_scope(required_locks=(second_lock_path,)):
                    assert hash_authority_scope_covers(
                        (first_lock_path, second_lock_path)
                    )
                    read_hashed_file(path, label="payload")
                    read_hashed_file(path, label="payload")
            read_hashed_file(path, label="payload")

    assert reads == [path.absolute()] * 3


def test_same_size_in_place_mutation_invalidates_hash_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"before")
    lock_path = tmp_path / "authority.lock"
    reads = _count_hash_reads(monkeypatch)

    with SingleInstanceLock(lock_path):
        with file_hash_authority_scope(required_locks=(lock_path,)):
            before = read_hashed_file(path, label="payload").sha256
            with path.open("r+b") as handle:
                handle.write(b"after!")
                handle.flush()
                os.fsync(handle.fileno())
            after = read_hashed_file(path, label="payload").sha256

    assert before != after
    assert reads == [path.absolute(), path.absolute()]


def test_external_rename_aba_invalidates_hash_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "payload.bin"
    displaced = tmp_path / "payload.displaced"
    malicious = tmp_path / "payload.malicious"
    captured = tmp_path / "payload.captured"
    path.write_bytes(b"trusted")
    malicious.write_bytes(b"hostile")
    lock_path = tmp_path / "authority.lock"
    reads = _count_hash_reads(monkeypatch)

    with SingleInstanceLock(lock_path):
        with file_hash_authority_scope(required_locks=(lock_path,)):
            expected = read_hashed_file(path, label="payload").sha256
            os.replace(path, displaced)
            os.replace(malicious, path)
            os.replace(path, captured)
            os.replace(displaced, path)
            actual = read_hashed_file(path, label="payload").sha256

    assert actual == expected
    assert reads == [path.absolute(), path.absolute()]


def test_sqlite_sidecar_changes_invalidate_database_hash_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "database.db"
    database.write_bytes(b"database")
    sidecar = Path(f"{database}-shm")
    lock_path = tmp_path / "authority.lock"
    reads = _count_hash_reads(monkeypatch)

    with SingleInstanceLock(lock_path):
        with file_hash_authority_scope(required_locks=(lock_path,)):
            expected = read_hashed_file(
                database,
                label="database",
                database=True,
            ).sha256
            sidecar.touch()
            with_sidecar = read_hashed_file(
                database,
                label="database",
                database=True,
            ).sha256
            sidecar.unlink()
            without_sidecar = read_hashed_file(
                database,
                label="database",
                database=True,
            ).sha256

    assert expected == with_sidecar == without_sidecar
    assert reads == [database.absolute()] * 3


def test_controlled_file_rename_rebinds_without_rehash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"stable")
    lock_path = tmp_path / "authority.lock"
    reads = _count_hash_reads(monkeypatch)

    with SingleInstanceLock(lock_path):
        with file_hash_authority_scope(required_locks=(lock_path,)):
            expected = read_hashed_file(source, label="source").sha256
            _replace_and_fsync(source, destination)
            actual = read_hashed_file(destination, label="destination").sha256

    assert actual == expected
    assert reads == [source.absolute()]


def test_controlled_rename_does_not_authorize_unrelated_external_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    victim = tmp_path / "victim.bin"
    displaced = tmp_path / "victim.displaced"
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    victim.write_bytes(b"trusted")
    source.write_bytes(b"stable")
    lock_path = tmp_path / "authority.lock"
    reads = _count_hash_reads(monkeypatch)

    with SingleInstanceLock(lock_path):
        with file_hash_authority_scope(required_locks=(lock_path,)):
            expected = read_hashed_file(victim, label="victim").sha256
            os.replace(victim, displaced)
            os.replace(displaced, victim)
            _replace_and_fsync(source, destination)
            actual = read_hashed_file(victim, label="victim").sha256

    assert actual == expected
    assert reads == [victim.absolute(), victim.absolute()]


def test_controlled_directory_publish_rebinds_children_without_rehash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    target = tmp_path / "published"
    staging.mkdir()
    source = staging / "database.db"
    source.write_bytes(b"stable")
    lock_path = tmp_path / "authority.lock"
    reads = _count_hash_reads(monkeypatch)

    with SingleInstanceLock(lock_path):
        with file_hash_authority_scope(required_locks=(lock_path,)):
            expected = read_hashed_file(
                source,
                label="source database",
                database=True,
            ).sha256
            _publish_directory(staging, target)
            destination = target / source.name
            actual = read_hashed_file(
                destination,
                label="published database",
                database=True,
            ).sha256

    assert actual == expected
    assert reads == [source.absolute()]
