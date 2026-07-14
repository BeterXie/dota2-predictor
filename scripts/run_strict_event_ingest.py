"""Incrementally archive approved Tier-1 Dota 2 completed matches."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
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
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "dota2.db")
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=ROOT / "data" / "raw" / "event_intelligence",
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
    return parser


@dataclass
class Runtime:
    ingestor: StrictEventIngestor
    scheduler: IngestScheduler
    close_callbacks: tuple[Callable[[], object], ...] = ()

    async def close(self) -> None:
        for callback in reversed(self.close_callbacks):
            result = callback()
            if asyncio.iscoroutine(result):
                await result


def build_default_runtime(args: argparse.Namespace) -> Runtime:
    """Late-bind concrete ports so importing this CLI has no database side effects."""
    try:
        from event_intelligence.opendota import OpenDotaAdapter
        from event_intelligence.raw_archive import RawArchive
        from event_intelligence.ingest_adapters import (
            RegistryIngestAdapter,
            SQLiteIngestAdapter,
        )
        from event_intelligence.registry import EventRegistry
        from event_intelligence.storage import IntelligenceStorage
        from fetch.db import Database
    except (ImportError, ModuleNotFoundError) as error:
        raise RuntimeError(
            "strict ingestion components are incomplete; install the event_intelligence "
            f"storage/registry/facts adapters ({error})"
        ) from error

    storage = IntelligenceStorage(args.database)
    try:
        storage.init_schema()
        legacy_database = Database(connection=storage.connection)
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
    return Runtime(ingestor, scheduler, callbacks)


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
        direct_one_shot = bool(
            args.once
            or args.event is not None
            or args.match is not None
            or args.reconcile
        )
        while True:
            now = datetime.now(timezone.utc)
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
            print(_report_json(report), flush=True)
            if direct_one_shot or args.scheduler_once:
                return 0
            await asyncio.sleep(args.interval)
    finally:
        await runtime.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.match is not None and args.active:
        parser.error("--match cannot be combined with --active")
    if args.scheduler_once and (
        args.once or args.event is not None or args.match is not None or args.reconcile
    ):
        parser.error("--scheduler-once cannot be combined with direct one-shot filters")
    try:
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
