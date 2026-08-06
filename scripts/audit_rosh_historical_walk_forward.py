"""Run the 20-map R.O.S.H. historical temporal-semantics audit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import build_engine, require_database_url  # noqa: E402
from database.session import PostgresSession  # noqa: E402
from event_intelligence.rosh_historical_walk_forward import (  # noqa: E402
    StratzBatchTransport,
    load_temporal_sample,
    report_as_dict,
    run_temporal_semantics_audit,
)


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="PostgreSQL URL (default: DATABASE_URL)")
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--throttle-seconds", type=_nonnegative_float, default=1.0)
    parser.add_argument("--timeout-seconds", type=_nonnegative_float, default=30.0)
    return parser


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = str(
        os.environ.get("STRATZ_API_TOKEN") or os.environ.get("STRATZ_TOKEN") or ""
    ).strip()
    if not token:
        raise ValueError("STRATZ_API_TOKEN is required")
    engine = build_engine(require_database_url(args.database_url))
    session = PostgresSession(engine)
    try:
        with session.transaction():
            session.execute("SET TRANSACTION READ ONLY")
            sample = load_temporal_sample(session)
    finally:
        session.close()
        engine.dispose()
    report = run_temporal_semantics_audit(
        sample,
        transport=StratzBatchTransport(token, timeout_seconds=args.timeout_seconds),
        artifact_root=args.artifact_root,
        throttle_seconds=args.throttle_seconds,
    )
    payload = json.dumps(
        report_as_dict(report),
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
    )
    if args.json_output is not None:
        _write(args.json_output, payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
