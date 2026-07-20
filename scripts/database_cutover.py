"""Fail-closed SQLite checkpoint and prepared-database cutover checks."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_betting.database_protocol import (  # noqa: E402
    sqlite_sidecar_state,
    truncate_wal_checkpoint,
    verify_prepared_database,
)
from live_betting.service_coordination import (  # noqa: E402
    DatabaseFileIdentity,
    SingleInstanceLock,
    add_single_database_argument,
    database_authority_lock_paths,
    database_service_lock_path,
    database_web_lock_path,
    require_unique_database_file,
    scan_managed_writers,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    checkpoint = commands.add_parser(
        "checkpoint",
        help="checkpoint and truncate WAL after all managed writers stop",
    )
    add_single_database_argument(checkpoint, required=True)
    checkpoint.add_argument(
        "--lock",
        type=Path,
        help=(
            "additional lock path; both global role locks and both local locks "
            "are always held"
        ),
    )

    verify = commands.add_parser(
        "verify-prepared",
        help="verify schema, contracts, and artifacts using mode=ro/query_only",
    )
    add_single_database_argument(verify, required=True)
    verify.add_argument("--odds-raw-root", type=Path)
    verify.add_argument(
        "--lock",
        type=Path,
        help=(
            "additional lock path; both global role locks and both local locks "
            "are always held"
        ),
    )
    return parser


def _require_no_writers(database: Path) -> None:
    result = scan_managed_writers(database, mode="offline")
    if result.unverifiable_pids:
        raise RuntimeError(
            "managed writer scan could not verify PIDs: "
            + ",".join(str(pid) for pid in result.unverifiable_pids)
        )
    if result.conflicts:
        raise RuntimeError(
            "managed writers still target this database: "
            + ",".join(str(item.pid) for item in result.conflicts)
        )


@contextmanager
def _database_authority(
    database: Path,
    custom_lock: Path | None,
) -> Iterator[tuple[DatabaseFileIdentity, Path, Path, Path | None]]:
    initial_identity = require_unique_database_file(database)
    assert initial_identity is not None
    standard_lock = database_service_lock_path(database)
    web_lock = database_web_lock_path(database)
    authority_locks = database_authority_lock_paths(database)
    additional_lock = custom_lock.resolve() if custom_lock is not None else None
    if additional_lock in set(authority_locks):
        additional_lock = None
    with ExitStack() as locks:
        for authority_lock in authority_locks:
            locks.enter_context(SingleInstanceLock(authority_lock))
        if additional_lock is not None:
            locks.enter_context(SingleInstanceLock(additional_lock))
        locked_identity = require_unique_database_file(
            database,
            expected_identity=initial_identity,
        )
        assert locked_identity is not None
        _require_no_writers(database)
        require_unique_database_file(
            database,
            expected_identity=locked_identity,
        )
        try:
            yield locked_identity, standard_lock, web_lock, additional_lock
        finally:
            failures: list[str] = []
            for label, check in (
                (
                    "identity_before_scan",
                    lambda: require_unique_database_file(
                        database,
                        expected_identity=locked_identity,
                    ),
                ),
                ("writer_scan", lambda: _require_no_writers(database)),
                (
                    "identity_after_scan",
                    lambda: require_unique_database_file(
                        database,
                        expected_identity=locked_identity,
                    ),
                ),
            ):
                try:
                    check()
                except Exception as error:
                    failures.append(
                        f"{label}:{type(error).__name__}:{error}"
                    )
            if failures:
                raise RuntimeError(
                    "database authority changed after cutover operation: "
                    + ";".join(failures)
                )


def _error_payload(
    command: str,
    database: Path,
    error: Exception,
    **fields: object,
) -> dict[str, object]:
    return {
        "status": "error",
        "command": command,
        "database": str(database.resolve()),
        **fields,
        "error_type": type(error).__name__,
        "error": " ".join(str(error).split()),
    }


def _checkpoint(args: argparse.Namespace) -> int:
    database = args.database.resolve()
    standard_lock = database_service_lock_path(database)
    web_lock = database_web_lock_path(database)
    additional_lock = args.lock.resolve() if args.lock is not None else None
    if additional_lock in {standard_lock, web_lock}:
        additional_lock = None
    try:
        with _database_authority(database, additional_lock):
            result = truncate_wal_checkpoint(database)
    except Exception as error:
        print(
            json.dumps(
                _error_payload(
                    "checkpoint",
                    database,
                    error,
                    busy=None,
                    log=None,
                    checkpoint=None,
                    wal_bytes=(
                        Path(f"{database}-wal").stat().st_size
                        if Path(f"{database}-wal").exists()
                        else 0
                    ),
                    sqlite_sidecars=(
                        sqlite_sidecar_state(database)
                        if database.exists()
                        else None
                    ),
                    service_lock=str(standard_lock),
                    web_lock=str(web_lock),
                    additional_lock=(
                        str(additional_lock) if additional_lock is not None else None
                    ),
                ),
                sort_keys=True,
            )
        )
        return 1
    payload = {
        "status": "ok" if result.safe else "unsafe",
        "command": "checkpoint",
        "database": str(result.database),
        "busy": result.busy,
        "log": result.log,
        "checkpoint": result.checkpoint,
        "wal_bytes": result.wal_bytes,
        "sqlite_sidecars": getattr(result, "sidecars", sqlite_sidecar_state(database)),
        "service_lock": str(standard_lock),
        "web_lock": str(web_lock),
        "additional_lock": (
            str(additional_lock) if additional_lock is not None else None
        ),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if result.safe else 1


def _verify_prepared(args: argparse.Namespace) -> int:
    database = args.database.resolve()
    standard_lock = database_service_lock_path(database)
    web_lock = database_web_lock_path(database)
    additional_lock = args.lock.resolve() if args.lock is not None else None
    if additional_lock in {standard_lock, web_lock}:
        additional_lock = None
    odds_raw_root = (
        args.odds_raw_root.resolve()
        if args.odds_raw_root is not None
        else database.parent / "live_betting" / "raw-v2"
    )
    try:
        with _database_authority(database, additional_lock):
            result = verify_prepared_database(
                database,
                odds_raw_root=odds_raw_root,
                immutable_locks=database_authority_lock_paths(database),
            )
    except Exception as error:
        print(
            json.dumps(
                _error_payload(
                    "verify-prepared",
                    database,
                    error,
                    open_mode="ro",
                    query_only=True,
                    odds_raw_root=str(odds_raw_root),
                    service_lock=str(standard_lock),
                    web_lock=str(web_lock),
                    additional_lock=(
                        str(additional_lock) if additional_lock is not None else None
                    ),
                ),
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "command": "verify-prepared",
                "database": str(result.database),
                "live_schema_version": result.live_schema_version,
                "intelligence_schema_version": result.intelligence_schema_version,
                "runtime_schema_version": result.runtime_schema_version,
                "open_mode": "ro",
                "query_only": True,
                "odds_raw_root": str(odds_raw_root),
                "service_lock": str(standard_lock),
                "web_lock": str(web_lock),
                "additional_lock": (
                    str(additional_lock) if additional_lock is not None else None
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "checkpoint":
        return _checkpoint(args)
    return _verify_prepared(args)


if __name__ == "__main__":
    raise SystemExit(main())
