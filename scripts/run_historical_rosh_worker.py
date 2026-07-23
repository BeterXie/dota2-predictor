"""Continuously backfill historical Rosh scores for approved Tier-1 maps."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_intelligence.historical_rosh_backfill import (  # noqa: E402
    HistoricalRoshBackfillReport,
    backfill_historical_rosh_scores,
    existing_historical_rosh_score_for_identity,
    historical_rosh_score_is_complete,
    load_existing_historical_rosh_score,
    load_formal_match_ids,
    persist_historical_rosh_score,
)
from event_intelligence.storage import IntelligenceStorage  # noqa: E402
from live_betting.health import record_health  # noqa: E402
from live_betting.service_coordination import (  # noqa: E402
    add_single_database_argument,
    database_writer_authority,
)
from live_betting.stratz_rosh_client import (  # noqa: E402
    StratzRoshClient,
    StratzRoshError,
    resolve_stratz_api_token,
)


logger = logging.getLogger(__name__)
COMPONENT = "historical_rosh_worker"
DEFAULT_BATCH_SIZE = 5
DEFAULT_IDLE_SECONDS = 60.0
DEFAULT_ERROR_BACKOFF_SECONDS = 300.0
DEFAULT_PARTIAL_RETRY_SECONDS = 604800.0
GLOBAL_BACKOFF_FAILURES = frozenset(
    {
        "StratzRoshError: graphql_internal_server_error",
        "StratzRoshError: graphql_auth_failure",
        "StratzRoshError: graphql_rate_limited",
        "StratzRoshError: http_auth_failure",
        "StratzRoshError: http_429",
        "StratzRoshError: http_5xx",
        "StratzRoshError: network_failure",
    }
)


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


HealthRecorder = Callable[..., None]
ClientFactory = Callable[[str], StratzRoshClient]
Clock = Callable[[], datetime]
Emitter = Callable[[Mapping[str, Any]], None]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(parser, required=True)
    parser.add_argument("--batch-size", type=_positive_int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--idle-seconds", type=_non_negative_float, default=DEFAULT_IDLE_SECONDS
    )
    parser.add_argument(
        "--error-backoff-seconds",
        type=_non_negative_float,
        default=DEFAULT_ERROR_BACKOFF_SECONDS,
    )
    parser.add_argument(
        "--partial-retry-seconds",
        type=_non_negative_float,
        default=DEFAULT_PARTIAL_RETRY_SECONDS,
    )
    parser.add_argument("--max-attempts", type=_positive_int, default=3)
    parser.add_argument(
        "--initial-backoff-seconds", type=_non_negative_float, default=1.0
    )
    parser.add_argument("--throttle-seconds", type=_non_negative_float, default=0.25)
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    return parser


def _open_read_connection(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _prepare_schema(database: Path) -> None:
    with database_writer_authority(database):
        with IntelligenceStorage(database) as storage:
            storage.init_schema()


def _record_worker_health(
    database: Path,
    status: str,
    heartbeat_at: datetime,
    *,
    successful: bool = False,
    error: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    with database_writer_authority(database):
        with IntelligenceStorage(database) as storage:
            record_health(
                storage.connection,
                COMPONENT,
                status,
                heartbeat_at=heartbeat_at,
                success_at=heartbeat_at if successful else None,
                error_at=(
                    heartbeat_at
                    if status in {"degraded", "unhealthy"}
                    else None
                ),
                error=error,
                details={"source": "worker", **dict(details or {})},
            )


def _persist_with_short_writer_authority(
    database: Path,
    _read_storage: object,
    match_id: int,
    identity: Mapping[str, Sequence[int]],
    score: object,
    created_at: datetime,
) -> bool:
    with database_writer_authority(database):
        with IntelligenceStorage(database) as writer:
            existing = existing_historical_rosh_score_for_identity(
                writer.connection,
                match_id=match_id,
                formula_version=score.formula_version,
                identity=identity,
            )
            if historical_rosh_score_is_complete(
                existing,
                include_current_player_adjustment=True,
            ):
                return False
            return persist_historical_rosh_score(
                writer,
                match_id,
                identity,
                score,
                created_at,
            )


def _fair_due_ids(
    match_ids: Sequence[int],
    *,
    cursor: int | None,
    settled: set[int],
    deferred_until: Mapping[int, datetime],
    now: datetime,
    limit: int,
) -> tuple[int, ...]:
    ordered = tuple(sorted(match_ids))
    if cursor is not None:
        ordered = tuple(value for value in ordered if value > cursor) + tuple(
            value for value in ordered if value <= cursor
        )
    return tuple(
        value
        for value in ordered
        if value not in settled
        and deferred_until.get(value, datetime.min.replace(tzinfo=timezone.utc)) <= now
    )[:limit]


def _partial_retry_at(
    existing: object | None,
    retry_seconds: float,
) -> datetime | None:
    if existing is None or getattr(existing, "player_coverage_count", None) == 10:
        return None
    created_at = getattr(existing, "created_at", None)
    if not isinstance(created_at, datetime):
        return None
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        return None
    return created_at.astimezone(timezone.utc) + timedelta(seconds=retry_seconds)


def _safe_client_error(error: Exception) -> str:
    if isinstance(error, StratzRoshError):
        return f"StratzRoshError: {error.category}"
    return type(error).__name__


def _stop_aware_wait(stopper: StopSignal, seconds: float) -> None:
    if stopper.wait(seconds):
        raise StratzRoshError(
            "STRATZ request cancelled",
            category="request_cancelled",
        )


def _default_emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(dict(payload), sort_keys=True, default=str), flush=True)


def run_worker(
    database: Path,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
    error_backoff_seconds: float = DEFAULT_ERROR_BACKOFF_SECONDS,
    partial_retry_seconds: float = DEFAULT_PARTIAL_RETRY_SECONDS,
    max_attempts: int = 3,
    initial_backoff_seconds: float = 1.0,
    throttle_seconds: float = 0.25,
    stop_event: StopSignal | None = None,
    clock: Clock | None = None,
    token_resolver: Callable[[], str | None] = resolve_stratz_api_token,
    client_factory: ClientFactory | None = None,
    health_recorder: HealthRecorder | None = None,
    read_connection_factory: Callable[[Path], sqlite3.Connection] = (
        _open_read_connection
    ),
    backfill: Callable[..., HistoricalRoshBackfillReport] = (
        backfill_historical_rosh_scores
    ),
    emit: Emitter = _default_emit,
) -> int:
    """Run until stopped, keeping all external waits outside writer authority."""
    database = database.resolve()
    stopper = stop_event or threading.Event()
    now = clock or (lambda: datetime.now(timezone.utc))
    health = health_recorder or (
        lambda status, heartbeat_at, **kwargs: _record_worker_health(
            database, status, heartbeat_at, **kwargs
        )
    )
    create_client = client_factory or (
        lambda token: StratzRoshClient(
            token=token,
            stop_requested=stopper.is_set,
        )
    )
    settled: set[int] = set()
    deferred_until: dict[int, datetime] = {}
    cursor: int | None = None
    client: StratzRoshClient | None = None

    started_at = now().astimezone(timezone.utc)
    health("starting", started_at, details={"batch_size": batch_size})
    try:
        while not stopper.is_set():
            if client is None:
                token = token_resolver()
                if not token:
                    heartbeat = now().astimezone(timezone.utc)
                    health(
                        "degraded",
                        heartbeat,
                        error="configuration_missing",
                        details={"reason": "stratz_token_missing"},
                    )
                    if stopper.wait(idle_seconds):
                        break
                    continue
                try:
                    client = create_client(token)
                except Exception as error:
                    failure = _safe_client_error(error)
                    heartbeat = now().astimezone(timezone.utc)
                    health(
                        "degraded",
                        heartbeat,
                        error=failure,
                        details={"reason": "client_initialization_failed"},
                    )
                    if stopper.wait(error_backoff_seconds):
                        break
                    continue

            connection = read_connection_factory(database)
            try:
                read_storage = type("ReadStorage", (), {"connection": connection})()
                heartbeat = now().astimezone(timezone.utc)
                match_ids = load_formal_match_ids(connection)
                selected = _fair_due_ids(
                    match_ids,
                    cursor=cursor,
                    settled=settled,
                    deferred_until=deferred_until,
                    now=heartbeat,
                    limit=batch_size,
                )
                if not selected:
                    health(
                        "healthy",
                        heartbeat,
                        successful=True,
                        details={
                            "run_status": "idle",
                            "formal_matches": len(match_ids),
                            "settled": len(settled),
                            "deferred": len(deferred_until),
                        },
                    )
                    wait_seconds = idle_seconds
                else:
                    wait_seconds = 0.0
                    requested = False
                    for match_id in selected:
                        if stopper.is_set():
                            break
                        cursor = match_id
                        checked_at = now().astimezone(timezone.utc)
                        try:
                            existing = load_existing_historical_rosh_score(
                                connection,
                                match_id,
                            )
                        except Exception:
                            existing = None
                        if historical_rosh_score_is_complete(
                            existing,
                            include_current_player_adjustment=True,
                        ):
                            settled.add(match_id)
                            deferred_until.pop(match_id, None)
                            continue
                        retry_at = _partial_retry_at(
                            existing,
                            partial_retry_seconds,
                        )
                        if retry_at is not None and retry_at > checked_at:
                            deferred_until[match_id] = retry_at
                            continue
                        if requested and throttle_seconds and stopper.wait(
                            throttle_seconds
                        ):
                            break
                        requested = True
                        health(
                            "healthy",
                            checked_at,
                            details={
                                "run_status": "processing",
                                "match_id": match_id,
                            },
                        )
                        report = backfill(
                            read_storage,
                            client,
                            match_id=match_id,
                            include_current_player_adjustment=True,
                            persist_score=lambda *args: (
                                _persist_with_short_writer_authority(
                                    database, *args
                                )
                            ),
                            max_attempts=max_attempts,
                            initial_backoff_seconds=initial_backoff_seconds,
                            throttle_seconds=0,
                            sleep=lambda seconds: _stop_aware_wait(
                                stopper,
                                seconds,
                            ),
                        )
                        finished_at = now().astimezone(timezone.utc)
                        payload = {
                            "match_id": match_id,
                            **asdict(report),
                        }
                        emit(payload)
                        if report.failed:
                            failure = report.failures[0].error
                            deferred_until[match_id] = finished_at + timedelta(
                                seconds=error_backoff_seconds
                            )
                            health(
                                "degraded",
                                finished_at,
                                error=failure,
                                details={"run_status": "failed", **payload},
                            )
                            if failure in GLOBAL_BACKOFF_FAILURES:
                                if failure in {
                                    "StratzRoshError: graphql_auth_failure",
                                    "StratzRoshError: http_auth_failure",
                                }:
                                    client = None
                                wait_seconds = error_backoff_seconds
                                break
                            continue

                        latest = load_existing_historical_rosh_score(
                            connection,
                            match_id,
                        )
                        if historical_rosh_score_is_complete(
                            latest,
                            include_current_player_adjustment=True,
                        ):
                            settled.add(match_id)
                            deferred_until.pop(match_id, None)
                            run_status = "settled"
                        else:
                            persisted_retry_at = _partial_retry_at(
                                latest,
                                partial_retry_seconds,
                            )
                            deferred_until[match_id] = max(
                                persisted_retry_at or finished_at,
                                finished_at
                                + timedelta(seconds=partial_retry_seconds),
                            )
                            run_status = "partial"
                        health(
                            "healthy",
                            finished_at,
                            successful=True,
                            details={"run_status": run_status, **payload},
                        )
            finally:
                connection.close()

            if wait_seconds and stopper.wait(wait_seconds):
                break
    finally:
        stopped_at = now().astimezone(timezone.utc)
        try:
            health("stopped", stopped_at, details={"reason": "stop_requested"})
        except Exception:
            logger.exception("failed to persist historical Rosh stopped health")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = args.database.resolve()
    if not args.schema_prepared:
        _prepare_schema(database)
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    return run_worker(
        database,
        batch_size=args.batch_size,
        idle_seconds=args.idle_seconds,
        error_backoff_seconds=args.error_backoff_seconds,
        partial_retry_seconds=args.partial_retry_seconds,
        max_attempts=args.max_attempts,
        initial_backoff_seconds=args.initial_backoff_seconds,
        throttle_seconds=args.throttle_seconds,
        stop_event=stop_event,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    raise SystemExit(main())
