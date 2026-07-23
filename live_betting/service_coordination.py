"""Cross-process authority for database-scoped service managers."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import ntpath
import os
import secrets
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

import psutil


_LOCK_FILE_BYTES = 4096
_LOCK_OWNER_FORMAT = "dota2-service-lock-owner-v1"
_GLOBAL_AUTHORITY_DIRECTORY = "dota2-predictor-authority-v1"
_MAX_ANCESTOR_DEPTH = 64
_SUPERVISOR_AUTHORITY_ENV = "DOTA2_SUPERVISOR_AUTHORITY_V1"
_WEB_FETCH_AUTHORITY_ENV = "DOTA2_WEB_FETCH_AUTHORITY_V1"
_WEB_FETCH_AUTHORITY_VERSION = 3
_WEB_FETCH_AUTHORITY_ROLE = "fetch"
_WEB_FETCH_AUTHORITY_SUFFIX = ".web.fetch-authority.json"
_MANAGER_CHILD_AUTHORITY_ENV = "DOTA2_MANAGER_CHILD_AUTHORITY_V1"
_MANAGER_CHILD_AUTHORITY_VERSION = 3
_MANAGER_CHILD_AUTHORITY_SUFFIX = ".manager-child-authority"
MANAGED_CHILD_BOOTSTRAP_SCRIPT = (
    Path(__file__).resolve().parent.parent / "managed_child_bootstrap.py"
).resolve()
_MANAGED_CHILD_TARGET_SENTINEL = "--target-argv"
_MANAGED_CHILD_PYTHON_FLAGS = frozenset(
    {"-B", "-E", "-O", "-OO", "-s", "-u"}
)
_MANAGED_CHILD_REJECTED_PYTHON_FLAGS = frozenset({"-I", "-S"})
_MANAGER_CHILD_BIND_TIMEOUT_SECONDS = 1.0
_MANAGER_CHILD_BIND_POLL_SECONDS = 0.01
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_MANAGER_DELEGATIONS = {
    "vision_supervisor": frozenset({"vision_watcher"}),
}
_WINDOWS_PATH_OPTIONS = frozenset(
    {
        "--archive-root",
        "--backup-dir",
        "--coverage-report",
        "--database",
        "--evidence-dir",
        "--lock",
        "--log-dir",
        "--odds-raw-root",
        "--output",
        "--output-dir",
        "--raw-dir",
        "--report",
        "--source-archive-root",
        "--vision-jsonl",
    }
)
_PYTHON_PROCESS_NAMES = frozenset(
    {"python", "python.exe", "pythonw", "pythonw.exe"}
)
_DEFAULT_PROCESS_ITER = psutil.process_iter
_DEFAULT_PROCESS_FACTORY = psutil.Process
_WRITER_MODULES = frozenset(
    {
        "fetch.fetch_matchups",
        "fetch.fetch_stratz_matchups",
        "fetch.hero_meta",
        "fetch.main",
        "live_betting.browser_companion",
        "live_betting.draft_publisher",
        "live_betting.monitor",
        "live_betting.postmatch_monitor",
        "live_betting.shadow_monitor",
        "scripts.accept_strict_live_mapping",
        "scripts.assign_strict_event_roles",
        "scripts.backfill_historical_rosh",
        "scripts.backfill_early_game",
        "scripts.backfill_team_profiles",
        "scripts.build_strict_team_profiles",
        "scripts.cleanup_vision_evidence",
        "scripts.invalidate_vision_observations",
        "scripts.refresh_draft_prediction_validations",
        "scripts.run_comeback_shadow",
        "scripts.run_dota_shadow_service",
        "scripts.run_historical_rosh_worker",
        "scripts.run_notification_worker",
        "scripts.run_postmatch_labeler",
        "scripts.run_strict_draft_backtest",
        "scripts.run_strict_event_ingest",
        "scripts.score_strict_event_players",
        "scripts.supervise_raybet_streams",
        "scripts.watch_raybet_stream",
    }
)
_WRITER_SCRIPTS = frozenset(
    {
        MANAGED_CHILD_BOOTSTRAP_SCRIPT.name,
        "accept_strict_live_mapping.py",
        "assign_strict_event_roles.py",
        "backfill_historical_rosh.py",
        "backfill_early_game.py",
        "backfill_team_profiles.py",
        "build_strict_team_profiles.py",
        "cleanup_vision_evidence.py",
        "fetch_matchups.py",
        "fetch_stratz_matchups.py",
        "hero_meta.py",
        "invalidate_vision_observations.py",
        "refresh_draft_prediction_validations.py",
        "run_browser_companion.py",
        "run_comeback_shadow.py",
        "run_dota_shadow_service.py",
        "run_historical_rosh_worker.py",
        "run_notification_worker.py",
        "run_postmatch_labeler.py",
        "run_strict_draft_backtest.py",
        "run_strict_event_ingest.py",
        "score_strict_event_players.py",
        "supervise_raybet_streams.py",
        "watch_raybet_stream.py",
    }
)


@dataclass(frozen=True, order=True)
class ProcessIdentity:
    pid: int
    created_at: float


@dataclass(frozen=True)
class DatabaseFileIdentity:
    resolved_path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class DirectoryIdentity:
    path: Path
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class StableRegularFile:
    resolved_path: Path
    device: int
    inode: int
    mode: int
    nlink: int
    bytes: int
    mtime_ns: int
    ctime_ns: int
    sha256: str
    payload: bytes | None


@dataclass(frozen=True)
class LockOwnerMetadata:
    pid: int
    created_at: float
    nonce: str
    lock_path: Path
    lock_device: int
    lock_inode: int
    database_identity: DatabaseFileIdentity | None = None


class _ManagerMarkerState:
    def __init__(self, database: Path, marker_path: Path, marker: str) -> None:
        self.database = database.resolve()
        self.marker_path = marker_path
        self.marker = marker
        self.unbound_bytes = marker.encode("ascii")
        self.bound_bytes: bytes | None = None
        self.bound_identity: ProcessIdentity | None = None
        self.marker_file_identity: tuple[int, int] | None = None
        self.publisher_pid = os.getpid()
        self.closed = False
        self.guard = threading.RLock()


class _ManagerChildAuthority(dict[str, str]):
    def __init__(self, marker: str, state: _ManagerMarkerState) -> None:
        super().__init__({_MANAGER_CHILD_AUTHORITY_ENV: marker})
        self.state = state


class _ManagerChildEnvironment(dict[str, str]):
    def __init__(
        self,
        values: Mapping[str, str],
        state: _ManagerMarkerState,
    ) -> None:
        super().__init__(values)
        self.state = state


@dataclass(frozen=True)
class WriterScanResult:
    conflicts: tuple[ProcessIdentity, ...]
    unverifiable_pids: tuple[int, ...]

    @property
    def safe(self) -> bool:
        return not self.conflicts and not self.unverifiable_pids


@dataclass(frozen=True)
class TerminationResult:
    ok: bool
    detail: str | None = None


@dataclass(frozen=True)
class ServiceDataPaths:
    database: Path
    live_betting_root: Path
    source_archive_root: Path
    strict_coverage_report: Path
    odds_raw_root: Path
    vision_observations: Path
    vision_evidence: Path
    vision_logs: Path
    managed_logs: Path


class SingleOccurrenceAction(argparse.Action):
    """Reject repeated occurrences of a command-line option."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        del parser, option_string
        marker = f"_single_occurrence_seen_{self.dest}"
        if getattr(namespace, marker, False):
            raise argparse.ArgumentError(self, "may be specified only once")
        setattr(namespace, marker, True)
        setattr(namespace, self.dest, values)


def add_single_database_argument(
    parser: argparse.ArgumentParser,
    **kwargs: Any,
) -> argparse.Action:
    """Add one ``--database`` option that fails closed on duplicates."""

    if "action" in kwargs:
        raise TypeError("database argument action is fixed")
    kwargs.setdefault("type", Path)
    return parser.add_argument(
        "--database",
        action=SingleOccurrenceAction,
        **kwargs,
    )


def require_unique_database_file(
    database: Path,
    *,
    expected_identity: DatabaseFileIdentity | None = None,
    allow_missing: bool = False,
) -> DatabaseFileIdentity | None:
    """Fence one SQLite file against hardlink aliases and path replacement."""

    resolved = database.resolve()
    try:
        metadata = resolved.lstat()
    except FileNotFoundError:
        if allow_missing and expected_identity is None:
            return None
        raise
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"database path is not a regular file: {resolved}")
    if int(metadata.st_nlink) != 1:
        raise RuntimeError(
            "database file must have exactly one hard link: "
            f"{resolved} (st_nlink={metadata.st_nlink})"
        )
    identity = DatabaseFileIdentity(
        resolved_path=resolved,
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
    )
    if identity.inode <= 0:
        raise RuntimeError(f"database file identity is unavailable: {resolved}")
    if expected_identity is not None and identity != expected_identity:
        raise RuntimeError(
            "database file identity changed: "
            f"expected={expected_identity.resolved_path} actual={resolved}"
        )
    return identity


def capture_directory_identity(path: Path, *, label: str) -> DirectoryIdentity:
    """Capture one real directory without following a final symlink."""

    logical = Path(os.path.abspath(os.fspath(path)))
    try:
        metadata = logical.lstat()
    except OSError as error:
        raise RuntimeError(f"{label} directory is unverifiable: {logical}") from error
    if not stat.S_ISDIR(metadata.st_mode) or (
        os.name == "nt"
        and int(getattr(metadata, "st_file_attributes", 0))
        & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise RuntimeError(f"{label} path is not a directory: {logical}")
    identity = DirectoryIdentity(
        logical,
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
    )
    if identity.inode <= 0:
        raise RuntimeError(f"{label} directory identity is unavailable: {logical}")
    return identity


def require_directory_identity(
    expected: DirectoryIdentity,
    *,
    label: str,
) -> DirectoryIdentity:
    """Require a directory path to still name the captured physical directory."""

    current = capture_directory_identity(expected.path, label=label)
    if current != expected:
        raise RuntimeError(f"{label} directory identity changed: {expected.path}")
    return current


def read_stable_regular_file(
    path: Path,
    *,
    label: str,
    include_payload: bool = True,
) -> StableRegularFile:
    """Read and hash one unaliased regular file through one stable descriptor."""

    logical = Path(os.path.abspath(os.fspath(path)))
    try:
        initial = logical.lstat()
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
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(logical, flags)
    except OSError as error:
        raise RuntimeError(f"{label} file is unverifiable: {logical}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or int(opened.st_nlink) != 1:
            raise RuntimeError(f"{label} file is unsafe: {logical}")
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            if include_payload:
                chunks.append(chunk)
            digest.update(chunk)
        completed = os.fstat(descriptor)
    except OSError as error:
        raise RuntimeError(f"{label} file is unverifiable: {logical}") from error
    finally:
        os.close(descriptor)
    try:
        current = logical.lstat()
    except OSError as error:
        raise RuntimeError(f"{label} file changed: {logical}") from error
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(
        int(getattr(initial, field)) != int(getattr(opened, field))
        for field in stable_fields
    ) or any(
        int(getattr(opened, field)) != int(getattr(completed, field))
        for field in stable_fields
    ) or any(
        int(getattr(completed, field)) != int(getattr(current, field))
        for field in stable_fields
    ):
        raise RuntimeError(f"{label} file changed: {logical}")
    if (
        not stat.S_ISREG(current.st_mode)
        or int(current.st_nlink) != 1
        or (
            os.name == "nt"
            and int(getattr(current, "st_file_attributes", 0))
            & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        )
    ):
        raise RuntimeError(f"{label} file is unsafe: {logical}")
    payload = b"".join(chunks) if include_payload else None
    if (
        (payload is not None and len(payload) != int(completed.st_size))
        or int(completed.st_ino) <= 0
    ):
        raise RuntimeError(f"{label} file changed: {logical}")
    return StableRegularFile(
        resolved_path=logical.resolve(),
        device=int(completed.st_dev),
        inode=int(completed.st_ino),
        mode=int(completed.st_mode),
        nlink=int(completed.st_nlink),
        bytes=int(completed.st_size),
        mtime_ns=int(completed.st_mtime_ns),
        ctime_ns=int(completed.st_ctime_ns),
        sha256=digest.hexdigest(),
        payload=payload,
    )


def fsync_directory(path: Path) -> None:
    """Persist directory-entry changes on POSIX filesystems."""

    if os.name == "nt":
        return
    identity = capture_directory_identity(path, label="fsync parent")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(identity.path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                int(opened.st_dev),
                int(opened.st_ino),
                int(opened.st_mode),
            ) != (identity.device, identity.inode, identity.mode):
                raise RuntimeError(f"fsync parent directory changed: {identity.path}")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise RuntimeError(
            f"fsync parent directory is unverifiable: {identity.path}"
        ) from error
    require_directory_identity(identity, label="fsync parent")


def service_data_paths(database: Path) -> ServiceDataPaths:
    resolved = database.resolve()
    live_betting_root = resolved.parent / "live_betting"
    vision_logs = live_betting_root / "watcher_logs"
    return ServiceDataPaths(
        database=resolved,
        live_betting_root=live_betting_root,
        source_archive_root=resolved.parent / "raw-sources",
        strict_coverage_report=(
            resolved.parent / "reports" / "strict_event_coverage_latest.json"
        ),
        odds_raw_root=live_betting_root / "raw-v2",
        vision_observations=live_betting_root / "live_observations",
        vision_evidence=live_betting_root / "live_evidence",
        vision_logs=vision_logs,
        managed_logs=live_betting_root / "logs" / "managed",
    )


_HELD_LOCKS: dict[Path, "SingleInstanceLock"] = {}
_HELD_LOCKS_GUARD = threading.RLock()


def _lock_owner_path(path: Path) -> Path:
    logical = Path(os.path.abspath(os.fspath(path)))
    return logical.with_name(f"{logical.name}.owner")


def _database_identity_payload(
    identity: DatabaseFileIdentity | None,
) -> dict[str, object] | None:
    if identity is None:
        return None
    return {
        "resolved_path": str(identity.resolved_path),
        "device": identity.device,
        "inode": identity.inode,
    }


def _lock_owner_payload(owner: LockOwnerMetadata) -> dict[str, object]:
    return {
        "format": _LOCK_OWNER_FORMAT,
        "pid": owner.pid,
        "created_at": owner.created_at,
        "nonce": owner.nonce,
        "lock_path": str(owner.lock_path),
        "lock_device": owner.lock_device,
        "lock_inode": owner.lock_inode,
        "database_identity": _database_identity_payload(owner.database_identity),
    }


def _canonical_ascii_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _note_cleanup_error(
    primary: BaseException,
    cleanup: BaseException,
    *,
    label: str,
) -> None:
    primary.add_note(
        f"{label}: {type(cleanup).__name__}: {cleanup}"
    )


def _parse_database_identity(value: object) -> DatabaseFileIdentity:
    if not isinstance(value, Mapping) or set(value) != {
        "resolved_path",
        "device",
        "inode",
    }:
        raise ValueError("database file identity is invalid")
    identity = DatabaseFileIdentity(
        Path(str(value["resolved_path"])).resolve(),
        int(value["device"]),
        int(value["inode"]),
    )
    if identity.device < 0 or identity.inode <= 0:
        raise ValueError("database file identity is invalid")
    return identity


def _parse_lock_owner(value: object) -> LockOwnerMetadata:
    if not isinstance(value, Mapping) or set(value) != {
        "format",
        "pid",
        "created_at",
        "nonce",
        "lock_path",
        "lock_device",
        "lock_inode",
        "database_identity",
    }:
        raise ValueError("lock owner record is invalid")
    if value["format"] != _LOCK_OWNER_FORMAT:
        raise ValueError("lock owner record format differs")
    nonce = str(value["nonce"])
    if len(nonce) != 64 or any(
        character not in "0123456789abcdef" for character in nonce
    ):
        raise ValueError("lock owner nonce is invalid")
    raw_database = value["database_identity"]
    owner = LockOwnerMetadata(
        pid=int(value["pid"]),
        created_at=float(value["created_at"]),
        nonce=nonce,
        lock_path=Path(str(value["lock_path"])).resolve(),
        lock_device=int(value["lock_device"]),
        lock_inode=int(value["lock_inode"]),
        database_identity=(
            None
            if raw_database is None
            else _parse_database_identity(raw_database)
        ),
    )
    if owner.pid <= 0 or owner.lock_device < 0 or owner.lock_inode <= 0:
        raise ValueError("lock owner record is invalid")
    return owner


def _read_lock_owner(path: Path) -> LockOwnerMetadata:
    owner_path = _lock_owner_path(path)
    encoded = _read_stable_regular_file(owner_path, label="service lock owner")
    try:
        payload = json.loads(encoded)
        owner = _parse_lock_owner(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise RuntimeError(f"service lock owner is invalid: {owner_path}") from error
    if owner.lock_path != path.resolve():
        raise RuntimeError(f"service lock owner path differs: {path}")
    try:
        lock_stat = path.resolve().lstat()
    except OSError as error:
        raise RuntimeError(f"service lock file is unverifiable: {path}") from error
    if (
        not stat.S_ISREG(lock_stat.st_mode)
        or int(lock_stat.st_nlink) != 1
        or (int(lock_stat.st_dev), int(lock_stat.st_ino))
        != (owner.lock_device, owner.lock_inode)
    ):
        raise RuntimeError(f"service lock file identity differs: {path}")
    return owner


def require_current_process_lock(path: Path) -> LockOwnerMetadata:
    """Return the stable owner proof for a lock held by this process."""

    resolved = Path(os.path.abspath(os.fspath(path)))
    with _HELD_LOCKS_GUARD:
        lock = _HELD_LOCKS.get(resolved)
        if lock is None:
            raise RuntimeError(f"required current-process lock is not held: {resolved}")
        owner = lock.owner
    try:
        process = psutil.Process(os.getpid())
        identity = ProcessIdentity(int(process.pid), float(process.create_time()))
    except (OSError, TypeError, ValueError, psutil.Error) as error:
        raise RuntimeError("current-process lock owner is unverifiable") from error
    if not _identity_is_allowed(
        ProcessIdentity(owner.pid, owner.created_at),
        (identity,),
    ):
        raise RuntimeError(f"required lock owner differs: {resolved}")
    if _read_lock_owner(resolved) != owner:
        raise RuntimeError(f"required lock owner changed: {resolved}")
    return owner


class SingleInstanceLock:
    """Non-blocking whole-record OS lock with an owner-proof sidecar."""

    def __init__(self, path: Path) -> None:
        self.path = Path(os.path.abspath(os.fspath(path)))
        self._handle: Any = None
        self._owner: LockOwnerMetadata | None = None
        self._owner_bytes: bytes | None = None
        self._parent_identity: DirectoryIdentity | None = None

    @property
    def owner(self) -> LockOwnerMetadata:
        if self._owner is None:
            raise RuntimeError(f"service lock is not held: {self.path}")
        return self._owner

    def _write_owner(self, owner: LockOwnerMetadata) -> None:
        if self._handle is None:
            raise RuntimeError(f"service lock is not held: {self.path}")
        if self._parent_identity is None:
            raise RuntimeError(f"service lock parent is not bound: {self.path}")
        require_directory_identity(
            self._parent_identity,
            label="service lock parent",
        )
        lock_metadata = self.path.lstat()
        opened = os.fstat(self._handle.fileno())
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or int(lock_metadata.st_nlink) != 1
            or (int(lock_metadata.st_dev), int(lock_metadata.st_ino))
            != (int(opened.st_dev), int(opened.st_ino))
        ):
            raise RuntimeError(f"service lock file identity changed: {self.path}")
        encoded = _canonical_ascii_json(_lock_owner_payload(owner)) + b"\n"
        if len(encoded) > _LOCK_FILE_BYTES:
            raise RuntimeError("service lock owner record exceeds fixed lock range")
        record = encoded.ljust(_LOCK_FILE_BYTES, b" ")
        self._handle.seek(0)
        self._handle.write(record)
        self._handle.truncate(_LOCK_FILE_BYTES)
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.seek(0)

        owner_path = _lock_owner_path(self.path)
        temporary = owner_path.with_name(
            f".{owner_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        write_error: BaseException | None = None
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            require_directory_identity(
                self._parent_identity,
                label="service lock parent",
            )
            os.replace(temporary, owner_path)
            fsync_directory(owner_path.parent)
        except BaseException as error:
            write_error = error
            raise
        finally:
            try:
                if temporary.exists() or temporary.is_symlink():
                    temporary.unlink()
                    fsync_directory(temporary.parent)
            except BaseException as cleanup_error:
                if write_error is None:
                    raise
                _note_cleanup_error(
                    write_error,
                    cleanup_error,
                    label="service lock owner temporary cleanup failed",
                )
        require_directory_identity(
            self._parent_identity,
            label="service lock parent",
        )
        self._owner = owner
        self._owner_bytes = encoded

    def bind_database(
        self,
        identity: DatabaseFileIdentity,
        root: ProcessIdentity,
    ) -> LockOwnerMetadata:
        owner = self.owner
        if not _identity_is_allowed(
            ProcessIdentity(owner.pid, owner.created_at),
            (root,),
        ):
            raise RuntimeError(f"service lock owner identity differs: {self.path}")
        if owner.database_identity not in {None, identity}:
            raise RuntimeError(f"service lock database identity differs: {self.path}")
        if owner.database_identity is None:
            self._write_owner(
                LockOwnerMetadata(
                    owner.pid,
                    owner.created_at,
                    owner.nonce,
                    owner.lock_path,
                    owner.lock_device,
                    owner.lock_inode,
                    identity,
                )
            )
        return self.owner

    def __enter__(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._parent_identity = capture_directory_identity(
            self.path.parent,
            label="service lock parent",
        )
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._handle = os.fdopen(descriptor, "r+b", buffering=0)
        except BaseException as error:
            try:
                os.close(descriptor)
            except BaseException as cleanup_error:
                _note_cleanup_error(
                    error,
                    cleanup_error,
                    label="service lock descriptor cleanup failed",
                )
            raise
        locked = False
        try:
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() < _LOCK_FILE_BYTES:
                self._handle.seek(_LOCK_FILE_BYTES - 1)
                self._handle.write(b"\0")
                self._handle.flush()
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(
                    self._handle.fileno(),
                    msvcrt.LK_NBLCK,
                    _LOCK_FILE_BYTES,
                )
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BaseException as error:
            try:
                self._handle.close()
            except BaseException as cleanup_error:
                _note_cleanup_error(
                    error,
                    cleanup_error,
                    label="service lock handle cleanup failed",
                )
            finally:
                self._handle = None
                self._parent_identity = None
            if not isinstance(error, Exception):
                raise
            raise RuntimeError(f"service lock is already held: {self.path}") from error

        try:
            process = psutil.Process(os.getpid())
            metadata = os.fstat(self._handle.fileno())
            owner = LockOwnerMetadata(
                pid=os.getpid(),
                created_at=float(process.create_time()),
                nonce=secrets.token_hex(32),
                lock_path=self.path,
                lock_device=int(metadata.st_dev),
                lock_inode=int(metadata.st_ino),
            )
            self._write_owner(owner)
            with _HELD_LOCKS_GUARD:
                if self.path in _HELD_LOCKS:
                    raise RuntimeError(f"service lock registry collision: {self.path}")
                _HELD_LOCKS[self.path] = self
        except BaseException as error:
            cleanup_errors: list[tuple[str, BaseException]] = []
            try:
                with _HELD_LOCKS_GUARD:
                    if _HELD_LOCKS.get(self.path) is self:
                        del _HELD_LOCKS[self.path]
            except BaseException as cleanup_error:
                cleanup_errors.append(("service lock registry cleanup failed", cleanup_error))
            if self._owner_bytes is not None:
                owner_path = _lock_owner_path(self.path)
                try:
                    if self._parent_identity is None:
                        raise RuntimeError(
                            f"service lock parent is not bound: {self.path}"
                        )
                    require_directory_identity(
                        self._parent_identity,
                        label="service lock parent",
                    )
                    persisted = _read_stable_regular_file(
                        owner_path,
                        label="service lock owner",
                    )
                    if persisted != self._owner_bytes:
                        raise RuntimeError(
                            f"service lock owner changed: {self.path}"
                        )
                    owner_path.unlink()
                    fsync_directory(owner_path.parent)
                except BaseException as cleanup_error:
                    cleanup_errors.append(
                        ("service lock owner cleanup failed", cleanup_error)
                    )
            if locked:
                try:
                    self._unlock()
                except BaseException as cleanup_error:
                    cleanup_errors.append(
                        ("service lock unlock failed", cleanup_error)
                    )
            try:
                self._handle.close()
            except BaseException as cleanup_error:
                cleanup_errors.append(("service lock close failed", cleanup_error))
            finally:
                self._handle = None
                self._owner = None
                self._owner_bytes = None
                self._parent_identity = None
            for label, cleanup_error in cleanup_errors:
                _note_cleanup_error(error, cleanup_error, label=label)
            if not isinstance(error, Exception):
                raise
            raise RuntimeError(
                f"service lock metadata could not be written: {self.path}"
            ) from error
        return self

    def _unlock(self) -> None:
        assert self._handle is not None
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(
                self._handle.fileno(),
                msvcrt.LK_UNLCK,
                _LOCK_FILE_BYTES,
            )
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)

    def __exit__(self, *args: object) -> None:
        if self._handle is None:
            return
        body_error = (
            args[1]
            if len(args) > 1 and isinstance(args[1], BaseException)
            else None
        )
        cleanup_errors: list[tuple[str, BaseException]] = []
        try:
            with _HELD_LOCKS_GUARD:
                if _HELD_LOCKS.get(self.path) is self:
                    del _HELD_LOCKS[self.path]
                else:
                    cleanup_errors.append(
                        (
                            "service lock registry cleanup failed",
                            RuntimeError(
                                f"service lock registry changed: {self.path}"
                            ),
                        )
                    )
        except BaseException as cleanup_error:
            cleanup_errors.append(
                ("service lock registry cleanup failed", cleanup_error)
            )
        parent_stable = False
        try:
            if self._parent_identity is None:
                raise RuntimeError(f"service lock parent is not bound: {self.path}")
            require_directory_identity(
                self._parent_identity,
                label="service lock parent",
            )
            parent_stable = True
        except BaseException as cleanup_error:
            cleanup_errors.append(("service lock parent cleanup failed", cleanup_error))
        if parent_stable:
            owner_path = _lock_owner_path(self.path)
            try:
                persisted = _read_stable_regular_file(
                    owner_path,
                    label="service lock owner",
                )
                if persisted != self._owner_bytes:
                    raise RuntimeError(f"service lock owner changed: {self.path}")
                owner_path.unlink()
                fsync_directory(owner_path.parent)
            except BaseException as cleanup_error:
                cleanup_errors.append(
                    ("service lock owner cleanup failed", cleanup_error)
                )
        try:
            self._unlock()
        except BaseException as cleanup_error:
            cleanup_errors.append(("service lock unlock failed", cleanup_error))
        try:
            self._handle.close()
        except BaseException as cleanup_error:
            cleanup_errors.append(("service lock close failed", cleanup_error))
        finally:
            self._handle = None
            self._owner = None
            self._owner_bytes = None
            self._parent_identity = None
        if not cleanup_errors:
            return
        if body_error is not None:
            for label, cleanup_error in cleanup_errors:
                _note_cleanup_error(body_error, cleanup_error, label=label)
            return
        label, cleanup_error = cleanup_errors[0]
        cleanup_error.add_note(label)
        for secondary_label, secondary_error in cleanup_errors[1:]:
            _note_cleanup_error(
                cleanup_error,
                secondary_error,
                label=secondary_label,
            )
        raise cleanup_error


def database_service_lock_path(database: Path) -> Path:
    """Return the canonical supervisor/writer authority lock for one database."""

    return database.resolve().with_suffix(".service.lock")


def database_web_lock_path(database: Path) -> Path:
    """Return the canonical Web lifetime lock for one database."""

    return database.resolve().with_suffix(".web.lock")


def _global_authority_directory() -> DirectoryIdentity:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA is required for global authority")
        base = Path(os.path.abspath(local_app_data))
        base_identity = capture_directory_identity(
            base,
            label="global authority profile root",
        )
        product_root = base / "dota2-predictor"
        product_root.mkdir(mode=0o700, exist_ok=True)
        product_identity = capture_directory_identity(
            product_root,
            label="global authority product root",
        )
        root = product_root / "authority"
        root.mkdir(mode=0o700, exist_ok=True)
        identity = capture_directory_identity(root, label="global authority")
        require_directory_identity(
            base_identity,
            label="global authority profile root",
        )
        require_directory_identity(
            product_identity,
            label="global authority product root",
        )
        return require_directory_identity(identity, label="global authority")

    base = Path(tempfile.gettempdir()).absolute()
    root = base / f"{_GLOBAL_AUTHORITY_DIRECTORY}-{os.geteuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    identity = capture_directory_identity(root, label="global authority")
    metadata = identity.path.lstat()
    if int(metadata.st_uid) != int(os.geteuid()):
        raise RuntimeError("global authority directory owner differs")
    if stat.S_IMODE(identity.mode) != 0o700:
        raise RuntimeError("global authority directory mode must be 0700")
    return require_directory_identity(identity, label="global authority")


def _database_global_role_lock_path(database: Path, role: str) -> Path:
    logical = os.path.normcase(os.path.abspath(os.fspath(database)))
    digest = hashlib.sha256(os.fsencode(logical)).hexdigest()
    root = _global_authority_directory()
    require_directory_identity(root, label="global authority")
    return root.path / f"{digest}.{role}.lock"


def database_global_service_lock_path(database: Path) -> Path:
    """Return root-external service-role authority for one logical database."""

    return _database_global_role_lock_path(database, "service")


def database_global_web_lock_path(database: Path) -> Path:
    """Return root-external Web-role authority for one logical database."""

    return _database_global_role_lock_path(database, "web")


def database_global_authority_lock_paths(database: Path) -> tuple[Path, Path]:
    """Return root-external service then Web locks in canonical order."""

    return (
        database_global_service_lock_path(database),
        database_global_web_lock_path(database),
    )


def database_local_authority_lock_paths(database: Path) -> tuple[Path, Path]:
    """Return the canonical locks stored beside the database."""

    return (
        database_service_lock_path(database),
        database_web_lock_path(database),
    )


def database_service_authority_lock_paths(database: Path) -> tuple[Path, Path]:
    """Return global then service authority for a managed supervisor."""

    return (
        database_global_service_lock_path(database),
        database_service_lock_path(database),
    )


def database_web_authority_lock_paths(database: Path) -> tuple[Path, Path]:
    """Return global then Web authority for the Web process lifetime."""

    return (
        database_global_web_lock_path(database),
        database_web_lock_path(database),
    )


def database_authority_lock_paths(database: Path) -> tuple[Path, Path, Path, Path]:
    """Return both global locks then both local locks in canonical order."""

    return (
        *database_global_authority_lock_paths(database),
        *database_local_authority_lock_paths(database),
    )


def _resolve_stable_authority_ancestor(
    expected_identity: ProcessIdentity,
    *,
    parent_pid: int,
    process_factory: Callable[[int], Any],
    label: str,
) -> Any:
    current_pid = parent_pid
    visited: set[int] = set()
    chain: list[tuple[ProcessIdentity, int]] = []
    root: Any | None = None
    actual_identity: ProcessIdentity | None = None
    try:
        for _ in range(_MAX_ANCESTOR_DEPTH):
            if current_pid <= 0 or current_pid in visited:
                break
            visited.add(current_pid)
            process = process_factory(current_pid)
            identity = ProcessIdentity(
                int(process.pid),
                float(process.create_time()),
            )
            if identity.pid != current_pid:
                raise RuntimeError(f"{label} ancestor PID changed")
            if current_pid == expected_identity.pid:
                root = process
                actual_identity = identity
                break
            next_pid = int(process.ppid())
            chain.append((identity, next_pid))
            current_pid = next_pid
        if root is None or actual_identity is None:
            raise RuntimeError(f"{label} root is not an ancestor")
        if not _identity_is_allowed(actual_identity, (expected_identity,)):
            raise RuntimeError(f"{label} parent identity changed")
        for identity, expected_parent_pid in chain:
            process = process_factory(identity.pid)
            repeated = ProcessIdentity(
                int(process.pid),
                float(process.create_time()),
            )
            if not _identity_is_allowed(repeated, (identity,)):
                raise RuntimeError(f"{label} ancestor identity changed")
            if int(process.ppid()) != expected_parent_pid:
                raise RuntimeError(f"{label} ancestor chain changed")
    except (
        psutil.NoSuchProcess,
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        psutil.Error,
    ) as error:
        raise RuntimeError(f"{label} parent is unverifiable") from error
    return root


def _web_fetch_authority_path(database: Path, token: str) -> Path:
    resolved = database.resolve()
    return resolved.with_name(
        f".{resolved.name}{_WEB_FETCH_AUTHORITY_SUFFIX}.{token}.json"
    )


def _web_fetch_marker(
    database: Path,
    database_identity: DatabaseFileIdentity,
    identity: ProcessIdentity,
    token: str,
    marker_path: Path,
    root_command: list[str],
    global_lock_owner: LockOwnerMetadata,
    web_lock_owner: LockOwnerMetadata,
) -> str:
    return json.dumps(
        {
            "version": _WEB_FETCH_AUTHORITY_VERSION,
            "root_pid": identity.pid,
            "root_created_at": identity.created_at,
            "database": str(database.resolve()),
            "database_identity": _database_identity_payload(database_identity),
            "token": token,
            "marker_path": str(marker_path.resolve()),
            "role": _WEB_FETCH_AUTHORITY_ROLE,
            "root_command": root_command,
            "global_lock_owner": _lock_owner_payload(global_lock_owner),
            "web_lock_owner": _lock_owner_payload(web_lock_owner),
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _web_root_command(command: list[str]) -> bool:
    module = any(
        argument == "-m" and command[index + 1] == "web.main"
        for index, argument in enumerate(command[:-1])
    )
    script = any(
        Path(argument).name == "main.py" and Path(argument).parent.name == "web"
        for argument in command
    )
    uvicorn_module = any(
        argument == "-m" and command[index + 1] == "uvicorn"
        for index, argument in enumerate(command[:-1])
    )
    uvicorn_executable = bool(command) and Path(command[0]).stem.casefold() == "uvicorn"
    uvicorn_app = "web.app:app" in command
    return module or script or ((uvicorn_module or uvicorn_executable) and uvicorn_app)


def _command_or_environment_database(
    command: list[str],
    environment: Mapping[str, str],
) -> Path:
    command_values: list[str] = []
    for index, argument in enumerate(command):
        if argument == "--database":
            if index + 1 >= len(command):
                raise ValueError("database argument is missing its value")
            command_values.append(command[index + 1])
        elif argument.startswith("--database="):
            command_values.append(argument.split("=", 1)[1])
    if len(command_values) > 1:
        raise ValueError("database argument is repeated")
    values = list(command_values)
    configured = environment.get("DATABASE_PATH")
    if configured:
        values.append(configured)
    if not values:
        raise ValueError("database authority is missing")
    resolved: list[Path] = []
    for value in values:
        candidate = Path(value)
        if not candidate.is_absolute():
            raise ValueError("database authority path is relative")
        resolved.append(candidate.resolve())
    if len(set(resolved)) != 1:
        raise ValueError("database authorities differ")
    return resolved[0]


def _require_lock_held(
    path: Path,
    lock_factory: Callable[[Path], Any],
) -> None:
    try:
        with lock_factory(path):
            pass
    except RuntimeError as error:
        if "already held" in str(error):
            return
        raise
    raise RuntimeError(f"required service lock is not held: {path}")


def _read_stable_regular_file_snapshot(
    path: Path,
    *,
    label: str,
) -> tuple[bytes, tuple[int, int]]:
    snapshot = read_stable_regular_file(path, label=label)
    assert snapshot.payload is not None
    return snapshot.payload, (snapshot.device, snapshot.inode)


def _read_stable_regular_file(path: Path, *, label: str) -> bytes:
    return _read_stable_regular_file_snapshot(path, label=label)[0]


def _require_lock_held_by(
    path: Path,
    root: ProcessIdentity,
    lock_factory: Callable[[Path], Any],
    process_factory: Callable[[int], Any],
    *,
    database_identity: DatabaseFileIdentity,
    expected_owner: LockOwnerMetadata | None = None,
) -> LockOwnerMetadata:
    del process_factory
    try:
        owner = _read_lock_owner(path)
    except RuntimeError as error:
        raise RuntimeError(
            f"required service lock is not held by authority root: {path}"
        ) from error
    if not _identity_is_allowed(
        ProcessIdentity(owner.pid, owner.created_at),
        (root,),
    ):
        raise RuntimeError(
            f"required service lock is not held by authority root: {path}"
        )
    if owner.database_identity != database_identity:
        raise RuntimeError(f"required service lock database identity differs: {path}")
    if expected_owner is not None and owner != expected_owner:
        raise RuntimeError(f"required service lock owner token differs: {path}")
    _require_lock_held(path, lock_factory)
    if _read_lock_owner(path) != owner:
        raise RuntimeError(f"required service lock owner changed: {path}")
    return owner


def _bind_current_lock_owner(
    path: Path,
    root: ProcessIdentity,
    database_identity: DatabaseFileIdentity,
) -> LockOwnerMetadata:
    resolved = path.resolve()
    if root.pid != os.getpid():
        raise RuntimeError("service lock authority root is not the current process")
    with _HELD_LOCKS_GUARD:
        lock = _HELD_LOCKS.get(resolved)
        if lock is None:
            raise RuntimeError(
                f"service lock owner token is unavailable: {resolved}"
            )
        owner = lock.bind_database(database_identity, root)
    persisted = _read_lock_owner(resolved)
    if persisted != owner:
        raise RuntimeError(f"service lock owner token differs: {resolved}")
    return owner


def _manager_role(value: object) -> str:
    role = str(value)
    if (
        not role
        or len(role) > 64
        or not role[0].islower()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in role)
    ):
        raise ValueError("manager child authority role is invalid")
    return role


def _manager_command(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("manager child authority command is invalid")
    command = [str(argument) for argument in value]
    if any(not argument or "\x00" in argument for argument in command):
        raise ValueError("manager child authority command is invalid")
    return command


def _windows_path_option(value: str) -> bool:
    return value in _WINDOWS_PATH_OPTIONS


def _windows_path_argument(command: list[str], index: int) -> bool:
    argument = command[index]
    if index == 0:
        return True
    if index > 0 and _windows_path_option(command[index - 1]):
        return True
    option, separator, value = argument.partition("=")
    if separator and _windows_path_option(option) and value:
        return True
    return (
        index > 0
        and argument.lower().endswith((".py", ".pyw"))
        and all(
            item in {"-B", "-E", "-I", "-O", "-OO", "-s", "-S", "-u"}
            for item in command[1:index]
        )
    )


def _normalize_windows_path_argument(argument: str) -> str:
    option, separator, value = argument.partition("=")
    if separator and _windows_path_option(option) and value:
        return f"{option}={ntpath.normcase(ntpath.normpath(value))}"
    return ntpath.normcase(ntpath.normpath(argument))


def _windows_commands_match(left: list[str], right: list[str]) -> bool:
    return _command_comparison_key(left, windows=True) == _command_comparison_key(
        right, windows=True
    )


def _command_comparison_key(
    command: list[str],
    *,
    windows: bool,
) -> tuple[str, ...]:
    if not windows:
        return tuple(command)
    return tuple(
        _normalize_windows_path_argument(argument)
        if _windows_path_argument(command, index)
        else argument
        for index, argument in enumerate(command)
    )


def command_comparison_key(command: list[str]) -> tuple[str, ...]:
    return _command_comparison_key(command, windows=os.name == "nt")


def _commands_match(left: list[str], right: list[str]) -> bool:
    if os.name == "nt":
        return _windows_commands_match(left, right)
    return left == right


def _manager_marker_path(database: Path, token: str) -> Path:
    return database.resolve().with_name(
        f".{database.resolve().name}{_MANAGER_CHILD_AUTHORITY_SUFFIX}.{token}.json"
    )


def _manager_marker(
    database: Path,
    database_identity: DatabaseFileIdentity,
    root: ProcessIdentity,
    token: str,
    marker_path: Path,
    role: str,
    command: list[str],
    delegate_roles: tuple[str, ...],
    lock_owners: tuple[LockOwnerMetadata, ...],
) -> str:
    return json.dumps(
        {
            "version": _MANAGER_CHILD_AUTHORITY_VERSION,
            "root_pid": root.pid,
            "root_created_at": root.created_at,
            "database": str(database.resolve()),
            "database_identity": _database_identity_payload(database_identity),
            "token": token,
            "marker_path": str(marker_path.resolve()),
            "role": role,
            "command": command,
            "delegate_roles": list(delegate_roles),
            "root_lock_owners": [
                _lock_owner_payload(owner) for owner in lock_owners
            ],
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_manager_marker(
    database: Path,
    marker: str,
) -> tuple[
    ProcessIdentity,
    Path,
    str,
    list[str],
    tuple[str, ...],
    DatabaseFileIdentity,
    tuple[LockOwnerMetadata, ...],
]:
    try:
        payload = json.loads(marker)
        if not isinstance(payload, dict):
            raise TypeError("marker must be an object")
        if set(payload) != {
            "version",
            "root_pid",
            "root_created_at",
            "database",
            "database_identity",
            "token",
            "marker_path",
            "role",
            "command",
            "delegate_roles",
            "root_lock_owners",
        }:
            raise ValueError("marker fields differ")
        if int(payload["version"]) != _MANAGER_CHILD_AUTHORITY_VERSION:
            raise ValueError("marker version differs")
        root = ProcessIdentity(
            int(payload["root_pid"]),
            float(payload["root_created_at"]),
        )
        marker_database = Path(str(payload["database"])).resolve()
        database_identity = _parse_database_identity(payload["database_identity"])
        token = str(payload["token"])
        marker_path = Path(str(payload["marker_path"])).resolve()
        role = _manager_role(payload["role"])
        command = _manager_command(payload["command"])
        raw_delegates = payload["delegate_roles"]
        raw_owners = payload["root_lock_owners"]
        if not isinstance(raw_delegates, list) or not isinstance(raw_owners, list):
            raise TypeError("marker collections are invalid")
        delegate_roles = tuple(_manager_role(item) for item in raw_delegates)
        lock_owners = tuple(_parse_lock_owner(item) for item in raw_owners)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("manager child authority marker is invalid") from error
    resolved = database.resolve()
    if marker_database != resolved:
        raise RuntimeError("manager child authority database differs")
    current_database_identity = require_unique_database_file(resolved)
    if current_database_identity != database_identity:
        raise RuntimeError("manager child authority database file identity differs")
    if len(token) != 64 or any(
        character not in "0123456789abcdef" for character in token
    ):
        raise RuntimeError("manager child authority token is invalid")
    if marker_path != _manager_marker_path(resolved, token):
        raise RuntimeError("manager child authority marker path differs")
    owner_paths = tuple(owner.lock_path for owner in lock_owners)
    supported_lock_sets = {
        database_service_authority_lock_paths(resolved),
        database_authority_lock_paths(resolved),
    }
    if owner_paths not in supported_lock_sets:
        raise RuntimeError("manager child authority root locks differ")
    if any(
        owner.database_identity != database_identity
        or not _identity_is_allowed(
            ProcessIdentity(owner.pid, owner.created_at),
            (root,),
        )
        for owner in lock_owners
    ):
        raise RuntimeError("manager child authority lock owner differs")
    if len(set(delegate_roles)) != len(delegate_roles) or set(delegate_roles) - set(
        _MANAGER_DELEGATIONS.get(role, ())
    ):
        raise RuntimeError("manager child authority delegation differs")
    if _command_database(command) != resolved:
        raise RuntimeError("manager child authority command database differs")
    return (
        root,
        marker_path,
        role,
        command,
        delegate_roles,
        database_identity,
        lock_owners,
    )


def _manager_marker_state(
    authority: Mapping[str, str],
) -> _ManagerMarkerState:
    if isinstance(authority, (_ManagerChildAuthority, _ManagerChildEnvironment)):
        return authority.state
    raise RuntimeError("manager child authority was not issued by this process")


def _bound_manager_marker(marker: str, identity: ProcessIdentity) -> bytes:
    payload = json.loads(marker)
    payload["child_identity"] = {
        "pid": identity.pid,
        "created_at": identity.created_at,
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _replace_polled_manager_marker(
    source: Path,
    destination: Path,
    *,
    expected_payload: bytes,
    expected_identity: tuple[int, int],
    parent_identity: DirectoryIdentity,
) -> None:
    deadline = time.monotonic() + _MANAGER_CHILD_BIND_TIMEOUT_SECONDS
    retry_error: PermissionError | None = None
    while True:
        if retry_error is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise retry_error
            time.sleep(min(_MANAGER_CHILD_BIND_POLL_SECONDS, remaining))
            if time.monotonic() >= deadline:
                raise retry_error
        require_directory_identity(
            parent_identity,
            label="manager child authority marker parent",
        )
        current, current_identity = _read_stable_regular_file_snapshot(
            destination,
            label="manager child authority marker",
        )
        if current != expected_payload or current_identity != expected_identity:
            raise RuntimeError(
                "manager child authority marker changed during bind retry"
            )
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            if os.name != "nt":
                raise
            retry_error = error


def _parse_bound_manager_marker(
    marker: str,
    persisted: bytes,
) -> ProcessIdentity | None:
    unbound = marker.encode("ascii")
    if persisted == unbound:
        return None
    try:
        payload = json.loads(persisted.decode("ascii"))
        unbound_payload = json.loads(marker)
        if not isinstance(payload, dict) or not isinstance(unbound_payload, dict):
            raise TypeError("marker must be an object")
        if set(payload) != {*unbound_payload, "child_identity"}:
            raise ValueError("bound marker fields differ")
        raw_identity = payload.pop("child_identity")
        if payload != unbound_payload or not isinstance(raw_identity, dict):
            raise ValueError("bound marker envelope differs")
        if set(raw_identity) != {"pid", "created_at"}:
            raise ValueError("bound marker identity fields differ")
        identity = ProcessIdentity(
            int(raw_identity["pid"]),
            float(raw_identity["created_at"]),
        )
        if (
            identity.pid <= 0
            or not math.isfinite(identity.created_at)
            or identity.created_at <= 0
            or persisted != _bound_manager_marker(marker, identity)
        ):
            raise ValueError("bound marker identity is invalid")
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise RuntimeError("manager child authority marker differs") from error
    return identity


def _wait_for_manager_marker_binding(
    marker_path: Path,
    marker: str,
) -> ProcessIdentity:
    deadline = time.monotonic() + _MANAGER_CHILD_BIND_TIMEOUT_SECONDS
    while True:
        persisted = _read_stable_regular_file(
            marker_path,
            label="manager child authority marker",
        )
        identity = _parse_bound_manager_marker(marker, persisted)
        if identity is not None:
            return identity
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("manager child authority binding timed out")
        time.sleep(min(_MANAGER_CHILD_BIND_POLL_SECONDS, remaining))


def bind_manager_child_authority(
    authority: Mapping[str, str],
    process_handle: Any,
    *,
    process_factory: Callable[[int], Any] = psutil.Process,
    lock_factory: Callable[[Path], Any] = SingleInstanceLock,
) -> ProcessIdentity:
    """Bind one published marker to the exact process returned by Popen."""

    state = _manager_marker_state(authority)
    marker = authority.get(_MANAGER_CHILD_AUTHORITY_ENV)
    if marker != state.marker:
        raise RuntimeError("manager child authority marker differs")
    with state.guard:
        if state.closed:
            raise RuntimeError("manager child authority marker is closed")
        if state.publisher_pid != os.getpid():
            raise RuntimeError("manager child authority publisher differs")
        if state.bound_identity is not None:
            raise RuntimeError("manager child authority is already bound")
        (
            root,
            marker_path,
            _,
            expected_command,
            _,
            database_identity,
            lock_owners,
        ) = _parse_manager_marker(state.database, marker)
        if marker_path != state.marker_path:
            raise RuntimeError("manager child authority marker path differs")
        persisted, original_file_identity = _read_stable_regular_file_snapshot(
            marker_path,
            label="manager child authority marker",
        )
        if persisted != state.unbound_bytes:
            raise RuntimeError("manager child authority is already bound or changed")
        for owner in lock_owners:
            _require_lock_held_by(
                owner.lock_path,
                root,
                lock_factory,
                process_factory,
                database_identity=database_identity,
                expected_owner=owner,
            )
        try:
            root_process = (
                psutil.Process(root.pid)
                if root.pid == os.getpid()
                else process_factory(root.pid)
            )
            root_identity = ProcessIdentity(
                int(root_process.pid),
                float(root_process.create_time()),
            )
            child_pid = int(process_handle.pid)
            child = process_factory(child_pid)
            child_identity = ProcessIdentity(
                int(child.pid),
                float(child.create_time()),
            )
            child_command = [str(item) for item in child.cmdline()]
        except (
            psutil.NoSuchProcess,
            AttributeError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            psutil.Error,
        ) as error:
            raise RuntimeError("manager child authority process is unverifiable") from error
        if root_identity != root:
            raise RuntimeError("manager child authority root identity changed")
        if child_identity.pid != child_pid or child_identity.pid <= 0:
            raise RuntimeError("manager child authority child identity changed")
        if (
            not math.isfinite(child_identity.created_at)
            or child_identity.created_at <= 0
            or not _commands_match(child_command, expected_command)
        ):
            raise RuntimeError("manager child authority Popen command differs")
        poll = getattr(process_handle, "poll", None)
        if callable(poll) and poll() is not None:
            raise RuntimeError("manager child authority child exited before binding")

        bound = _bound_manager_marker(marker, child_identity)
        temporary = marker_path.with_name(
            f".{marker_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        try:
            try:
                with temporary.open("xb") as handle:
                    os.chmod(temporary, 0o600)
                    handle.write(bound)
                    handle.flush()
                    os.fsync(handle.fileno())
                current, current_file_identity = _read_stable_regular_file_snapshot(
                    marker_path,
                    label="manager child authority marker",
                )
                if (
                    current != state.unbound_bytes
                    or current_file_identity != original_file_identity
                ):
                    raise RuntimeError(
                        "manager child authority marker changed before binding"
                    )
                _replace_polled_manager_marker(
                    temporary,
                    marker_path,
                    expected_payload=state.unbound_bytes,
                    expected_identity=original_file_identity,
                    parent_identity=capture_directory_identity(
                        marker_path.parent,
                        label="manager child authority marker parent",
                    ),
                )
                fsync_directory(marker_path.parent)
                bound_snapshot = read_stable_regular_file(
                    marker_path,
                    label="manager child authority marker",
                )
                if bound_snapshot.payload != bound:
                    raise RuntimeError("manager child authority binding differs")
                state.bound_bytes = bound
                state.bound_identity = child_identity
                state.marker_file_identity = (
                    bound_snapshot.device,
                    bound_snapshot.inode,
                )
            except BaseException:
                try:
                    recovered_snapshot = read_stable_regular_file(
                        marker_path,
                        label="manager child authority marker",
                    )
                except Exception:
                    recovered_snapshot = None
                if (
                    recovered_snapshot is not None
                    and recovered_snapshot.payload == bound
                ):
                    state.bound_bytes = bound
                    state.bound_identity = child_identity
                    state.marker_file_identity = (
                        recovered_snapshot.device,
                        recovered_snapshot.inode,
                    )
                raise
        finally:
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
                fsync_directory(temporary.parent)
        require_unique_database_file(
            state.database,
            expected_identity=database_identity,
        )
        return child_identity


def _validate_manager_child_authority(
    database: Path,
    marker: str,
    *,
    process_factory: Callable[[int], Any],
    parent_pid: int,
    current_pid: int,
    lock_factory: Callable[[Path], Any],
) -> tuple[
    ProcessIdentity,
    str,
    tuple[str, ...],
    DatabaseFileIdentity,
    tuple[LockOwnerMetadata, ...],
]:
    (
        root,
        marker_path,
        role,
        expected_command,
        delegates,
        database_identity,
        lock_owners,
    ) = (
        _parse_manager_marker(database, marker)
    )
    _resolve_stable_authority_ancestor(
        root,
        parent_pid=parent_pid,
        process_factory=process_factory,
        label="manager child authority",
    )
    for owner in lock_owners:
        _require_lock_held_by(
            owner.lock_path,
            root,
            lock_factory,
            process_factory,
            database_identity=database_identity,
            expected_owner=owner,
        )
    bound_identity = _wait_for_manager_marker_binding(marker_path, marker)
    try:
        current = process_factory(current_pid)
        current_identity = ProcessIdentity(
            int(current.pid),
            float(current.create_time()),
        )
        current_command = [str(item) for item in current.cmdline()]
    except (
        psutil.NoSuchProcess,
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        psutil.Error,
    ) as error:
        raise RuntimeError("manager child authority child is unverifiable") from error
    if current_identity.pid != current_pid or current_identity != bound_identity:
        raise RuntimeError("manager child authority child identity changed")
    if not _commands_match(current_command, expected_command):
        raise RuntimeError("manager child authority command differs")
    return root, role, delegates, database_identity, lock_owners


@contextmanager
def manager_child_authority(
    database: Path,
    *,
    role: str,
    command: list[str],
    delegate_roles: Iterable[str] = (),
    held_locks: Iterable[Path] | None = None,
    environ: Mapping[str, str] | None = None,
    process_factory: Callable[[int], Any] = psutil.Process,
    lock_factory: Callable[[Path], Any] = SingleInstanceLock,
) -> Iterator[dict[str, str]]:
    """Authorize one exact descendant from a proven canonical lock set."""

    resolved = database.resolve()
    identity = require_unique_database_file(resolved)
    assert identity is not None
    source_environment = os.environ if environ is None else environ
    requested_role = _manager_role(role)
    requested_command = _manager_command(command)
    if _command_database(requested_command) != resolved:
        raise RuntimeError("manager child authority command database differs")
    delegates = tuple(sorted({_manager_role(item) for item in delegate_roles}))
    if set(delegates) - set(_MANAGER_DELEGATIONS.get(requested_role, ())):
        raise RuntimeError("manager child authority delegation differs")

    upstream = source_environment.get(_MANAGER_CHILD_AUTHORITY_ENV)
    if upstream is None:
        process = process_factory(os.getpid())
        root = ProcessIdentity(int(process.pid), float(process.create_time()))
        if root.pid != os.getpid():
            raise RuntimeError("manager child authority root identity changed")
        requested_locks = (
            database_authority_lock_paths(resolved)
            if held_locks is None
            else tuple(Path(path).resolve() for path in held_locks)
        )
        supported_lock_sets = {
            database_service_authority_lock_paths(resolved),
            database_authority_lock_paths(resolved),
        }
        if requested_locks not in supported_lock_sets:
            raise RuntimeError("manager child authority root locks differ")
        lock_owners = tuple(
            _bind_current_lock_owner(lock_path, root, identity)
            for lock_path in requested_locks
        )
        for owner in lock_owners:
            _require_lock_held_by(
                owner.lock_path,
                root,
                lock_factory,
                process_factory,
                database_identity=identity,
                expected_owner=owner,
            )
    else:
        (
            root,
            upstream_role,
            allowed_delegates,
            upstream_identity,
            lock_owners,
        ) = _validate_manager_child_authority(
                resolved,
                upstream,
                process_factory=process_factory,
                parent_pid=os.getppid(),
                current_pid=os.getpid(),
                lock_factory=lock_factory,
            )
        if upstream_identity != identity:
            raise RuntimeError("manager child authority database file identity differs")
        if held_locks is not None and tuple(
            Path(path).resolve() for path in held_locks
        ) != tuple(owner.lock_path for owner in lock_owners):
            raise RuntimeError("manager child authority root locks differ")
        if requested_role not in allowed_delegates or requested_role not in (
            _MANAGER_DELEGATIONS.get(upstream_role, ())
        ):
            raise RuntimeError("manager child authority role is not delegated")

    token = secrets.token_hex(32)
    marker_path = _manager_marker_path(resolved, token)
    marker = _manager_marker(
        resolved,
        identity,
        root,
        token,
        marker_path,
        requested_role,
        requested_command,
        delegates,
        lock_owners,
    )
    state = _ManagerMarkerState(resolved, marker_path, marker)
    authority = _ManagerChildAuthority(marker, state)
    temporary = marker_path.with_name(
        f".{marker_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    temporary_snapshot: StableRegularFile | None = None
    temporary_file_identity: tuple[int, int] | None = None
    try:
        if marker_path.exists() or marker_path.is_symlink():
            raise RuntimeError("manager child authority marker already exists")
        with temporary.open("xb") as handle:
            created = os.fstat(handle.fileno())
            temporary_file_identity = (int(created.st_dev), int(created.st_ino))
            os.chmod(temporary, 0o600)
            handle.write(state.unbound_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_snapshot = read_stable_regular_file(
            temporary,
            label="manager child authority temporary marker",
        )
        os.replace(temporary, marker_path)
        fsync_directory(marker_path.parent)
        published_snapshot = read_stable_regular_file(
            marker_path,
            label="manager child authority marker",
        )
        if (
            published_snapshot.payload != state.unbound_bytes
            or published_snapshot.device != temporary_snapshot.device
            or published_snapshot.inode != temporary_snapshot.inode
            or published_snapshot.sha256 != temporary_snapshot.sha256
        ):
            raise RuntimeError("manager child authority publication differs")
        state.marker_file_identity = (
            published_snapshot.device,
            published_snapshot.inode,
        )
        yield authority
    finally:
        if temporary.exists() or temporary.is_symlink():
            current_temporary = read_stable_regular_file(
                temporary,
                label="manager child authority temporary marker",
            )
            if (
                temporary_file_identity is None
                or (current_temporary.device, current_temporary.inode)
                != temporary_file_identity
                or (
                    temporary_snapshot is not None
                    and current_temporary != temporary_snapshot
                )
            ):
                raise RuntimeError("manager child authority temporary marker changed")
            temporary.unlink()
            fsync_directory(temporary.parent)
        if marker_path.exists() or marker_path.is_symlink():
            with state.guard:
                expected = state.bound_bytes or state.unbound_bytes
                current_marker = read_stable_regular_file(
                    marker_path,
                    label="manager child authority marker",
                )
                if (
                    state.marker_file_identity is None
                    and temporary_snapshot is not None
                    and current_marker.payload == state.unbound_bytes
                    and current_marker.device == temporary_snapshot.device
                    and current_marker.inode == temporary_snapshot.inode
                    and current_marker.sha256 == temporary_snapshot.sha256
                ):
                    state.marker_file_identity = (
                        current_marker.device,
                        current_marker.inode,
                    )
                if (
                    current_marker.payload != expected
                    or state.marker_file_identity is None
                    or (
                        current_marker.device,
                        current_marker.inode,
                    )
                    != state.marker_file_identity
                ):
                    raise RuntimeError("manager child authority marker changed")
                for owner in lock_owners:
                    _require_lock_held_by(
                        owner.lock_path,
                        root,
                        lock_factory,
                        process_factory,
                        database_identity=identity,
                        expected_owner=owner,
                    )
                marker_path.unlink()
                fsync_directory(marker_path.parent)
                state.closed = True
        else:
            state.closed = True
        require_unique_database_file(resolved, expected_identity=identity)


def manager_child_process_environment(
    authority: Mapping[str, str],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an explicit environment containing only manager child authority."""

    if set(authority) != {_MANAGER_CHILD_AUTHORITY_ENV}:
        raise RuntimeError("manager child authority environment is invalid")
    child = dict(os.environ if environ is None else environ)
    child.pop(_SUPERVISOR_AUTHORITY_ENV, None)
    child.pop(_WEB_FETCH_AUTHORITY_ENV, None)
    child.pop(_MANAGER_CHILD_AUTHORITY_ENV, None)
    child.update(authority)
    if isinstance(authority, _ManagerChildAuthority):
        return _ManagerChildEnvironment(child, authority.state)
    return child


@contextmanager
def delegated_writer_process_environment(
    database: Path,
    *,
    role: str,
    command: list[str],
    environ: Mapping[str, str] | None = None,
) -> Iterator[dict[str, str]]:
    """Build exact delegated child authority from a generic manager marker."""

    source = os.environ if environ is None else environ
    with manager_child_authority(
        database,
        role=role,
        command=command,
        held_locks=(
            None
            if _MANAGER_CHILD_AUTHORITY_ENV in source
            else database_service_authority_lock_paths(database)
        ),
        environ=source,
    ) as authority:
        yield manager_child_process_environment(authority, environ=source)


@contextmanager
def web_fetch_child_authority(
    database: Path,
    *,
    process_factory: Callable[[int], Any] = psutil.Process,
    lock_factory: Callable[[Path], Any] = SingleInstanceLock,
    environ: Mapping[str, str] | None = None,
) -> Iterator[dict[str, str]]:
    """Publish one request-scoped marker from the actual Web lock owner."""

    resolved = database.resolve()
    identity = require_unique_database_file(resolved)
    assert identity is not None
    process = process_factory(os.getpid())
    root_identity = ProcessIdentity(
        int(process.pid),
        float(process.create_time()),
    )
    if root_identity.pid != os.getpid():
        raise RuntimeError("Web fetch authority root identity changed")
    try:
        root_command = [str(item) for item in process.cmdline()]
        root_environment = (
            dict(os.environ if environ is None else environ)
            if root_identity.pid == os.getpid()
            else dict(process.environ())
        )
    except (
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        psutil.Error,
    ) as error:
        raise RuntimeError("Web fetch authority root is unverifiable") from error
    if not _web_root_command(root_command):
        raise RuntimeError("Web fetch authority root command differs")
    try:
        root_database = _command_or_environment_database(
            root_command,
            root_environment,
        )
    except ValueError as error:
        raise RuntimeError("Web fetch authority root database is unverifiable") from error
    if root_database != resolved:
        raise RuntimeError("Web fetch authority root database differs")

    global_lock, web_lock = database_web_authority_lock_paths(resolved)
    global_owner = _bind_current_lock_owner(global_lock, root_identity, identity)
    web_owner = _bind_current_lock_owner(web_lock, root_identity, identity)
    for lock_path, owner in (
        (global_lock, global_owner),
        (web_lock, web_owner),
    ):
        _require_lock_held_by(
            lock_path,
            root_identity,
            lock_factory,
            process_factory,
            database_identity=identity,
            expected_owner=owner,
        )
    token = secrets.token_hex(32)
    marker_path = _web_fetch_authority_path(resolved, token)
    marker = _web_fetch_marker(
        resolved,
        identity,
        root_identity,
        token,
        marker_path,
        root_command,
        global_owner,
        web_owner,
    )
    temporary = marker_path.with_name(
        f".{marker_path.name}.{os.getpid()}.tmp"
    )
    encoded = marker.encode("ascii")
    published = False
    operation_error: BaseException | None = None
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker_path)
        fsync_directory(marker_path.parent)
        published = True
        yield {_WEB_FETCH_AUTHORITY_ENV: marker}
    except BaseException as error:
        operation_error = error
        raise
    finally:
        cleanup_errors: list[tuple[str, BaseException]] = []
        try:
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
                fsync_directory(temporary.parent)
        except BaseException as cleanup_error:
            cleanup_errors.append(
                ("Web fetch temporary cleanup failed", cleanup_error)
            )
        if published:
            marker_safe = True
            try:
                persisted = _read_stable_regular_file(
                    marker_path,
                    label="Web fetch authority marker",
                )
                if persisted != encoded:
                    raise RuntimeError("Web fetch authority marker changed")
            except BaseException as cleanup_error:
                marker_safe = False
                cleanup_errors.append(
                    ("Web fetch marker verification failed", cleanup_error)
                )
            for lock_path, owner in (
                (global_lock, global_owner),
                (web_lock, web_owner),
            ):
                try:
                    _require_lock_held_by(
                        lock_path,
                        root_identity,
                        lock_factory,
                        process_factory,
                        database_identity=identity,
                        expected_owner=owner,
                    )
                except BaseException as cleanup_error:
                    marker_safe = False
                    cleanup_errors.append(
                        ("Web fetch lock verification failed", cleanup_error)
                    )
            if marker_safe:
                try:
                    marker_path.unlink()
                    fsync_directory(marker_path.parent)
                except BaseException as cleanup_error:
                    cleanup_errors.append(
                        ("Web fetch marker cleanup failed", cleanup_error)
                    )
        try:
            require_unique_database_file(resolved, expected_identity=identity)
        except BaseException as cleanup_error:
            cleanup_errors.append(
                ("Web fetch database verification failed", cleanup_error)
            )
        if cleanup_errors:
            if operation_error is not None:
                for label, cleanup_error in cleanup_errors:
                    _note_cleanup_error(
                        operation_error,
                        cleanup_error,
                        label=label,
                    )
            else:
                label, cleanup_error = cleanup_errors[0]
                cleanup_error.add_note(label)
                for secondary_label, secondary_error in cleanup_errors[1:]:
                    _note_cleanup_error(
                        cleanup_error,
                        secondary_error,
                        label=secondary_label,
                    )
                raise cleanup_error


def _validate_web_fetch_child_authority(
    database: Path,
    marker: str,
    *,
    process_factory: Callable[[int], Any],
    parent_pid: int,
    current_pid: int,
    lock_factory: Callable[[Path], Any],
) -> ProcessIdentity:
    try:
        payload = json.loads(marker)
        if not isinstance(payload, dict):
            raise TypeError("marker must be an object")
        if int(payload["version"]) != _WEB_FETCH_AUTHORITY_VERSION:
            raise ValueError("marker version differs")
        root_pid = int(payload["root_pid"])
        root_created_at = float(payload["root_created_at"])
        if set(payload) != {
            "version",
            "root_pid",
            "root_created_at",
            "database",
            "database_identity",
            "token",
            "marker_path",
            "role",
            "root_command",
            "global_lock_owner",
            "web_lock_owner",
        }:
            raise ValueError("marker fields differ")
        marker_database = Path(str(payload["database"])).resolve()
        database_identity = _parse_database_identity(payload["database_identity"])
        token = str(payload["token"])
        marker_path = Path(str(payload["marker_path"])).resolve()
        role = str(payload["role"])
        expected_root_command = _manager_command(payload["root_command"])
        global_lock_owner = _parse_lock_owner(payload["global_lock_owner"])
        web_lock_owner = _parse_lock_owner(payload["web_lock_owner"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("Web fetch authority marker is invalid") from error
    if marker_database != database.resolve():
        raise RuntimeError("Web fetch authority database differs")
    current_database_identity = require_unique_database_file(database)
    if current_database_identity != database_identity:
        raise RuntimeError("Web fetch authority database file identity differs")
    if role != _WEB_FETCH_AUTHORITY_ROLE:
        raise RuntimeError("Web fetch authority role differs")
    if len(token) != 64 or any(
        character not in "0123456789abcdef" for character in token
    ):
        raise RuntimeError("Web fetch authority token is invalid")
    if marker_path != _web_fetch_authority_path(database, token):
        raise RuntimeError("Web fetch authority marker path differs")

    expected_identity = ProcessIdentity(root_pid, root_created_at)
    root = _resolve_stable_authority_ancestor(
        expected_identity,
        parent_pid=parent_pid,
        process_factory=process_factory,
        label="Web fetch authority",
    )
    try:
        root_command = [str(item) for item in root.cmdline()]
        root_environment = root.environ()
    except (
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        psutil.Error,
    ) as error:
        raise RuntimeError("Web fetch authority root is unverifiable") from error
    if (
        not isinstance(root_environment, Mapping)
        or not _commands_match(root_command, expected_root_command)
        or not _web_root_command(root_command)
    ):
        raise RuntimeError("Web fetch authority root command differs")
    try:
        root_database = _command_or_environment_database(
            root_command,
            root_environment,
        )
    except ValueError as error:
        raise RuntimeError("Web fetch authority root database is unverifiable") from error
    if root_database != database.resolve():
        raise RuntimeError("Web fetch authority root database differs")
    persisted = _read_stable_regular_file(
        marker_path,
        label="Web fetch authority marker",
    )
    if persisted != marker.encode("ascii"):
        raise RuntimeError("Web fetch authority token differs")
    for lock_path, owner in zip(
        database_web_authority_lock_paths(database),
        (global_lock_owner, web_lock_owner),
        strict=True,
    ):
        _require_lock_held_by(
            lock_path,
            expected_identity,
            lock_factory,
            process_factory,
            database_identity=database_identity,
            expected_owner=owner,
        )
    try:
        current = process_factory(current_pid)
        current_identity = ProcessIdentity(
            int(current.pid),
            float(current.create_time()),
        )
        command = [str(item) for item in current.cmdline()]
    except (
        psutil.NoSuchProcess,
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        psutil.Error,
    ) as error:
        raise RuntimeError("Web fetch authority child is unverifiable") from error
    if current_identity.pid != current_pid:
        raise RuntimeError("Web fetch authority child identity changed")
    is_fetch = any(
        argument == "-m" and command[index + 1] == "fetch.main"
        for index, argument in enumerate(command[:-1])
    )
    if not is_fetch or _command_database(command) != database.resolve():
        raise RuntimeError("Web fetch authority child command differs")
    return expected_identity


def web_fetch_process_environment(
    authority: Mapping[str, str],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the explicit fetch environment without broader writer authority."""

    if set(authority) != {_WEB_FETCH_AUTHORITY_ENV}:
        raise RuntimeError("Web fetch authority environment is invalid")
    child = dict(os.environ if environ is None else environ)
    child.pop(_SUPERVISOR_AUTHORITY_ENV, None)
    child.pop(_WEB_FETCH_AUTHORITY_ENV, None)
    child.pop(_MANAGER_CHILD_AUTHORITY_ENV, None)
    child.update(authority)
    return child


@contextmanager
def database_writer_authority(
    database: Path,
    *,
    require_manager_child: bool = False,
    environ: Mapping[str, str] | None = None,
    process_factory: Callable[[int], Any] = psutil.Process,
    parent_pid: int | None = None,
    current_pid: int | None = None,
    lock_factory: Callable[[Path], Any] = SingleInstanceLock,
    writer_scanner: Callable[[Path], WriterScanResult] | None = None,
) -> Iterator[None]:
    """Hold standalone authority or prove the caller is a managed child."""

    resolved = database.resolve()
    root_identity = capture_directory_identity(
        resolved.parent,
        label="database root",
    )
    initial_identity = require_unique_database_file(resolved, allow_missing=True)
    source_environment = os.environ if environ is None else environ
    marker = source_environment.get(_SUPERVISOR_AUTHORITY_ENV)
    if marker is not None:
        raise RuntimeError("legacy supervisor child authority is unsupported")

    manager_marker = source_environment.get(_MANAGER_CHILD_AUTHORITY_ENV)
    if require_manager_child and manager_marker is None:
        raise RuntimeError("managed child authority is required")
    if manager_marker is not None:
        _validate_manager_child_authority(
            resolved,
            manager_marker,
            process_factory=process_factory,
            parent_pid=os.getppid() if parent_pid is None else parent_pid,
            current_pid=os.getpid() if current_pid is None else current_pid,
            lock_factory=lock_factory,
        )
        require_unique_database_file(
            resolved,
            expected_identity=initial_identity,
            allow_missing=initial_identity is None,
        )
        require_directory_identity(root_identity, label="database root")
        try:
            yield
        finally:
            require_directory_identity(root_identity, label="database root")
            _validate_manager_child_authority(
                resolved,
                manager_marker,
                process_factory=process_factory,
                parent_pid=os.getppid() if parent_pid is None else parent_pid,
                current_pid=os.getpid() if current_pid is None else current_pid,
                lock_factory=lock_factory,
            )
            if initial_identity is None:
                require_unique_database_file(resolved)
            else:
                require_unique_database_file(
                    resolved,
                    expected_identity=initial_identity,
                )
        return

    web_marker = source_environment.get(_WEB_FETCH_AUTHORITY_ENV)
    if web_marker is not None:
        _validate_web_fetch_child_authority(
            resolved,
            web_marker,
            process_factory=process_factory,
            parent_pid=os.getppid() if parent_pid is None else parent_pid,
            current_pid=os.getpid() if current_pid is None else current_pid,
            lock_factory=lock_factory,
        )
        require_directory_identity(root_identity, label="database root")
        lock_paths = database_service_authority_lock_paths(resolved)
    else:
        lock_paths = database_service_authority_lock_paths(resolved)

    with ExitStack() as locks:
        for lock_path in lock_paths:
            locks.enter_context(lock_factory(lock_path))
        locked_identity = require_unique_database_file(
            resolved,
            expected_identity=initial_identity,
            allow_missing=initial_identity is None,
        )
        scan = (
            writer_scanner(resolved)
            if writer_scanner is not None
            else scan_managed_writers(resolved, mode="normal")
        )
        if scan.unverifiable_pids:
            raise RuntimeError(
                "managed writer scan could not verify PIDs: "
                + ",".join(str(pid) for pid in scan.unverifiable_pids)
            )
        if scan.conflicts:
            raise RuntimeError(
                "managed writers already target this database: "
                + ",".join(str(item.pid) for item in scan.conflicts)
            )
        require_unique_database_file(
            resolved,
            expected_identity=locked_identity,
            allow_missing=locked_identity is None,
        )
        require_directory_identity(root_identity, label="database root")
        try:
            yield
        finally:
            require_directory_identity(root_identity, label="database root")
            if locked_identity is None:
                require_unique_database_file(resolved)
            else:
                require_unique_database_file(
                    resolved,
                    expected_identity=locked_identity,
                )


@contextmanager
def database_offline_authority(
    database: Path,
    *,
    allow_missing: bool = False,
    allow_replacement: bool = False,
    replacement_identity_getter: (
        Callable[[], DatabaseFileIdentity | None] | None
    ) = None,
    expected_root_identity: DirectoryIdentity | None = None,
    lock_factory: Callable[[Path], Any] = SingleInstanceLock,
    writer_scanner: Callable[[Path], WriterScanResult] | None = None,
) -> Iterator[None]:
    """Exclude Web and every writer for one complete offline operation."""

    if replacement_identity_getter is not None and not allow_replacement:
        raise ValueError(
            "replacement identity getter requires allow_replacement=True"
        )

    resolved = database.resolve()
    root_identity = (
        capture_directory_identity(resolved.parent, label="database root")
        if expected_root_identity is None
        else require_directory_identity(
            expected_root_identity,
            label="database root",
        )
    )
    if root_identity.path != Path(os.path.abspath(os.fspath(resolved.parent))):
        raise RuntimeError("database root authority path differs")
    initial_identity = require_unique_database_file(
        resolved,
        allow_missing=allow_missing,
    )
    with ExitStack() as locks:
        for lock_path in database_authority_lock_paths(resolved):
            locks.enter_context(lock_factory(lock_path))
        locked_identity = require_unique_database_file(
            resolved,
            expected_identity=initial_identity,
            allow_missing=initial_identity is None,
        )
        scan = (
            writer_scanner(resolved)
            if writer_scanner is not None
            else scan_managed_writers(resolved, mode="offline")
        )
        if scan.unverifiable_pids:
            raise RuntimeError(
                "managed writer scan could not verify PIDs: "
                + ",".join(str(pid) for pid in scan.unverifiable_pids)
            )
        if scan.conflicts:
            raise RuntimeError(
                "managed writers already target this database: "
                + ",".join(str(item.pid) for item in scan.conflicts)
            )
        require_unique_database_file(
            resolved,
            expected_identity=locked_identity,
            allow_missing=locked_identity is None,
        )
        require_directory_identity(root_identity, label="database root")
        completed_normally = False
        try:
            yield
            completed_normally = True
        finally:
            require_directory_identity(root_identity, label="database root")
            completed = (
                writer_scanner(resolved)
                if writer_scanner is not None
                else scan_managed_writers(resolved, mode="offline")
            )
            if not completed.safe:
                raise RuntimeError(
                    "database writer authority changed during offline operation"
                )
            expected_replacement = (
                replacement_identity_getter()
                if replacement_identity_getter is not None
                else None
            )
            if expected_replacement is not None:
                require_unique_database_file(
                    resolved,
                    expected_identity=expected_replacement,
                )
            elif completed_normally and replacement_identity_getter is not None:
                raise RuntimeError(
                    "offline database replacement identity was not bound"
                )
            elif locked_identity is None:
                require_unique_database_file(
                    resolved,
                    allow_missing=allow_missing and not completed_normally,
                )
            elif allow_replacement:
                require_unique_database_file(resolved)
            else:
                require_unique_database_file(
                    resolved,
                    expected_identity=locked_identity,
                )
            require_directory_identity(root_identity, label="database root")


def _command_database(command: list[str]) -> Path:
    candidates: list[str] = []
    for index, argument in enumerate(command):
        if argument == _MANAGED_CHILD_TARGET_SENTINEL:
            break
        if argument == "--database":
            if index + 1 >= len(command):
                raise ValueError("writer database argument is missing its value")
            candidates.append(command[index + 1])
        if argument.startswith("--database="):
            candidates.append(argument.split("=", 1)[1])
    if len(candidates) != 1 or not candidates[0]:
        raise ValueError("writer command must contain exactly one database argument")
    candidate = Path(candidates[0])
    if not candidate.is_absolute():
        raise ValueError("writer database path is relative")
    return candidate.resolve()


def managed_child_command(command: list[str]) -> list[str]:
    """Wrap a target command so authority validation precedes target import."""

    target = _manager_command(command)
    try:
        kind, entrypoint, _, _ = _managed_child_entrypoint(target)
    except ValueError:
        kind = ""
        entrypoint = ""
    is_wrapper = (
        kind == "path"
        and Path(entrypoint).resolve() == MANAGED_CHILD_BOOTSTRAP_SCRIPT
    )
    if is_wrapper:
        if managed_child_target(target) is None:
            raise ValueError("managed child wrapper is invalid")
        return target
    if _MANAGED_CHILD_TARGET_SENTINEL in target:
        raise ValueError("managed child target argv contains a reserved argument")
    kind, entrypoint, arguments, python_flags = _managed_child_entrypoint(target)
    if kind == "path":
        target = [
            target[0],
            *python_flags,
            str(Path(entrypoint).resolve()),
            *arguments,
        ]
    database = _command_database(target)
    return [
        target[0],
        *python_flags,
        str(MANAGED_CHILD_BOOTSTRAP_SCRIPT),
        "--database",
        str(database),
        _MANAGED_CHILD_TARGET_SENTINEL,
        *target,
    ]


def _managed_child_entrypoint(
    command: list[str],
) -> tuple[str, str, list[str], list[str]]:
    target = _manager_command(command)
    if not _python_process_name(target[0]):
        raise ValueError("managed child Python interpreter is unsupported")
    index = 1
    python_flags: list[str] = []
    while index < len(target):
        flag = target[index]
        if flag in _MANAGED_CHILD_REJECTED_PYTHON_FLAGS:
            raise ValueError(f"managed child Python flag {flag} is unsupported")
        if flag not in _MANAGED_CHILD_PYTHON_FLAGS:
            break
        python_flags.append(flag)
        index += 1
    if index >= len(target):
        raise ValueError("managed child target entrypoint is missing")
    if target[index] == "-c":
        raise ValueError("managed child inline code is unsupported")
    if target[index] == "-m":
        if index + 1 >= len(target) or not target[index + 1]:
            raise ValueError("managed child target module is missing")
        return "module", target[index + 1], target[index + 2 :], python_flags
    script = target[index]
    if Path(script).suffix.lower() not in {".py", ".pyw"}:
        raise ValueError("managed child target script is unsupported")
    return "path", script, target[index + 1 :], python_flags


def managed_child_entrypoint(
    command: list[str],
) -> tuple[str, str, list[str]]:
    kind, entrypoint, arguments, _ = _managed_child_entrypoint(command)
    return kind, entrypoint, arguments


def managed_child_target(command: list[str]) -> list[str] | None:
    try:
        kind, entrypoint, arguments, _ = _managed_child_entrypoint(command)
    except ValueError:
        return None
    if (
        kind != "path"
        or not Path(entrypoint).is_absolute()
        or Path(entrypoint).resolve() != MANAGED_CHILD_BOOTSTRAP_SCRIPT
    ):
        return None
    if (
        len(arguments) < 4
        or arguments[0] != "--database"
        or arguments[2] != _MANAGED_CHILD_TARGET_SENTINEL
    ):
        return None
    target = arguments[3:]
    try:
        target_kind, target_entrypoint, _, _ = _managed_child_entrypoint(target)
        outer_database = Path(arguments[1])
        if (
            not outer_database.is_absolute()
            or _command_database(target) != outer_database.resolve()
            or (
                target_kind == "path"
                and not Path(target_entrypoint).is_absolute()
            )
        ):
            return None
    except (OSError, ValueError):
        return None
    return target


def _is_managed_writer_command(command: list[str]) -> bool:
    target = managed_child_target(command)
    if target is not None:
        return _is_managed_writer_command(target)
    for index, argument in enumerate(command[:-1]):
        if argument == "-m" and command[index + 1] in _WRITER_MODULES:
            return True
    return any(Path(argument).name in _WRITER_SCRIPTS for argument in command)


def _is_web_process_command(command: list[str]) -> bool:
    return _web_root_command(command)


def _process_environment(process: Any, info: Mapping[str, object]) -> Mapping[str, str] | None:
    value = info.get("environ")
    if isinstance(value, Mapping):
        return {str(key): str(item) for key, item in value.items()}
    try:
        value = process.environ()
    except (AttributeError, OSError, psutil.Error):
        return None
    if not isinstance(value, Mapping):
        return None
    return {str(key): str(item) for key, item in value.items()}


def _offline_process_database(
    command: list[str],
    environment: Mapping[str, str] | None,
    *,
    web_process: bool,
) -> Path:
    command_has_database = any(
        argument == "--database" or argument.startswith("--database=")
        for argument in command
    )
    if command_has_database:
        return _command_database(command)
    if web_process and environment is not None:
        configured = environment.get("DATABASE_PATH")
        if configured:
            candidate = Path(configured)
            if not candidate.is_absolute():
                raise ValueError("process database path is relative")
            return candidate.resolve()
    raise ValueError("process database authority is unavailable")


def _identity_is_allowed(
    identity: ProcessIdentity,
    allowed: Iterable[ProcessIdentity],
) -> bool:
    return any(
        identity.pid == item.pid
        and identity.created_at == item.created_at
        for item in allowed
    )


def _python_process_name(value: object) -> bool:
    name = Path(str(value or "")).name.casefold()
    return name in _PYTHON_PROCESS_NAMES or Path(name).stem.startswith("python")


def _fallback_process_value(process: Any, name: str) -> object | None:
    try:
        value = getattr(process, name)
        return value() if callable(value) else value
    except (AttributeError, OSError, psutil.Error):
        return None


def _obvious_non_python_system_process(process: Any, pid: int) -> bool:
    if os.name != "nt":
        return False
    if pid in {0, 4}:
        return True
    raw_parent = _fallback_process_value(process, "ppid")
    try:
        return int(raw_parent) in {0, 4}
    except (TypeError, ValueError):
        return False


def scan_managed_writers(
    database: Path,
    *,
    allowed_identities: Iterable[ProcessIdentity] = (),
    process_iter: Callable[..., Iterable[Any]] = _DEFAULT_PROCESS_ITER,
    process_factory: Callable[[int], Any] | None = None,
    mode: str = "normal",
    revalidation_passes: int = 2,
) -> WriterScanResult:
    """Find database peers; offline mode also fences Web and unknown Python."""

    if mode not in {"normal", "offline"}:
        raise ValueError("writer scan mode must be normal or offline")
    if revalidation_passes < 0:
        raise ValueError("writer scan revalidation passes must be nonnegative")

    expected_identity = require_unique_database_file(database, allow_missing=True)
    expected_database = (
        expected_identity.resolved_path
        if expected_identity is not None
        else database.resolve()
    )
    allowed = tuple(allowed_identities)
    conflicts: list[ProcessIdentity] = []
    unverifiable: set[int] = set()
    try:
        attributes = ["pid", "name", "cmdline", "create_time"]
        if mode == "offline":
            attributes.append("environ")
        processes = process_iter(tuple(attributes))
        for process in processes:
            info: dict[str, object] = {}
            try:
                raw_info = process.info
                if isinstance(raw_info, dict):
                    info = raw_info
            except (AttributeError, OSError, psutil.Error):
                pass
            raw_pid = info.get("pid")
            if raw_pid is None:
                raw_pid = _fallback_process_value(process, "pid")
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                continue
            if pid == os.getpid():
                continue
            name_value = info.get("name")
            if not name_value:
                name_value = _fallback_process_value(process, "name")
            if not name_value:
                name_value = _fallback_process_value(process, "exe")
            possible_python = _python_process_name(name_value)
            possible_uvicorn = Path(str(name_value or "")).stem.casefold() == "uvicorn"
            command_value = info.get("cmdline")
            if not command_value:
                command_value = _fallback_process_value(process, "cmdline")
            command = (
                [str(item) for item in command_value]
                if isinstance(command_value, (list, tuple))
                else []
            )
            if not command:
                if possible_python or (
                    not name_value
                    and not _obvious_non_python_system_process(process, pid)
                ):
                    unverifiable.add(pid)
                continue
            managed_writer = _is_managed_writer_command(command)
            web_process = _is_web_process_command(command)
            if not managed_writer and not (mode == "offline" and web_process):
                if mode == "offline" and (possible_python or possible_uvicorn):
                    unverifiable.add(pid)
                continue
            try:
                if mode == "offline":
                    process_database = _offline_process_database(
                        command,
                        _process_environment(process, info),
                        web_process=web_process,
                    )
                else:
                    process_database = _command_database(command)
            except (OSError, RuntimeError, ValueError):
                unverifiable.add(pid)
                continue
            if process_database != expected_database:
                continue
            created_value = info.get("create_time")
            if created_value is None:
                created_value = _fallback_process_value(process, "create_time")
            try:
                created_at = float(created_value)
            except (TypeError, ValueError):
                unverifiable.add(pid)
                continue
            identity = ProcessIdentity(pid, created_at)
            if not _identity_is_allowed(identity, allowed):
                conflicts.append(identity)
    except (OSError, psutil.Error):
        unverifiable.add(-1)
    recheck_factory = process_factory
    if recheck_factory is None and process_iter is _DEFAULT_PROCESS_ITER:
        recheck_factory = _DEFAULT_PROCESS_FACTORY
    if revalidation_passes and unverifiable and recheck_factory is not None:
        retry_processes: list[Any] = []
        persistent: set[int] = set()
        for pid in sorted(unverifiable):
            if pid <= 0:
                persistent.add(pid)
                continue
            try:
                retry_processes.append(recheck_factory(pid))
            except (KeyError, ProcessLookupError, psutil.NoSuchProcess):
                continue
            except (AttributeError, OSError, TypeError, ValueError, psutil.Error):
                persistent.add(pid)
        if retry_processes:
            rechecked = scan_managed_writers(
                database,
                allowed_identities=allowed,
                process_iter=lambda _attributes: retry_processes,
                process_factory=recheck_factory,
                mode=mode,
                revalidation_passes=revalidation_passes - 1,
            )
            conflicts.extend(rechecked.conflicts)
            persistent.update(rechecked.unverifiable_pids)
        unverifiable = persistent
    completed_identity = require_unique_database_file(
        expected_database,
        allow_missing=True,
    )
    if completed_identity != expected_identity:
        raise RuntimeError("database file identity changed during writer scan")
    return WriterScanResult(
        tuple(sorted(set(conflicts))),
        tuple(sorted(unverifiable)),
    )


def _process_identity(process: Any) -> tuple[ProcessIdentity | None, str | None]:
    try:
        return ProcessIdentity(
            int(process.pid),
            float(process.create_time()),
        ), None
    except psutil.NoSuchProcess:
        return None, "process_exited_before_identity"
    except (AttributeError, OSError, TypeError, ValueError, psutil.Error) as error:
        return None, f"identity_unverifiable:{type(error).__name__}"


def resolve_process_identity(
    identity: ProcessIdentity,
    process_factory: Callable[[int], Any],
) -> tuple[bool | None, Any | None, str | None]:
    try:
        process = process_factory(identity.pid)
    except (psutil.NoSuchProcess, KeyError):
        return False, None, None
    except (AttributeError, OSError, TypeError, ValueError, psutil.Error) as error:
        return None, None, f"identity_unverifiable:{type(error).__name__}"
    try:
        created_at = float(process.create_time())
        if created_at != identity.created_at:
            return False, None, None
        running = bool(process.is_running())
        status = process.status()
    except psutil.NoSuchProcess:
        return False, None, None
    except (AttributeError, OSError, TypeError, ValueError, psutil.Error) as error:
        return None, None, f"identity_unverifiable:{type(error).__name__}"
    if not running or status == psutil.STATUS_ZOMBIE:
        return False, None, None
    return True, process, None


def _resume_suspended(
    suspended: dict[ProcessIdentity, Any],
    process_factory: Callable[[int], Any],
) -> list[str]:
    errors: list[str] = []
    for identity in reversed(tuple(suspended)):
        alive, process, resolve_error = resolve_process_identity(
            identity, process_factory
        )
        if resolve_error is not None:
            errors.append(f"resume_verify_failed:{identity.pid}:{resolve_error}")
            continue
        if not alive or process is None:
            continue
        try:
            process.resume()
        except psutil.NoSuchProcess:
            continue
        except (AttributeError, OSError, psutil.Error) as error:
            errors.append(
                f"resume_failed:{identity.pid}:{type(error).__name__}"
            )
    return errors


def _capture_stable_process_tree(
    process: Any,
    *,
    expected_root: ProcessIdentity | None,
    process_factory: Callable[[int], Any],
    max_passes: int,
) -> tuple[dict[ProcessIdentity, Any] | None, str | None]:
    root_identity, identity_error = _process_identity(process)
    if root_identity is None:
        return None, identity_error
    if expected_root is not None and (
        root_identity.pid != expected_root.pid
        or root_identity.created_at != expected_root.created_at
    ):
        return None, "root_identity_changed_before_suspend"

    suspended: dict[ProcessIdentity, Any] = {}

    def fail(reason: str) -> tuple[None, str]:
        resume_errors = _resume_suspended(suspended, process_factory)
        details = [reason, *resume_errors]
        return None, ";".join(details)

    try:
        process.suspend()
    except psutil.NoSuchProcess:
        return fail("root_exited_before_tree_capture")
    except (AttributeError, OSError, psutil.Error) as error:
        return fail(f"suspend_failed:{type(error).__name__}")
    suspended[root_identity] = process

    previous: frozenset[ProcessIdentity] | None = None
    for _ in range(max_passes):
        try:
            children = list(process.children(recursive=True))
        except psutil.NoSuchProcess:
            return fail("root_exited_during_tree_capture")
        except (AttributeError, OSError, psutil.Error) as error:
            return fail(f"tree_enumeration_failed:{type(error).__name__}")
        current: dict[ProcessIdentity, Any] = {}
        for child in children:
            identity, error = _process_identity(child)
            if identity is None:
                if error == "process_exited_before_identity":
                    continue
                return fail(str(error))
            current[identity] = child
            if identity in suspended:
                continue
            try:
                child.suspend()
            except psutil.NoSuchProcess:
                continue
            except (AttributeError, OSError, psutil.Error) as suspend_error:
                return fail(f"suspend_failed:{type(suspend_error).__name__}")
            suspended[identity] = child
        fingerprint = frozenset(current)
        if fingerprint == previous:
            return suspended, None
        previous = fingerprint
    return fail("tree_enumeration_not_stable")


def terminate_process_tree(
    process: Any,
    *,
    process_factory: Callable[[int], Any] = psutil.Process,
    expected_root: ProcessIdentity | None = None,
    terminate_timeout: float = 8,
    kill_timeout: float = 3,
    max_tree_passes: int = 8,
) -> TerminationResult:
    """Stop and prove one process tree, restoring suspension on capture failure."""

    tree, error = _capture_stable_process_tree(
        process,
        expected_root=expected_root,
        process_factory=process_factory,
        max_passes=max_tree_passes,
    )
    if tree is None:
        return TerminationResult(False, error)
    root_identity = expected_root or next(
        (identity for identity, target in tree.items() if target is process),
        None,
    )
    if root_identity is None:
        resume_errors = _resume_suspended(tree, process_factory)
        return TerminationResult(
            False,
            ";".join(("root_identity_lost", *resume_errors)),
        )
    ordered = [
        (identity, target)
        for identity, target in tree.items()
        if identity != root_identity
    ]
    ordered.append((root_identity, process))
    errors: list[str] = []

    for identity, target in ordered:
        try:
            target.terminate()
        except psutil.NoSuchProcess:
            continue
        except (AttributeError, OSError, psutil.Error) as terminate_error:
            errors.append(
                f"terminate_failed:{identity.pid}:"
                f"{type(terminate_error).__name__}"
            )

    survivors: list[tuple[ProcessIdentity, Any]] = []
    for identity, target in ordered:
        try:
            target.wait(timeout=terminate_timeout)
        except (psutil.TimeoutExpired, subprocess.TimeoutExpired):
            pass
        except psutil.NoSuchProcess:
            continue
        except (AttributeError, OSError, psutil.Error) as wait_error:
            errors.append(
                f"wait_failed:{identity.pid}:{type(wait_error).__name__}"
            )
        alive, current, resolve_error = resolve_process_identity(
            identity, process_factory
        )
        if resolve_error is not None:
            errors.append(f"verify_failed:{identity.pid}:{resolve_error}")
        elif alive and current is not None:
            survivors.append((identity, current))

    for identity, target in survivors:
        try:
            target.kill()
        except psutil.NoSuchProcess:
            continue
        except (AttributeError, OSError, psutil.Error) as kill_error:
            errors.append(
                f"kill_failed:{identity.pid}:{type(kill_error).__name__}"
            )
            continue
        try:
            target.wait(timeout=kill_timeout)
        except psutil.NoSuchProcess:
            pass
        except (psutil.TimeoutExpired, subprocess.TimeoutExpired) as wait_error:
            errors.append(
                f"kill_wait_failed:{identity.pid}:{type(wait_error).__name__}"
            )
        except (AttributeError, OSError, psutil.Error) as wait_error:
            errors.append(
                f"kill_wait_failed:{identity.pid}:{type(wait_error).__name__}"
            )

    final_failure = False
    for identity in tree:
        alive, _, resolve_error = resolve_process_identity(
            identity, process_factory
        )
        if resolve_error is not None:
            errors.append(f"final_verify_failed:{identity.pid}:{resolve_error}")
            final_failure = True
        elif alive:
            errors.append(f"process_still_alive:{identity.pid}")
            final_failure = True
    if final_failure:
        errors.extend(_resume_suspended(tree, process_factory))
        return TerminationResult(False, ";".join(dict.fromkeys(errors)))
    return TerminationResult(True)


def terminate_subprocess_tree(
    process_handle: Any,
    *,
    process_factory: Callable[[int], Any] = psutil.Process,
    terminate_timeout: float = 8,
    kill_timeout: float = 3,
    max_tree_passes: int = 8,
) -> TerminationResult:
    """Stop a live ``Popen`` tree without ever targeting a reused PID."""

    try:
        if process_handle.poll() is not None:
            return TerminationResult(True)
        pid = int(process_handle.pid)
    except (AttributeError, OSError, TypeError, ValueError) as error:
        return TerminationResult(
            False,
            f"subprocess_identity_unverifiable:{type(error).__name__}",
        )
    try:
        process = process_factory(pid)
        identity = ProcessIdentity(pid, float(process.create_time()))
    except psutil.NoSuchProcess:
        try:
            exited = process_handle.poll() is not None
        except (AttributeError, OSError):
            exited = False
        return TerminationResult(
            exited,
            None if exited else "subprocess_identity_missing",
        )
    except (
        KeyError,
        AttributeError,
        OSError,
        TypeError,
        ValueError,
        psutil.Error,
    ) as error:
        return TerminationResult(
            False,
            f"subprocess_identity_unverifiable:{type(error).__name__}",
        )
    try:
        if process_handle.poll() is not None:
            return TerminationResult(True)
    except (AttributeError, OSError) as error:
        return TerminationResult(
            False,
            f"subprocess_identity_unverifiable:{type(error).__name__}",
        )
    return terminate_process_tree(
        process,
        process_factory=process_factory,
        expected_root=identity,
        terminate_timeout=terminate_timeout,
        kill_timeout=kill_timeout,
        max_tree_passes=max_tree_passes,
    )


__all__ = [
    "DatabaseFileIdentity",
    "DirectoryIdentity",
    "LockOwnerMetadata",
    "ProcessIdentity",
    "ServiceDataPaths",
    "SingleInstanceLock",
    "StableRegularFile",
    "TerminationResult",
    "WriterScanResult",
    "add_single_database_argument",
    "bind_manager_child_authority",
    "capture_directory_identity",
    "command_comparison_key",
    "database_authority_lock_paths",
    "database_global_authority_lock_paths",
    "database_global_service_lock_path",
    "database_global_web_lock_path",
    "database_local_authority_lock_paths",
    "database_offline_authority",
    "database_service_authority_lock_paths",
    "database_service_lock_path",
    "database_web_authority_lock_paths",
    "database_web_lock_path",
    "database_writer_authority",
    "delegated_writer_process_environment",
    "manager_child_authority",
    "manager_child_process_environment",
    "managed_child_command",
    "managed_child_entrypoint",
    "managed_child_target",
    "fsync_directory",
    "read_stable_regular_file",
    "require_current_process_lock",
    "scan_managed_writers",
    "resolve_process_identity",
    "require_unique_database_file",
    "require_directory_identity",
    "service_data_paths",
    "terminate_process_tree",
    "terminate_subprocess_tree",
    "web_fetch_child_authority",
    "web_fetch_process_environment",
]
