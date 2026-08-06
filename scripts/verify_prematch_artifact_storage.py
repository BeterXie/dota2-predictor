"""Verify shared Prematch corpus storage in an isolated acceptance database."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import require_database_url  # noqa: E402
from database.session import PostgresSession  # noqa: E402
from event_intelligence.prematch_backtest import (  # noqa: E402
    persist_prematch_backtest_result,
    run_prematch_backtest,
)
from event_intelligence.prematch_report import build_prematch_report  # noqa: E402
from event_intelligence.prematch_storage import (  # noqa: E402
    load_prematch_model_artifact,
)
from event_intelligence.raw_archive import canonical_json_bytes  # noqa: E402
from event_intelligence.storage import IntelligenceStorage  # noqa: E402


def _acceptance_maps(value: str) -> int:
    parsed = int(value)
    if not 100 <= parsed <= 300:
        raise argparse.ArgumentTypeError("must be between 100 and 300")
    return parsed


def _require_isolated_database(database_url: str) -> str:
    configured = require_database_url(database_url)
    database = make_url(configured).database or ""
    if not database.endswith("_artifact_acceptance"):
        raise ValueError("refusing non-isolated database")
    return configured


def _scalar(
    connection: PostgresSession,
    statement: str,
    parameters: Sequence[object] = (),
) -> object:
    return connection.execute(statement, parameters).scalar_one()


def _counts(connection: PostgresSession) -> dict[str, int]:
    return {
        "corpus_rows": int(
            _scalar(connection, "SELECT COUNT(*) FROM prematch_training_corpus_rows")
        ),
        "corpus_prefixes": int(
            _scalar(
                connection,
                "SELECT COUNT(*) FROM prematch_training_corpus_prefixes",
            )
        ),
        "model_runs": int(
            _scalar(connection, "SELECT COUNT(*) FROM prematch_model_runs")
        ),
    }


def _relation_bytes(connection: PostgresSession) -> dict[str, int]:
    return {
        "corpus_rows_bytes": int(
            _scalar(
                connection,
                "SELECT pg_total_relation_size('prematch_training_corpus_rows')",
            )
        ),
        "corpus_prefixes_bytes": int(
            _scalar(
                connection,
                "SELECT pg_total_relation_size('prematch_training_corpus_prefixes')",
            )
        ),
    }


def verify_storage(
    database_url: str,
    *,
    artifact_root: str | Path,
    max_maps: int,
) -> dict[str, Any]:
    configured = _require_isolated_database(database_url)
    if not 100 <= max_maps <= 300:
        raise ValueError("max_maps must be between 100 and 300")
    with IntelligenceStorage(configured) as storage:
        storage.init_schema()
        connection = storage.connection
        before = _counts(connection)
        started = time.perf_counter()
        result = run_prematch_backtest(
            storage,
            artifact_root=artifact_root,
            max_maps=max_maps,
        )
        training_seconds = time.perf_counter() - started
        report = build_prematch_report(result)

        started = time.perf_counter()
        first = persist_prematch_backtest_result(
            result,
            storage,
            report=report,
            dry_run=False,
        )
        first_write_seconds = time.perf_counter() - started

        started = time.perf_counter()
        second = persist_prematch_backtest_result(
            result,
            storage,
            report=report,
            dry_run=False,
        )
        repeated_write_seconds = time.perf_counter() - started

        final_model = next(
            row.model_artifact
            for row in result.final_models
            if row.model_artifact.model_kind == "team_plus_draft_rosh"
        )
        if final_model.support == 0:
            raise ValueError("Draft + R.O.S.H. final model has no corpus support")
        final_hash = final_model.model_hash
        started = time.perf_counter()
        loaded = load_prematch_model_artifact(connection, final_hash)
        initial_load_seconds = time.perf_counter() - started
        if loaded.model_hash != final_hash:
            raise ValueError("loaded final model hash mismatch")

        prefix_hash = _scalar(
            connection,
            """SELECT artifact_json::jsonb #>> '{training_corpus,prefix_hash}'
                 FROM prematch_model_runs WHERE run_id=?""",
            (final_hash,),
        )
        if not isinstance(prefix_hash, str) or not prefix_hash:
            raise ValueError("final model lacks a shared corpus prefix")

        try:
            with connection.transaction():
                connection.execute(
                    """DELETE FROM prematch_training_corpus_prefixes
                        WHERE prefix_hash=?""",
                    (prefix_hash,),
                )
        except DBAPIError:
            delete_rejected = True
        else:
            delete_rejected = False
        if not delete_rejected:
            raise ValueError("append-only corpus prefix deletion was not rejected")
        reloaded = load_prematch_model_artifact(connection, final_hash)

        probe_payload = {"probe": "simulated_interruption"}
        probe_json = canonical_json_bytes(probe_payload).decode()
        probe_hash = hashlib.sha256(
            canonical_json_bytes(
                {
                    "domain": "prematch-training-row/v1",
                    "model_kind": "team_only",
                    "row": probe_payload,
                }
            )
        ).hexdigest()
        try:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO prematch_training_corpus_rows
                           (row_hash, model_kind, row_json, created_at)
                         VALUES (?, 'team_only', ?, ?)""",
                    (
                        probe_hash,
                        probe_json,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                raise RuntimeError("simulated_interruption")
        except RuntimeError as error:
            if str(error) != "simulated_interruption":
                raise
        probe_rows = int(
            _scalar(
                connection,
                """SELECT COUNT(*) FROM prematch_training_corpus_rows
                    WHERE row_hash=?""",
                (probe_hash,),
            )
        )
        if probe_rows != 0:
            raise ValueError("interrupted corpus insert did not roll back")
        rollback_loaded = load_prematch_model_artifact(connection, final_hash)

        after = {**_counts(connection), **_relation_bytes(connection)}

    return {
        "database": make_url(configured).database,
        "maps": max_maps,
        "before": before,
        "after": after,
        "training_seconds": training_seconds,
        "first_write_seconds": first_write_seconds,
        "repeated_write_seconds": repeated_write_seconds,
        "initial_load_seconds": initial_load_seconds,
        "first_counts": asdict(first.counts),
        "second_counts": asdict(second.counts),
        "settled_predictions": first.settled_predictions,
        "unchanged_settlements_on_repeat": second.unchanged_settlements,
        "append_only_delete_rejected": delete_rejected,
        "load_after_rejected_delete_verified": reloaded.model_hash == final_hash,
        "interruption_rollback_verified": rollback_loaded.model_hash == final_hash,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="isolated PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--max-maps", type=_acceptance_maps, default=100)
    parser.add_argument("--json-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = verify_storage(
        require_database_url(args.database_url),
        artifact_root=args.artifact_root,
        max_maps=args.max_maps,
    )
    serialized = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
    )
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(serialized + "\n", encoding="utf-8", newline="\n")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
