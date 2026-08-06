"""Run the read-only legacy pure R.O.S.H. retrospective utility analysis."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import build_engine, require_database_url  # noqa: E402
from database.session import PostgresSession  # noqa: E402
from event_intelligence.rosh_retrospective_utility import (  # noqa: E402
    DEFAULT_BOOTSTRAP_SAMPLES,
    DEFAULT_SANITY_PERMUTATIONS,
    analysis_as_json,
    analysis_as_markdown,
    build_analysis,
    load_retrospective_cohort,
)


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="PostgreSQL URL (default: DATABASE_URL)")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument(
        "--bootstrap-samples",
        type=_positive_integer,
        default=DEFAULT_BOOTSTRAP_SAMPLES,
    )
    parser.add_argument(
        "--sanity-permutations",
        type=_positive_integer,
        default=DEFAULT_SANITY_PERMUTATIONS,
    )
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
            session.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            cohort = load_retrospective_cohort(session)
    finally:
        session.close()
        engine.dispose()
    analysis = build_analysis(
        cohort,
        bootstrap_samples=args.bootstrap_samples,
        sanity_permutations=args.sanity_permutations,
    )
    payload = analysis_as_json(analysis)
    markdown = analysis_as_markdown(analysis)
    if args.json_output is not None:
        _write(args.json_output, payload + "\n")
    if args.markdown_output is not None:
        _write(args.markdown_output, markdown)
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
