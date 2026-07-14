"""Narrow archive-oriented adapter around the existing OpenDota client."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from fetch.client import OpenDotaClient

from .raw_archive import canonical_json_bytes, sanitize_request_identity, schema_fingerprint


class MatchIdentityError(ValueError):
    """The response cannot be proven to describe the requested match."""


class OpenDotaClientProtocol(Protocol):
    async def get_leagues(self) -> list[dict[str, Any]]: ...

    async def get_match(self, match_id: int) -> dict[str, Any]: ...

    async def get_league_matches(self, league_id: int) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class OpenDotaResponse:
    endpoint: str
    request_identity: str
    received_at: datetime
    status_code: int
    content_type: str
    payload: dict[str, Any] | list[dict[str, Any]]
    canonical_json: bytes
    content_sha256: str
    schema_fingerprint: str

    @property
    def status(self) -> str:
        return "ok"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _positive_id(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _receipt_time(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("OpenDota receipt clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


class OpenDotaAdapter:
    """Reuse rate limiting/retries while retaining canonical response bytes."""

    def __init__(
        self,
        client: OpenDotaClientProtocol | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        rate_limit: int = 50,
    ) -> None:
        self._client = client if client is not None else OpenDotaClient(rate_limit=rate_limit)
        self._owns_client = client is None
        self._clock = clock

    async def fetch_match(self, match_id: int) -> OpenDotaResponse:
        match_id = _positive_id(match_id, "match_id")
        endpoint = f"/api/matches/{match_id}"
        payload = await self._client.get_match(match_id)
        if not isinstance(payload, dict):
            raise TypeError("OpenDota match response must be a JSON object")
        return self._response(endpoint, payload)

    async def fetch_leagues(self) -> OpenDotaResponse:
        endpoint = "/api/leagues"
        payload = await self._client.get_leagues()
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise TypeError("OpenDota league catalog response must be a JSON array")
        return self._response(endpoint, payload)

    async def fetch_league_matches(self, league_id: int) -> OpenDotaResponse:
        league_id = _positive_id(league_id, "league_id")
        endpoint = f"/api/leagues/{league_id}/matches"
        payload = await self._client.get_league_matches(league_id)
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise TypeError("OpenDota league response must be a JSON array of objects")
        return self._response(endpoint, payload)

    def _response(
        self,
        endpoint: str,
        payload: dict[str, Any] | list[dict[str, Any]],
    ) -> OpenDotaResponse:
        canonical = canonical_json_bytes(payload)
        return OpenDotaResponse(
            endpoint=endpoint,
            request_identity=sanitize_request_identity(endpoint),
            received_at=_receipt_time(self._clock),
            status_code=200,
            content_type="application/json",
            payload=payload,
            canonical_json=canonical,
            content_sha256=hashlib.sha256(canonical).hexdigest(),
            schema_fingerprint=schema_fingerprint(payload),
        )

    async def close(self) -> None:
        if not self._owns_client:
            return
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()

    async def __aenter__(self) -> "OpenDotaAdapter":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()
