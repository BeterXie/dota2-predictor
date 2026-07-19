"""Incrementally archive approved Tier-1 Dota 2 completed matches."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_intelligence.ingest import (  # noqa: E402
    StrictEventIngestor,
    completed_match_processing_result,
)
from event_intelligence.scheduler import IngestScheduler  # noqa: E402
from live_betting.service_coordination import (  # noqa: E402
    add_single_database_argument,
    database_writer_authority,
)


logger = logging.getLogger(__name__)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _non_empty_text(value: str) -> str:
    parsed = value.strip()
    if not parsed:
        raise argparse.ArgumentTypeError("must not be empty")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(parser, default=ROOT / "data" / "dota2.db")
    parser.add_argument(
        "--archive-root",
        type=Path,
    )
    parser.add_argument("--once", action="store_true", help="run one immediate cycle")
    parser.add_argument(
        "--scheduler-once",
        action="store_true",
        help="run one due scheduler cycle and persist its checkpoints",
    )
    parser.add_argument(
        "--event", type=_non_empty_text, help="approved internal event ID"
    )
    parser.add_argument(
        "--match", type=_positive_int, help="formal discovered match ID"
    )
    parser.add_argument(
        "--active",
        action="store_true",
        help="limit discovery to active approved events",
    )
    parser.add_argument(
        "--reconcile", action="store_true", help="update event reconciliation evidence"
    )
    parser.add_argument("--interval", type=_positive_float, default=30.0)
    parser.add_argument("--max-concurrency", type=_positive_int, default=3)
    parser.add_argument("--rate-limit", type=_positive_int, default=30)
    parser.add_argument(
        "--coverage-report",
        type=Path,
    )
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    return parser


def resolve_data_paths(args: argparse.Namespace) -> argparse.Namespace:
    database = Path(args.database).resolve()
    args.database = database
    args.archive_root = (
        Path(args.archive_root).resolve()
        if args.archive_root is not None
        else database.parent / "raw-sources"
    )
    args.coverage_report = (
        Path(args.coverage_report).resolve()
        if args.coverage_report is not None
        else database.parent / "reports" / "strict_event_coverage_latest.json"
    )
    return args


@dataclass
class Runtime:
    ingestor: StrictEventIngestor
    scheduler: IngestScheduler
    close_callbacks: tuple[Callable[[], object], ...] = ()
    database: Path | None = None
    health_connection: sqlite3.Connection | None = None
    derived_pipeline: object | None = None
    coverage_report: Path | None = None

    async def close(self) -> None:
        for callback in reversed(self.close_callbacks):
            result = callback()
            if asyncio.iscoroutine(result):
                await result


def build_default_runtime(args: argparse.Namespace) -> Runtime:
    """Late-bind concrete ports so importing this CLI has no database side effects."""
    args = resolve_data_paths(args)
    try:
        from event_intelligence.opendota import OpenDotaAdapter
        from event_intelligence.raw_archive import RawArchive
        from event_intelligence.ingest_adapters import (
            RegistryIngestAdapter,
            SQLiteIngestAdapter,
        )
        from event_intelligence.registry import EventRegistry
        from event_intelligence.incremental import StrictDerivedPipeline
        from event_intelligence.storage import IntelligenceStorage
        from fetch.db import Database
    except (ImportError, ModuleNotFoundError) as error:
        raise RuntimeError(
            "strict ingestion components are incomplete; install the event_intelligence "
            f"storage/registry/facts adapters ({error})"
        ) from error

    storage = IntelligenceStorage(args.database)
    try:
        if not getattr(args, "schema_prepared", False):
            storage.init_schema()
        legacy_database = Database(connection=storage.connection)
        if not getattr(args, "schema_prepared", False):
            legacy_database.init_db()
        registry_impl = EventRegistry(storage)
        registry = RegistryIngestAdapter(registry_impl)
        store = SQLiteIngestAdapter(storage, registry_impl, legacy_database)
    except Exception:
        storage.close()
        raise

    def clock() -> datetime:
        return datetime.now(timezone.utc)

    archive = RawArchive(args.archive_root, observation_sink=store.record_raw_artifact)
    client = OpenDotaAdapter(clock=clock, rate_limit=args.rate_limit)
    ingestor = StrictEventIngestor(
        registry,
        store,
        archive,
        client,
        processor=completed_match_processing_result,
        clock=clock,
        max_concurrency=args.max_concurrency,
    )
    scheduler = IngestScheduler(ingestor, store)
    callbacks = tuple(
        callback
        for callback in (
            getattr(client, "close", None),
            getattr(storage, "close", None),
        )
        if callback is not None
    )
    return Runtime(
        ingestor,
        scheduler,
        callbacks,
        database=args.database.resolve(),
        health_connection=storage.connection,
        derived_pipeline=StrictDerivedPipeline(args.database),
        coverage_report=args.coverage_report,
    )


def _report_json(value: object) -> str:
    try:
        payload = asdict(value)  # type: ignore[arg-type]
    except TypeError:
        payload = value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


async def run(
    args: argparse.Namespace,
    runtime_factory: Callable[[argparse.Namespace], Runtime] = build_default_runtime,
) -> int:
    if args.match is not None and args.active:
        raise ValueError("--match cannot be combined with --active")
    if args.scheduler_once and (
        args.once or args.event is not None or args.match is not None or args.reconcile
    ):
        raise ValueError(
            "--scheduler-once cannot be combined with direct one-shot filters"
        )
    runtime = runtime_factory(args)
    try:
        _record_runtime_health(runtime, "starting", datetime.now(timezone.utc))
        direct_one_shot = bool(
            args.once
            or args.event is not None
            or args.match is not None
            or args.reconcile
        )
        while True:
            now = datetime.now(timezone.utc)
            try:
                if direct_one_shot:
                    report = await runtime.ingestor.run_once(
                        event_id=args.event,
                        match_id=args.match,
                        active_only=args.active,
                        reconcile=args.reconcile,
                        now=now,
                    )
                else:
                    report = await runtime.scheduler.run_due(
                        now, include_recent=not args.active
                    )
                derived = _run_derived(runtime, report)
                if _report_due(report, direct_one_shot):
                    _write_coverage(runtime, now)
                candidate_error = getattr(report, "candidate_error", None)
                successful = _report_due(report, direct_one_shot)
                _record_runtime_health(
                    runtime,
                    "degraded" if candidate_error else "healthy",
                    datetime.now(timezone.utc),
                    report=report,
                    derived=derived,
                    successful=successful,
                    error=candidate_error,
                    error_at=getattr(report, "candidate_error_at", None),
                )
            except BaseException as error:
                failed_at = datetime.now(timezone.utc)
                try:
                    _record_runtime_health(
                        runtime,
                        "unhealthy",
                        failed_at,
                        error=error,
                        error_at=failed_at,
                    )
                except Exception:
                    logger.exception("Failed to persist strict_ingest_worker health")
                raise
            print(_report_json(report), flush=True)
            if direct_one_shot or args.scheduler_once:
                return 0
            await asyncio.sleep(args.interval)
    finally:
        await runtime.close()


def _run_derived(runtime: Runtime, report: object) -> object | None:
    pipeline = runtime.derived_pipeline
    if pipeline is None:
        return None
    return pipeline.run(getattr(report, "changed_match_ids", ()))


def _report_due(report: object, direct_one_shot: bool) -> bool:
    return direct_one_shot or any(
        bool(getattr(report, field, False))
        for field in ("active_polled", "recent_rescanned", "candidate_scanned")
    )


def _write_coverage(runtime: Runtime, generated_at: datetime) -> None:
    if runtime.database is None or runtime.coverage_report is None:
        return
    from event_intelligence.coverage import write_coverage_report

    write_coverage_report(
        runtime.database,
        runtime.coverage_report,
        generated_at=generated_at,
    )


def _record_runtime_health(
    runtime: Runtime,
    status: str,
    heartbeat_at: datetime,
    *,
    report: object | None = None,
    derived: object | None = None,
    successful: bool = False,
    error: BaseException | str | None = None,
    error_at: datetime | None = None,
) -> None:
    if runtime.health_connection is None:
        return
    from live_betting.health import record_health

    details = {
        "source": "worker",
        "run": _report_payload(report),
        "derived": _report_payload(derived),
    }
    error_text = None if error is None else str(error)
    record_health(
        runtime.health_connection,
        "strict_ingest_worker",
        status,
        heartbeat_at=heartbeat_at,
        success_at=heartbeat_at if successful else None,
        error_at=error_at,
        error=error_text,
        details=details,
    )


def _report_payload(value: object | None) -> object:
    if value is None:
        return None
    try:
        return asdict(value)  # type: ignore[arg-type]
    except TypeError:
        return value if isinstance(value, (dict, list, str, int, float, bool)) else str(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = resolve_data_paths(parser.parse_args(argv))
    if args.match is not None and args.active:
        parser.error("--match cannot be combined with --active")
    if args.scheduler_once and (
        args.once or args.event is not None or args.match is not None or args.reconcile
    ):
        parser.error("--scheduler-once cannot be combined with direct one-shot filters")
    try:
        with database_writer_authority(args.database):
            return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        logger.error("strict event ingestion failed: %s", error)
        return 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    raise SystemExit(main())
