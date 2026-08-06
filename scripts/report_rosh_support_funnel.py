"""Report where historical R.O.S.H. evidence loses Prematch support."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import require_database_url  # noqa: E402
from event_intelligence.rosh_support_funnel import (  # noqa: E402
    build_rosh_support_funnel,
    report_as_dict,
    report_as_markdown,
)
from event_intelligence.storage import IntelligenceStorage  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        required=True,
        help="root containing authoritative R.O.S.H. artifacts",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with IntelligenceStorage(require_database_url(args.database_url)) as storage:
        storage.init_schema()
        report = build_rosh_support_funnel(
            storage.connection,
            artifact_root=args.artifact_root,
        )
    payload = json.dumps(
        report_as_dict(report),
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
    )
    if args.json_output is not None:
        _write(args.json_output, payload + "\n")
    if args.markdown_output is not None:
        _write(args.markdown_output, report_as_markdown(report))
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
