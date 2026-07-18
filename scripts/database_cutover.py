"""Fail-closed SQLite checkpoint and prepared-database cutover checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_betting.database_protocol import (  # noqa: E402
    truncate_wal_checkpoint,
    verify_prepared_database,
)
from scripts.run_dota_shadow_service import SingleInstanceLock  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    checkpoint = commands.add_parser(
        "checkpoint",
        help="checkpoint and truncate WAL after all managed writers stop",
    )
    checkpoint.add_argument("--database", type=Path, required=True)
    checkpoint.add_argument(
        "--lock",
        type=Path,
        help="supervisor lock path; defaults to <database>.service.lock",
    )

    verify = commands.add_parser(
        "verify-prepared",
        help="verify schema, contracts, and artifacts using mode=ro/query_only",
    )
    verify.add_argument("--database", type=Path, required=True)
    verify.add_argument("--odds-raw-root", type=Path)
    return parser


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
    lock = (args.lock or database.with_suffix(".service.lock")).resolve()
    try:
        with SingleInstanceLock(lock):
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
                    service_lock=str(lock),
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
        "service_lock": str(lock),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if result.safe else 1


def _verify_prepared(args: argparse.Namespace) -> int:
    database = args.database.resolve()
    odds_raw_root = (
        args.odds_raw_root.resolve()
        if args.odds_raw_root is not None
        else database.parent / "live_betting" / "raw-v2"
    )
    try:
        result = verify_prepared_database(database, odds_raw_root=odds_raw_root)
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
                "open_mode": "ro",
                "query_only": True,
                "odds_raw_root": str(odds_raw_root),
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
