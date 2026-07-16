"""Localhost companion for sanitized Edge browser events."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from .browser_origin import SlidingWindowRateLimiter, is_extension_origin
from .browser_contract import BrowserEvent, find_forbidden_batch_key
from .browser_ingest import BrowserEventIngestor
from .storage import LiveBettingStore


ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = 8765
MAX_BODY_BYTES = 1024 * 1024
MAX_BATCH_EVENTS = 50
AUTH_HEADERS = "Content-Type, X-Dota-Extension-Version"
SUPPORTED_EXTENSION_VERSION = "0.1.0"
PROTOCOL_VERSION = 1
DEFAULT_EXTENSION_ORIGIN = os.environ.get(
    "DOTA2_BROWSER_EXTENSION_ORIGIN",
    "chrome-extension://gfccbmpmpgicjfleahjbokeifhjnemam",
)
EVENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CompanionConfig:
    database: Path = ROOT / "data" / "dota2.db"
    extension_origin: str = DEFAULT_EXTENSION_ORIGIN
    report_url: str | None = None

    def __post_init__(self) -> None:
        if not is_extension_origin(self.extension_origin):
            raise ValueError("extension_origin must be an exact chrome-extension origin")

    def safe_report_url(self) -> str | None:
        if not self.report_url:
            return None
        parsed = urlsplit(self.report_url)
        if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}:
            return self.report_url
        return None


class RuntimeStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.duplicates = 0
        self.rejections = 0

    def add(self, *, duplicates: int = 0, rejections: int = 0) -> None:
        with self._lock:
            self.duplicates += duplicates
            self.rejections += rejections

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self.duplicates, self.rejections


def _cors_origin(origin: str | None, config: CompanionConfig) -> str | None:
    return origin if origin == config.extension_origin else None


def _cors_headers(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": AUTH_HEADERS,
        "Access-Control-Max-Age": "300",
        "Vary": "Origin",
    }


def _error(code: str, status: int, detail: str = "request rejected") -> JSONResponse:
    return JSONResponse({"code": code, "detail": detail}, status_code=status)


def _direct_access_error(
    request: Request,
    config: CompanionConfig,
    limiter: SlidingWindowRateLimiter,
    *,
    bucket: str,
    limit: int,
) -> JSONResponse | None:
    origin = request.headers.get("origin")
    if origin != config.extension_origin:
        return _error("origin_not_allowed", 403)
    if request.headers.get("x-dota-extension-version") != SUPPORTED_EXTENSION_VERSION:
        return _error("unsupported_extension_version", 400)
    if not limiter.allow(bucket, origin, limit):
        return _error("rate_limited", 429)
    return None


def _safe_event_id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    value = item.get("event_id")
    return value if isinstance(value, str) and len(value) <= 128 else None


def _model_event(item: Any) -> BrowserEvent:
    encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return BrowserEvent.model_validate_json(encoded)


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _strict_json_loads(body: bytes | str) -> Any:
    return json.loads(body, parse_constant=_reject_json_constant)


async def _read_bounded_body(request: Request) -> bytes | None:
    """Read a request body without allowing an unbounded chunked upload."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    config: CompanionConfig | None = None,
    *,
    ingestor: BrowserEventIngestor | None = None,
    limiter: SlidingWindowRateLimiter | None = None,
    initialize_schema: bool = True,
) -> FastAPI:
    config = config or CompanionConfig()
    ingestor = ingestor or BrowserEventIngestor()
    limiter = limiter or SlidingWindowRateLimiter()
    stats = RuntimeStats()

    if initialize_schema:
        config.database.parent.mkdir(parents=True, exist_ok=True)
        with LiveBettingStore(config.database) as store:
            store.init_schema()

    app = FastAPI(
        title="Dota 2 Browser Companion",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config

    @app.middleware("http")
    async def cors_and_size_guard(request: Request, call_next):
        origin = request.headers.get("origin")
        allowed_origin = _cors_origin(origin, config)
        if request.method == "OPTIONS":
            if not allowed_origin:
                return Response(status_code=403)
            return Response(status_code=204, headers=_cors_headers(allowed_origin))
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
        json_post = request.method == "POST" and request.url.path in {"/v1/events", "/v1/status"}
        content_length = request.headers.get("content-length")
        if json_post and content_type != "application/json":
            response = _error("unsupported_media_type", 415)
        elif content_length and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
            response = _error("body_too_large", 413)
        else:
            response = await call_next(request)
        if allowed_origin:
            response.headers.update(_cors_headers(allowed_origin))
        return response

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"protocol_version": PROTOCOL_VERSION, "state": "ok"}

    @app.post("/v1/events")
    async def events(request: Request) -> Response:
        access_error = _direct_access_error(
            request, config, limiter, bucket="events", limit=120
        )
        if access_error is not None:
            return access_error
        body = await _read_bounded_body(request)
        if body is None:
            return _error("body_too_large", 413)
        try:
            raw_batch = _strict_json_loads(body)
        except (UnicodeDecodeError, ValueError):
            stats.add(rejections=1)
            return _error("invalid_json", 400)
        forbidden = find_forbidden_batch_key(raw_batch)
        if forbidden is not None:
            stats.add(rejections=1)
            return _error("forbidden_field", 400)
        if not isinstance(raw_batch, list) or not 1 <= len(raw_batch) <= MAX_BATCH_EVENTS:
            stats.add(rejections=1)
            return _error("invalid_batch", 400)

        results: list[dict[str, Any]] = []
        duplicate_count = 0
        rejection_count = 0
        try:
            with LiveBettingStore(config.database) as store:
                for item in raw_batch:
                    event_id = _safe_event_id(item)
                    try:
                        event = _model_event(item)
                    except (ValueError, TypeError):
                        rejection_count += 1
                        results.append({
                            "event_id": event_id,
                            "status": "rejected",
                            "reason": "invalid_event",
                        })
                        continue
                    if event.extension_version != SUPPORTED_EXTENSION_VERSION:
                        rejection_count += 1
                        results.append({
                            "event_id": event.event_id,
                            "status": "rejected",
                            "reason": "unsupported_extension_version",
                        })
                        continue
                    result = ingestor.ingest(store, event)
                    if result.outcome == "duplicate":
                        duplicate_count += 1
                    elif result.outcome == "rejected":
                        rejection_count += 1
                    results.append({
                        "event_id": result.event_id,
                        "status": result.outcome,
                        "processing_status": result.processing_status,
                        "reason": result.reason,
                    })
        except sqlite3.OperationalError:
            stats.add(rejections=1)
            return _error("database_unavailable", 503)
        stats.add(duplicates=duplicate_count, rejections=rejection_count)
        return JSONResponse(
            {"protocol_version": PROTOCOL_VERSION, "results": results}
        )

    @app.post("/v1/status")
    async def status(request: Request) -> Response:
        access_error = _direct_access_error(
            request, config, limiter, bucket="status", limit=60
        )
        if access_error is not None:
            return access_error
        body = await _read_bounded_body(request)
        if body is None:
            return _error("body_too_large", 413)
        try:
            status_body = _strict_json_loads(body)
        except (UnicodeDecodeError, ValueError):
            stats.add(rejections=1)
            return _error("invalid_json", 400)
        if type(status_body) is not dict or status_body != {}:
            stats.add(rejections=1)
            return _error("invalid_status_body", 400)
        with LiveBettingStore(config.database) as store:
            latest = store.connection.execute(
                "SELECT MAX(received_at) FROM browser_events"
            ).fetchone()[0]
            counts = {
                str(row[0]): int(row[1])
                for row in store.connection.execute(
                    "SELECT event_type, COUNT(*) FROM browser_events GROUP BY event_type"
                )
            }
            match_count = int(store.connection.execute(
                "SELECT COUNT(DISTINCT raybet_match_id) FROM browser_events "
                "WHERE raybet_match_id IS NOT NULL"
            ).fetchone()[0])
            shadow_active = store.connection.execute(
                """SELECT 1 FROM service_health
                    WHERE component='shadow'
                      AND status IN ('healthy', 'degraded')
                      AND last_heartbeat_at IS NOT NULL
                      AND (julianday('now') - julianday(last_heartbeat_at)) * 86400 <= 90
                    LIMIT 1"""
            ).fetchone() is not None
        duplicates, rejections = stats.snapshot()
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "latest_accepted_at": latest,
            "event_type_counts": counts,
            "duplicate_count": duplicates,
            "rejection_count": rejections,
            "known_dota_match_count": match_count,
            "database_health": "ok",
            "shadow_strategy_active": shadow_active,
        }
        report_url = config.safe_report_url()
        if report_url:
            payload["report_url"] = report_url
        return JSONResponse(payload)

    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=CompanionConfig().database)
    parser.add_argument("--extension-origin", default=DEFAULT_EXTENSION_ORIGIN)
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = CompanionConfig(database=args.database, extension_origin=args.extension_origin)
    if not getattr(args, "schema_prepared", False):
        with LiveBettingStore(config.database) as database:
            database.init_schema()
    if args.check_config:
        print(json.dumps({
            "host": HOST,
            "port": PORT,
            "database_health": "ok",
            "extension_origin": config.extension_origin,
        }))
        return 0
    print(f"Direct extension origin: {config.extension_origin}", flush=True)
    app = create_app(config, initialize_schema=False)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
