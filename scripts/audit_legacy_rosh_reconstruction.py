"""Audit legacy R.O.S.H. reconstruction using only persisted local evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import build_engine, require_database_url  # noqa: E402
from database.session import PostgresSession  # noqa: E402
from event_intelligence.legacy_rosh_reconstruction import (  # noqa: E402
    audit_legacy_rosh_reconstruction,
    report_as_dict,
    report_as_markdown,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="PostgreSQL URL (default: DATABASE_URL)")
    parser.add_argument("--max-rows", type=_positive_int)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = build_engine(require_database_url(args.database_url))
    session = PostgresSession(engine)
    try:
        with session.transaction():
            session.execute("SET TRANSACTION READ ONLY")
            report = audit_legacy_rosh_reconstruction(
                session,
                max_rows=args.max_rows,
            )
    finally:
        session.close()
        engine.dispose()
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
