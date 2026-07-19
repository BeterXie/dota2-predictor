"""Operation-scoped reuse of cryptographically verified file identities."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path

from .service_coordination import (
    StableRegularFile,
    read_stable_regular_file,
    require_current_process_lock,
)


_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass(frozen=True)
class _FileSignature:
    logical_path: Path
    resolved_path: Path
    device: int
    inode: int
    mode: int
    nlink: int
    bytes: int
    mtime_ns: int
    ctime_ns: int

    def matches(self, snapshot: StableRegularFile) -> bool:
        return (
            self.resolved_path == snapshot.resolved_path
            and self.device == snapshot.device
            and self.inode == snapshot.inode
            and self.mode == snapshot.mode
            and self.nlink == snapshot.nlink
            and self.bytes == snapshot.bytes
            and self.mtime_ns == snapshot.mtime_ns
            and self.ctime_ns == snapshot.ctime_ns
        )

    def same_physical_file(self, other: _FileSignature) -> bool:
        return (
            self.device == other.device
            and self.inode == other.inode
            and self.mode == other.mode
            and self.nlink == other.nlink
            and self.bytes == other.bytes
            and self.mtime_ns == other.mtime_ns
            and self.ctime_ns == other.ctime_ns
        )


@dataclass(frozen=True)
class _DirectorySignature:
    logical_path: Path
    device: int
    inode: int
    mode: int
    nlink: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _FileHashToken:
    signature: _FileSignature
    parent: _DirectorySignature
    sidecars: tuple[_FileSignature | None, ...]
    snapshot: StableRegularFile


def _logical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _capture_signature(
    path: Path,
    *,
    label: str,
    allow_missing: bool = False,
) -> _FileSignature | None:
    logical = _logical_path(path)
    try:
        initial = logical.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise RuntimeError(f"{label} file is missing: {logical}") from None
    except OSError as error:
        raise RuntimeError(f"{label} file is unverifiable: {logical}") from error
    if (
        not stat.S_ISREG(initial.st_mode)
        or int(initial.st_nlink) != 1
        or (
            os.name == "nt"
            and int(getattr(initial, "st_file_attributes", 0))
            & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        )
    ):
        raise RuntimeError(f"{label} file is unsafe: {logical}")
    try:
        resolved = logical.resolve(strict=True)
        completed = logical.lstat()
    except OSError as error:
        raise RuntimeError(f"{label} file changed: {logical}") from error
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(
        int(getattr(initial, field)) != int(getattr(completed, field))
        for field in fields
    ):
        raise RuntimeError(f"{label} file changed: {logical}")
    if int(completed.st_ino) <= 0:
        raise RuntimeError(f"{label} file identity is unavailable: {logical}")
    return _FileSignature(
        logical_path=logical,
        resolved_path=resolved,
        device=int(completed.st_dev),
        inode=int(completed.st_ino),
        mode=int(completed.st_mode),
        nlink=int(completed.st_nlink),
        bytes=int(completed.st_size),
        mtime_ns=int(completed.st_mtime_ns),
        ctime_ns=int(completed.st_ctime_ns),
    )


def _capture_sidecars(
    database: Path,
    *,
    label: str,
) -> tuple[_FileSignature | None, ...]:
    return tuple(
        _capture_signature(
            Path(f"{_logical_path(database)}{suffix}"),
            label=f"{label} SQLite sidecar",
            allow_missing=True,
        )
        for suffix in _SQLITE_SIDECAR_SUFFIXES
    )


def _capture_parent(path: Path, *, label: str) -> _DirectorySignature:
    logical = _logical_path(path).parent
    try:
        metadata = logical.lstat()
    except OSError as error:
        raise RuntimeError(f"{label} parent is unverifiable: {logical}") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (
            os.name == "nt"
            and int(getattr(metadata, "st_file_attributes", 0))
            & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        )
    ):
        raise RuntimeError(f"{label} parent is unsafe: {logical}")
    return _DirectorySignature(
        logical_path=logical,
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        mode=int(metadata.st_mode),
        nlink=int(metadata.st_nlink),
        mtime_ns=int(metadata.st_mtime_ns),
        ctime_ns=int(metadata.st_ctime_ns),
    )


class FileHashAuthorityCache:
    """Reuse one SHA only while every observable file identity field is stable."""

    def __init__(self) -> None:
        self._tokens: dict[tuple[Path, bool], _FileHashToken] = {}

    def read(
        self,
        path: Path,
        *,
        label: str,
        include_payload: bool,
        database: bool,
    ) -> StableRegularFile:
        _require_scope_locks()
        logical = _logical_path(path)
        key = (logical, database)
        before = _capture_signature(logical, label=label)
        assert before is not None
        before_parent = _capture_parent(logical, label=label)
        before_sidecars = (
            _capture_sidecars(logical, label=label) if database else ()
        )
        cached = self._tokens.get(key)
        if (
            not include_payload
            and cached is not None
            and cached.signature == before
            and cached.parent == before_parent
            and cached.sidecars == before_sidecars
        ):
            return cached.snapshot

        snapshot = read_stable_regular_file(
            logical,
            label=label,
            include_payload=include_payload,
        )
        after = _capture_signature(logical, label=label)
        assert after is not None
        after_parent = _capture_parent(logical, label=label)
        after_sidecars = _capture_sidecars(logical, label=label) if database else ()
        if (
            before != after
            or before_parent != after_parent
            or before_sidecars != after_sidecars
            or not after.matches(snapshot)
        ):
            self._tokens.pop(key, None)
            raise RuntimeError(f"{label} file changed during cryptographic verification")
        cached_snapshot = replace(snapshot, payload=None)
        self._tokens[key] = _FileHashToken(
            signature=after,
            parent=after_parent,
            sidecars=after_sidecars,
            snapshot=cached_snapshot,
        )
        return snapshot if include_payload else cached_snapshot

    def invalidate(self, *paths: Path, recursive: bool = False) -> None:
        roots = tuple(_logical_path(path) for path in paths)
        for key in tuple(self._tokens):
            logical, _ = key
            if logical in roots or (
                recursive
                and any(logical.is_relative_to(root) for root in roots)
            ):
                self._tokens.pop(key, None)

    def rebind(self, source: Path, destination: Path, *, recursive: bool) -> None:
        _require_scope_locks()
        source_root = _logical_path(source)
        destination_root = _logical_path(destination)
        candidates = [
            (key, token)
            for key, token in self._tokens.items()
            if key[0] == source_root
            or (recursive and key[0].is_relative_to(source_root))
        ]
        self.invalidate(destination_root, recursive=recursive)
        for (old_path, database), token in candidates:
            self._tokens.pop((old_path, database), None)
            relative = old_path.relative_to(source_root)
            new_path = destination_root / relative
            try:
                old_path.lstat()
            except FileNotFoundError:
                pass
            except OSError:
                continue
            else:
                continue
            try:
                current = _capture_signature(new_path, label="rebound hash authority")
                assert current is not None
                if not token.signature.same_physical_file(current):
                    continue
                if database:
                    if any(
                        sidecar is not None
                        for sidecar in _capture_sidecars(
                            old_path,
                            label="rebound hash authority source",
                        )
                    ):
                        continue
                    current_sidecars = _capture_sidecars(
                        new_path,
                        label="rebound hash authority destination",
                    )
                    if len(current_sidecars) != len(token.sidecars) or any(
                        (left is None) != (right is None)
                        or (
                            left is not None
                            and right is not None
                            and not left.same_physical_file(right)
                        )
                        for left, right in zip(
                            token.sidecars,
                            current_sidecars,
                            strict=True,
                        )
                    ):
                        continue
                else:
                    current_sidecars = ()
            except (OSError, RuntimeError):
                continue
            rebound_snapshot = replace(
                token.snapshot,
                resolved_path=current.resolved_path,
            )
            self._tokens[(new_path, database)] = _FileHashToken(
                signature=current,
                parent=_capture_parent(new_path, label="rebound hash authority"),
                sidecars=current_sidecars,
                snapshot=rebound_snapshot,
            )

    def clear(self) -> None:
        self._tokens.clear()


_CURRENT_CACHE: ContextVar[FileHashAuthorityCache | None] = ContextVar(
    "dota2_file_hash_authority_cache",
    default=None,
)
_CURRENT_LOCKS: ContextVar[tuple[Path, ...]] = ContextVar(
    "dota2_file_hash_authority_locks",
    default=(),
)


def _require_scope_locks() -> None:
    locks = _CURRENT_LOCKS.get()
    if not locks:
        raise RuntimeError("hash authority scope has no current-process locks")
    try:
        for lock in locks:
            require_current_process_lock(lock)
    except BaseException:
        cache = _CURRENT_CACHE.get()
        if cache is not None:
            cache.clear()
        raise


@contextmanager
def file_hash_authority_scope(
    *,
    required_locks: Iterable[Path],
) -> Iterator[FileHashAuthorityCache]:
    locks = tuple(dict.fromkeys(_logical_path(path) for path in required_locks))
    if not locks:
        raise ValueError("hash authority scope requires at least one held lock")
    for lock in locks:
        require_current_process_lock(lock)
    existing = _CURRENT_CACHE.get()
    inherited_locks = _CURRENT_LOCKS.get()
    combined_locks = tuple(dict.fromkeys((*inherited_locks, *locks)))
    locks_token = _CURRENT_LOCKS.set(combined_locks)
    if existing is not None:
        added_authority = combined_locks != inherited_locks
        if added_authority:
            existing.clear()
        try:
            yield existing
        finally:
            if added_authority:
                existing.clear()
            _CURRENT_LOCKS.reset(locks_token)
        return
    cache = FileHashAuthorityCache()
    cache_token = _CURRENT_CACHE.set(cache)
    try:
        yield cache
    finally:
        cache.clear()
        _CURRENT_CACHE.reset(cache_token)
        _CURRENT_LOCKS.reset(locks_token)


def read_hashed_file(
    path: Path,
    *,
    label: str,
    include_payload: bool = False,
    database: bool = False,
) -> StableRegularFile:
    cache = _CURRENT_CACHE.get()
    if cache is None:
        return read_stable_regular_file(
            path,
            label=label,
            include_payload=include_payload,
        )
    return cache.read(
        path,
        label=label,
        include_payload=include_payload,
        database=database,
    )


def invalidate_hashed_paths(*paths: Path, recursive: bool = False) -> None:
    cache = _CURRENT_CACHE.get()
    if cache is not None:
        cache.invalidate(*paths, recursive=recursive)


def rebind_hashed_paths(
    source: Path,
    destination: Path,
    *,
    recursive: bool = False,
) -> None:
    cache = _CURRENT_CACHE.get()
    if cache is not None:
        cache.rebind(source, destination, recursive=recursive)


def hash_authority_scope_covers(required_locks: Iterable[Path]) -> bool:
    if _CURRENT_CACHE.get() is None:
        return False
    required = tuple(dict.fromkeys(_logical_path(path) for path in required_locks))
    held = frozenset(_CURRENT_LOCKS.get())
    if any(lock not in held for lock in required):
        return False
    _require_scope_locks()
    return True


__all__ = [
    "FileHashAuthorityCache",
    "file_hash_authority_scope",
    "hash_authority_scope_covers",
    "invalidate_hashed_paths",
    "rebind_hashed_paths",
    "read_hashed_file",
]
