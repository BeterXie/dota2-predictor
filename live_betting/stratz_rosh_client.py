"""Exact-byte STRATZ transport for the frozen legacy pure-lineup scorer."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from curl_cffi import requests as cffi_requests

from prematch.stratz_rosh import build_rosh_query_requests


STRATZ_GRAPHQL_ENDPOINT = "https://api.stratz.com/graphql"
ROSH_FORMULA_VERSION = "dematus-rosh-0e1e6651dd932055dee69c4fb44435774f619793"
ROSH_SOURCE_NAME = "stratz"


class StratzRoshError(RuntimeError):
    """A sanitized transport failure with structured retry guidance."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
        category: str = "request_failure",
    ) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.retry_after_seconds = retry_after_seconds
        self.category = category


@dataclass(frozen=True)
class FetchedLegacyRoshBatch:
    request_bodies: Mapping[str, bytes] = field(repr=False)
    response_bodies: Mapping[str, bytes] = field(repr=False)
    collected_at: datetime


def resolve_stratz_api_token(
    environment: Mapping[str, str] | None = None,
) -> str | None:
    env = os.environ if environment is None else environment
    token = str(env.get("STRATZ_API_TOKEN", "")).strip()
    return token or None


class StratzRoshClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        endpoint: str = STRATZ_GRAPHQL_ENDPOINT,
        timeout_seconds: float = 30.0,
        post: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        resolved = token.strip() if isinstance(token, str) else None
        self._token = resolved or resolve_stratz_api_token()
        if not self._token:
            raise StratzRoshError("STRATZ API token is not configured")
        if endpoint != STRATZ_GRAPHQL_ENDPOINT:
            raise ValueError("STRATZ endpoint must match the frozen transport")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._post = post or cffi_requests.post
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop_requested = stop_requested or (lambda: False)
        self._sleeper = sleeper or time.sleep

    def fetch_legacy_lineup_batch(
        self,
        radiant_heroes: Sequence[int],
        dire_heroes: Sequence[int],
        *,
        statistics_cutoff: datetime,
    ) -> FetchedLegacyRoshBatch:
        radiant = _trusted_hero_slots(radiant_heroes, "radiant")
        dire = _trusted_hero_slots(dire_heroes, "dire")
        if set(radiant) & set(dire):
            raise ValueError("radiant and dire hero IDs must not overlap")
        cutoff = _utc(statistics_cutoff)
        requests = build_rosh_query_requests((*radiant, *dire), int(cutoff.timestamp()))
        request_bodies: dict[str, bytes] = {}
        response_bodies: dict[str, bytes] = {}
        for operation, request in requests.items():
            request_body = _legacy_request_body(request)
            request_bodies[operation] = request_body
            response_bodies[operation] = self._request_exact_bytes(
                request,
                request_body=request_body,
            )
        return FetchedLegacyRoshBatch(
            request_bodies=MappingProxyType(request_bodies),
            response_bodies=MappingProxyType(response_bodies),
            collected_at=_utc(self._clock()),
        )

    def _request_exact_bytes(
        self,
        request: Mapping[str, Any],
        *,
        request_body: bytes,
    ) -> bytes:
        query = request.get("query")
        if not isinstance(query, str) or not query.strip():
            raise StratzRoshError("STRATZ GraphQL query is empty")
        if self._stop_requested():
            raise StratzRoshError(
                "STRATZ request cancelled",
                category="request_cancelled",
            )
        try:
            response = self._post(
                self.endpoint,
                data=request_body,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                impersonate="chrome120",
                timeout=self.timeout_seconds,
            )
        except Exception as error:
            raise StratzRoshError(
                f"STRATZ request failed ({type(error).__name__})",
                retryable=True,
                category="network_failure",
            ) from None
        status = getattr(response, "status_code", None)
        if status != 200:
            retryable = status == 429 or (
                isinstance(status, int) and 500 <= status <= 599
            )
            raise StratzRoshError(
                f"STRATZ request returned HTTP {status}",
                retryable=retryable,
                retry_after_seconds=_retry_after_seconds(response),
                category="http_failure",
            )
        raw = getattr(response, "content", None)
        if not isinstance(raw, (bytes, bytearray)):
            raise StratzRoshError(
                "STRATZ response body is unavailable",
                category="invalid_response",
            )
        body = bytes(raw)
        try:
            payload = json.loads(
                body.decode("utf-8"),
                parse_constant=_reject_non_finite_json_constant,
            )
        except (UnicodeError, ValueError):
            raise StratzRoshError(
                "STRATZ returned invalid JSON",
                category="invalid_json",
            ) from None
        if not isinstance(payload, Mapping):
            raise StratzRoshError(
                "STRATZ returned a non-object response",
                category="invalid_response",
            )
        if payload.get("errors"):
            retryable, category = _graphql_retry_policy(payload["errors"])
            raise StratzRoshError(
                "STRATZ GraphQL request failed",
                retryable=retryable,
                retry_after_seconds=_retry_after_seconds(response),
                category=category,
            )
        if not isinstance(payload.get("data"), Mapping):
            raise StratzRoshError(
                "STRATZ GraphQL response has no data",
                category="invalid_response",
            )
        return body


def _legacy_request_body(request: Mapping[str, Any]) -> bytes:
    query = request.get("query")
    variables = request.get("variables")
    if not isinstance(query, str) or not query.strip():
        raise StratzRoshError("STRATZ GraphQL query is empty")
    if not isinstance(variables, Mapping):
        raise StratzRoshError("STRATZ GraphQL variables are invalid")
    try:
        return json.dumps(
            {
                "operationName": request.get("operation_name"),
                "query": query,
                "variables": dict(variables),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise StratzRoshError(
            "STRATZ legacy request serialization failed",
            category="profile_drift",
        ) from None


def _trusted_hero_slots(values: Sequence[int], side: str) -> tuple[int, ...]:
    heroes = tuple(values)
    if (
        len(heroes) != 5
        or any(type(value) is not int or value <= 0 for value in heroes)
        or len(set(heroes)) != 5
    ):
        raise ValueError(f"{side} heroes must contain five unique positive IDs")
    return heroes


def _reject_non_finite_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _retry_after_seconds(response: Any) -> float | None:
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    try:
        value = float(getter("Retry-After"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 <= value < float("inf"):
        return None
    return min(value, 60.0)


def _graphql_retry_policy(errors: Any) -> tuple[bool, str]:
    if not isinstance(errors, list) or not errors:
        return False, "graphql_failure"
    codes: list[str] = []
    for error in errors:
        extensions = error.get("extensions") if isinstance(error, Mapping) else None
        code = extensions.get("code") if isinstance(extensions, Mapping) else None
        if not isinstance(code, str) or not code.strip():
            return False, "graphql_failure"
        codes.append(code.strip().upper())
    if any(code in {"UNAUTHENTICATED", "FORBIDDEN"} for code in codes):
        return False, "graphql_auth_failure"
    retryable = all(code in {"RATE_LIMITED", "INTERNAL_SERVER_ERROR"} for code in codes)
    return retryable, "graphql_rate_limited" if retryable else "graphql_failure"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "FetchedLegacyRoshBatch",
    "ROSH_FORMULA_VERSION",
    "STRATZ_GRAPHQL_ENDPOINT",
    "StratzRoshClient",
    "StratzRoshError",
    "resolve_stratz_api_token",
]
