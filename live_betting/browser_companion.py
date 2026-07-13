"""Authenticated localhost companion for sanitized Edge browser events."""

from __future__ import annotations

import argparse
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .browser_auth import (
    AuthFailure,
    PairingManager,
    PairingStateStore,
    RequestAuthenticator,
    SlidingWindowRateLimiter,
    is_extension_origin,
)
from .browser_contract import BrowserEvent, find_forbidden_batch_key
from .browser_ingest import BrowserEventIngestor
from .storage import LiveBettingStore


ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = 8765
MAX_BODY_BYTES = 1024 * 1024
MAX_BATCH_EVENTS = 50
AUTH_HEADERS = (
    "Content-Type, X-Dota-Extension-Version, X-Dota-Timestamp, "
    "X-Dota-Nonce, X-Dota-Signature"
)
EVENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CompanionConfig:
    database: Path = ROOT / "data" / "dota2.db"
    pairing_state: Path | None = None
    report_url: str | None = None

    def safe_report_url(self) -> str | None:
        if not self.report_url:
            return None
        parsed = urlsplit(self.report_url)
        if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}:
            return self.report_url
        return None


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1, max_length=32)
    extension_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")


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


def _cors_origin(path: str, origin: str | None, pairing: PairingManager) -> str | None:
    if not origin:
        return None
    if path == "/v1/pair" and is_extension_origin(origin):
        return origin
    if pairing.state is not None and origin == pairing.state.origin:
        return origin
    return None


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


def _safe_event_id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    value = item.get("event_id")
    return value if isinstance(value, str) and len(value) <= 128 else None


def _model_event(item: Any) -> BrowserEvent:
    encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return BrowserEvent.model_validate_json(encoded)


def create_app(
    config: CompanionConfig | None = None,
    *,
    pairing: PairingManager | None = None,
    authenticator: RequestAuthenticator | None = None,
    ingestor: BrowserEventIngestor | None = None,
) -> FastAPI:
    config = config or CompanionConfig()
    pairing = pairing or PairingManager(PairingStateStore(config.pairing_state))
    authenticator = authenticator or RequestAuthenticator(pairing)
    ingestor = ingestor or BrowserEventIngestor()
    pair_limiter = SlidingWindowRateLimiter()
    stats = RuntimeStats()

    config.database.parent.mkdir(parents=True, exist_ok=True)
    with LiveBettingStore(config.database) as store:
        store.init_schema()

    app = FastAPI(title="Dota 2 Browser Companion", version="1.0.0", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.pairing = pairing

    @app.middleware("http")
    async def cors_and_size_guard(request: Request, call_next):
        origin = request.headers.get("origin")
        allowed_origin = _cors_origin(request.url.path, origin, pairing)
        if request.method == "OPTIONS":
            if not allowed_origin:
                return Response(status_code=403)
            return Response(status_code=204, headers=_cors_headers(allowed_origin))
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
        json_post = request.method == "POST" and request.url.path in {"/v1/pair", "/v1/events"}
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
        return {"protocol_version": 1, "state": "ok"}

    @app.post("/v1/pair")
    async def pair(request: Request) -> Response:
        origin = request.headers.get("origin")
        if not is_extension_origin(origin):
            return _error("invalid_pairing_request", 401, "pairing failed")
        if not pair_limiter.allow("pair", origin or "", 5):
            return _error("rate_limited", 429)
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return _error("body_too_large", 413)
        try:
            payload = PairRequest.model_validate_json(body)
            secret = pairing.pair(payload.code, origin or "")
        except (ValueError, AuthFailure):
            return _error("invalid_pairing_request", 401, "pairing failed")
        return JSONResponse({"protocol_version": 1, "secret": secret})

    @app.post("/v1/events")
    async def events(request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return _error("body_too_large", 413)
        origin = request.headers.get("origin")
        try:
            authenticator.authenticate(
                request.headers,
                origin=origin,
                method="POST",
                path="/v1/events",
                body=body,
                rate_bucket="events",
            )
        except AuthFailure as error:
            return _error(error.code, error.status_code, "authentication failed")
        try:
            raw_batch = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
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
        with LiveBettingStore(config.database) as store:
            store.init_schema()
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
                result = ingestor.ingest(store, event)
                if result.outcome == "duplicate":
                    duplicate_count += 1
                results.append({
                    "event_id": result.event_id,
                    "status": result.outcome,
                    "processing_status": result.processing_status,
                    "reason": result.reason,
                })
        stats.add(duplicates=duplicate_count, rejections=rejection_count)
        return JSONResponse({"results": results})

    @app.get("/v1/status")
    async def status(request: Request) -> Response:
        origin = request.headers.get("origin")
        try:
            authenticator.authenticate(
                request.headers,
                origin=origin,
                method="GET",
                path="/v1/status",
                body=b"",
                rate_bucket="status",
            )
        except AuthFailure as error:
            return _error(error.code, error.status_code, "authentication failed")
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
        duplicates, rejections = stats.snapshot()
        payload = {
            "protocol_version": 1,
            "latest_accepted_at": latest,
            "event_type_counts": counts,
            "duplicate_count": duplicates,
            "rejection_count": rejections,
            "known_dota_match_count": match_count,
            "database_health": "ok",
            "shadow_strategy_active": False,
        }
        report_url = config.safe_report_url()
        if report_url:
            payload["report_url"] = report_url
        return JSONResponse(payload)

    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=CompanionConfig().database)
    parser.add_argument("--pairing-state", type=Path)
    parser.add_argument("--reset-pairing", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = CompanionConfig(database=args.database, pairing_state=args.pairing_state)
    store = PairingStateStore(config.pairing_state)
    pairing = PairingManager(store)
    if args.reset_pairing:
        pairing.reset()
        print("Pairing state reset")
        return 0
    with LiveBettingStore(config.database) as database:
        database.init_schema()
    if args.check_config:
        print(json.dumps({"host": HOST, "port": PORT, "database_health": "ok"}))
        return 0
    if pairing.state is None:
        print(f"One-time pairing code (expires in 10 minutes): {pairing.issue_code()}")
    else:
        print("Companion is already paired")
    app = create_app(config, pairing=pairing)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
