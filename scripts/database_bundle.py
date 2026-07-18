"""Create, verify, or restore a self-contained database and raw-artifact bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_betting.database_bundle import (  # noqa: E402
    bundle_result_json,
    create_database_bundle,
    restore_database_bundle,
    verify_database_bundle,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create and fully verify a bundle")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--odds-raw-root", type=Path, required=True)
    create.add_argument("--bundle", type=Path, required=True)
    create.add_argument(
        "--allow-source-root",
        type=Path,
        action="append",
        default=[],
        help="additional allowed root for absolute raw_source_artifacts paths",
    )
    create.add_argument(
        "--resume",
        action="store_true",
        help="resume a target-bound staging checkpoint",
    )

    verify = commands.add_parser("verify", help="verify every bundled byte")
    verify.add_argument("--bundle", type=Path, required=True)

    restore = commands.add_parser("restore", help="restore into a new directory")
    restore.add_argument("--bundle", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--database-name", default="dota2.db")
    restore.add_argument(
        "--resume",
        action="store_true",
        help="resume a target-bound restore staging checkpoint",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "create":
        result = create_database_bundle(
            args.database,
            args.odds_raw_root,
            args.bundle,
            allowed_source_roots=args.allow_source_root,
            resume=args.resume,
        )
        print(bundle_result_json(result))
        return 0
    if args.command == "verify":
        manifest = verify_database_bundle(args.bundle)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "bundle": str(args.bundle.resolve()),
                    "database_sha256": manifest["database"]["sha256"],
                    "artifact_count": manifest["artifact_count"],
                    "git_commit": manifest["git_commit"],
                },
                sort_keys=True,
            )
        )
        return 0
    result = restore_database_bundle(
        args.bundle,
        args.destination,
        database_name=args.database_name,
        resume=args.resume,
    )
    print(bundle_result_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
