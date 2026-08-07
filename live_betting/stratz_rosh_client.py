"""Authenticated STRATZ transport for the dematus Rosh scoring flow."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Any

from curl_cffi import requests as cffi_requests

from prematch import stratz_official_profile as official_profile
from prematch.stratz_official_profile import RoshRequestPlan
from prematch.stratz_rosh import (
    build_player_highlights_query,
    build_rosh_match_context,
    build_rosh_match_query_request,
    build_rosh_query_requests,
    normalize_player_highlights_response,
    normalize_rosh_analysis,
    position_ordered_rosh_heroes,
    score_rosh_picks,
    score_rosh_lineups,
)


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
        self.category = (
            category
            if category
            in {
                "network_failure",
                "http_auth_failure",
                "http_429",
                "http_5xx",
                "http_failure",
                "graphql_rate_limited",
                "graphql_internal_server_error",
                "graphql_auth_failure",
                "graphql_failure",
                "request_cancelled",
                "invalid_json",
                "invalid_response",
                "profile_drift",
                "request_failure",
            }
            else "request_failure"
        )


@dataclass(frozen=True)
class FetchedRoshLineupScore:
    pure_lineup_score: float
    player_adjusted_lineup_score: float | None
    effective_lineup_score: float
    scoring_mode: str
    player_coverage_count: int
    stake_multiplier: float
    formula_version: str
    source_name: str
    source_week: int
    cache_week_start: int
    source_as_of: datetime
    evidence: Mapping[str, Any]
    evidence_hash: str

    @property
    def stake_cap(self) -> float:
        """Maximum strategy stake; actual orders may choose any lower value."""
        return float(self.stake_multiplier)


@dataclass(frozen=True)
class FetchedHistoricalRoshScore:
    context: Mapping[str, Any]
    score: FetchedHistoricalRoshLineupScore | None
    minute_table: Sequence[Mapping[str, Any]]


@dataclass(frozen=True)
class FetchedHistoricalRoshLineupScore:
    """Retrospective score with explicitly current-only player correction."""

    pure_lineup_score: float
    current_player_adjusted_lineup_score: float | None
    effective_lineup_score: float
    scoring_mode: str
    player_coverage_count: int
    formula_version: str
    source_name: str
    source_week: int
    cache_week_start: int
    source_as_of: datetime
    player_stats_as_of: datetime | None
    evidence: Mapping[str, Any]
    evidence_hash: str


@dataclass(frozen=True)
class OfficialRoshBatch:
    """Exact official batch evidence plus its validated parsed responses."""

    request_body: bytes = field(repr=False)
    response_body: bytes = field(repr=False)
    responses: tuple[Mapping[str, Any], ...] = field(repr=False)
    collected_at: datetime
    diagnostics: Mapping[str, Any]


@dataclass(frozen=True)
class FetchedLegacyRoshBatch:
    """Exact transport bytes for the frozen legacy pure-lineup operations."""

    request_bodies: Mapping[str, bytes] = field(repr=False)
    response_bodies: Mapping[str, bytes] = field(repr=False)
    collected_at: datetime


def resolve_stratz_api_token(
    environment: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the production variable first and retain the legacy fallback."""
    env = os.environ if environment is None else environment
    primary = str(env.get("STRATZ_API_TOKEN", "")).strip()
    if primary:
        return primary
    legacy = str(env.get("STRATZ_TOKEN", "")).strip()
    return legacy or None


def rosh_cache_week_start(as_of: datetime) -> int:
    """Return the UTC natural-week key without changing STRATZ query input."""
    utc = _utc(as_of)
    monday = (utc - timedelta(days=utc.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return int(monday.timestamp())


def canonical_evidence_hash(evidence: Mapping[str, Any]) -> str:
    payload = json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
        official_max_attempts: int = 3,
        official_backoff_base_seconds: float = 1.0,
        official_backoff_cap_seconds: float = 8.0,
    ) -> None:
        resolved = token.strip() if isinstance(token, str) else None
        self._token = resolved or resolve_stratz_api_token()
        if not self._token:
            raise StratzRoshError("STRATZ API token is not configured")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._post = post or cffi_requests.post
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop_requested = stop_requested or (lambda: False)
        self._sleeper = sleeper or time.sleep
        if type(official_max_attempts) is not int or official_max_attempts < 1:
            raise ValueError("official_max_attempts must be a positive integer")
        if not _is_non_negative_finite(official_backoff_base_seconds):
            raise ValueError("official_backoff_base_seconds must be finite and non-negative")
        if not _is_non_negative_finite(official_backoff_cap_seconds):
            raise ValueError("official_backoff_cap_seconds must be finite and non-negative")
        self._official_max_attempts = official_max_attempts
        self._official_backoff_base_seconds = float(
            official_backoff_base_seconds
        )
        self._official_backoff_cap_seconds = float(
            official_backoff_cap_seconds
        )

    def fetch_official_batch(self, plan: RoshRequestPlan) -> OfficialRoshBatch:
        """Execute one canonical official-v2 GraphQL batch, failing closed."""
        _validate_official_plan_and_endpoint(plan, self.endpoint)
        request_body = _official_request_body(plan)
        retry_delays: list[float] = []

        for attempt in range(self._official_max_attempts):
            if self._stop_requested():
                raise StratzRoshError(
                    "STRATZ official request cancelled",
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
            except Exception:
                raise StratzRoshError(
                    "STRATZ official request failed",
                    retryable=True,
                    category="network_failure",
                ) from None

            status = getattr(response, "status_code", None)
            if status == 200:
                response_body, responses = _parse_official_response(
                    response,
                    len(plan.operations),
                )
                collected_at = _utc(self._clock())
                return OfficialRoshBatch(
                    request_body=request_body,
                    response_body=response_body,
                    responses=responses,
                    collected_at=collected_at,
                    diagnostics=MappingProxyType(
                        {
                            "endpoint": self.endpoint,
                            "request_hash": plan.request_hash,
                            "operation_names": tuple(
                                operation.operation_name
                                for operation in plan.operations
                            ),
                            "attempt_count": attempt + 1,
                            "retry_delays_seconds": tuple(retry_delays),
                            "timeout_seconds": self.timeout_seconds,
                        }
                    ),
                )

            retryable = status == 429 or (
                type(status) is int and 500 <= status <= 599
            )
            category = (
                "http_auth_failure"
                if status in {401, 403}
                else "http_429"
                if status == 429
                else "http_5xx"
                if type(status) is int and 500 <= status <= 599
                else "http_failure"
            )
            retry_after = _official_retry_after_seconds(response, self._clock)
            if retryable and attempt + 1 < self._official_max_attempts:
                backoff = min(
                    self._official_backoff_base_seconds * (2**attempt),
                    self._official_backoff_cap_seconds,
                )
                delay = max(backoff, retry_after or 0.0)
                retry_delays.append(delay)
                self._sleeper(delay)
                continue
            safe_status = status if type(status) is int else "unknown"
            raise StratzRoshError(
                f"STRATZ official request returned HTTP {safe_status}",
                retryable=retryable,
                retry_after_seconds=retry_after,
                category=category,
            )

        raise AssertionError("official retry loop exhausted unexpectedly")

    def fetch_lineup_score(
        self,
        radiant_heroes: Sequence[int],
        dire_heroes: Sequence[int],
        *,
        as_of: datetime,
        radiant_player_ids: Sequence[int | None] | None = None,
        dire_player_ids: Sequence[int | None] | None = None,
        player_identity_evidence: Mapping[str, Any] | None = None,
    ) -> FetchedRoshLineupScore:
        radiant = _trusted_hero_slots(radiant_heroes, "radiant")
        dire = _trusted_hero_slots(dire_heroes, "dire")
        query_started_at = _utc(as_of)
        week = int(query_started_at.timestamp())
        cache_week_start = rosh_cache_week_start(query_started_at)

        response_hashes: dict[str, str] = {}
        responses: dict[str, Mapping[str, Any]] = {}
        for key, request in build_rosh_query_requests((*radiant, *dire), week).items():
            response = self._request(request)
            responses[key] = response
            response_hashes[key] = _payload_hash(response)

        players, slots = _player_slots(
            radiant,
            dire,
            radiant_player_ids,
            dire_player_ids,
        )
        highlights: dict[int, Mapping[str, Any] | None] = {}
        selected_indices = [
            index for index, slot in enumerate(slots) if slot["selected"]
        ]
        identity_valid = False
        if len(selected_indices) == 10:
            try:
                normalized_identity_evidence = _normalize_player_identity_evidence(
                    player_identity_evidence,
                    radiant_heroes=radiant,
                    dire_heroes=dire,
                    radiant_player_ids=_optional_player_ids(
                        radiant_player_ids, "radiant"
                    ),
                    dire_player_ids=_optional_player_ids(dire_player_ids, "dire"),
                    available_at=query_started_at,
                )
                identity_valid = True
            except ValueError:
                normalized_identity_evidence = None
                for index in selected_indices:
                    slots[index]["fallback_reason"] = (
                        "player_identity_evidence_invalid"
                    )
        elif player_identity_evidence is not None:
            raise ValueError(
                "player identity evidence requires ten selected player IDs"
            )
        else:
            normalized_identity_evidence = None
            # Partial player lookups remain evidence-only and can never enable
            # adjusted mode; trusted identity metadata is mandatory at 10/10.
            identity_valid = True
        player_response_hashes: dict[str, str] = {}
        if selected_indices and identity_valid:
            highlights, player_response_hashes, _ = self._fetch_player_highlights(
                players,
                slots,
            )

        for index, slot in enumerate(slots):
            resolved = highlights.get(index) is not None
            slot["resolved"] = resolved
            if resolved:
                slot["fallback_reason"] = None

        coverage_count = sum(bool(slot["resolved"]) for slot in slots)
        use_player_adjustment = coverage_count == 10
        radiant_highlights = (
            [highlights[index] for index in range(5)]
            if use_player_adjustment
            else None
        )
        dire_highlights = (
            [highlights[index] for index in range(5, 10)]
            if use_player_adjustment
            else None
        )
        scored = score_rosh_lineups(
            radiant,
            dire,
            normalize_rosh_analysis(responses),
            radiant_player_highlights=radiant_highlights,
            dire_player_highlights=dire_highlights,
        )
        pure_score = _required_score(scored.get("pure_lineup_score"), "pure")
        adjusted_score = (
            _required_score(scored.get("player_adjusted_lineup_score"), "player-adjusted")
            if use_player_adjustment
            else None
        )
        mode = "player_adjusted" if use_player_adjustment else "pure"
        effective_score = adjusted_score if adjusted_score is not None else pure_score
        pure_minute_table = list(scored.get("pure_minute_table") or ())
        adjusted_minute_table = list(scored.get("minute_table") or ())
        source_as_of = max(_utc(self._clock()), query_started_at)
        evidence: dict[str, Any] = {
            "source": ROSH_SOURCE_NAME,
            "source_week": week,
            "source_as_of": source_as_of.isoformat(),
            "cache_week_start": cache_week_start,
            "formula_version": ROSH_FORMULA_VERSION,
            "response_hashes": response_hashes,
            "player_response_hashes": player_response_hashes,
            "player_slots": slots,
            "pure_minute_table": pure_minute_table,
            "score": {
                "pure_lineup_score": pure_score,
                "player_adjusted_lineup_score": adjusted_score,
                "effective_lineup_score": effective_score,
                "scoring_mode": mode,
                "player_coverage_count": coverage_count,
            },
        }
        if use_player_adjustment:
            evidence["minute_table"] = adjusted_minute_table
        if normalized_identity_evidence is not None:
            evidence["player_identity_evidence"] = normalized_identity_evidence
        return FetchedRoshLineupScore(
            pure_lineup_score=pure_score,
            player_adjusted_lineup_score=adjusted_score,
            effective_lineup_score=effective_score,
            scoring_mode=mode,
            player_coverage_count=coverage_count,
            stake_multiplier=1.0 if use_player_adjustment else 0.5,
            formula_version=ROSH_FORMULA_VERSION,
            source_name=ROSH_SOURCE_NAME,
            source_week=week,
            cache_week_start=cache_week_start,
            source_as_of=source_as_of,
            evidence=evidence,
            evidence_hash=canonical_evidence_hash(evidence),
        )

    def fetch_legacy_lineup_batch(
        self,
        radiant_heroes: Sequence[int],
        dire_heroes: Sequence[int],
        *,
        statistics_cutoff: datetime,
    ) -> FetchedLegacyRoshBatch:
        """Fetch the three frozen legacy operations without losing transport bytes."""

        radiant = _trusted_hero_slots(radiant_heroes, "radiant")
        dire = _trusted_hero_slots(dire_heroes, "dire")
        if set(radiant) & set(dire):
            raise ValueError("radiant and dire hero IDs must not overlap")
        cutoff = _utc(statistics_cutoff)
        requests = build_rosh_query_requests(
            (*radiant, *dire),
            int(cutoff.timestamp()),
        )
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

    def _fetch_player_highlights(
        self,
        players: Sequence[Mapping[str, Any]],
        slots: list[dict[str, Any]],
    ) -> tuple[
        dict[int, Mapping[str, Any] | None],
        dict[str, str],
        datetime | None,
    ]:
        selected_indices = [
            index for index, slot in enumerate(slots) if slot["selected"]
        ]
        if not selected_indices:
            return {}, {}, None

        batch = build_player_highlights_query(players)
        batch_response = self._request(batch, allow_partial=True)
        response_hashes = {"batch": _payload_hash(batch_response)}
        highlights = normalize_player_highlights_response(batch, batch_response)
        error_policy = _player_error_policy(batch, batch_response)

        # STRATZ may return HTTP 200 with per-alias GraphQL errors. Preserve
        # successful aliases and retry every selected unresolved slot alone.
        for index in selected_indices:
            if highlights.get(index) is not None:
                continue
            policy = error_policy.get(index, "missing")
            if policy == "missing_or_anonymous":
                slots[index]["fallback_reason"] = (
                    "player_missing_or_anonymous_in_stratz"
                )
                continue
            if policy == "unsupported":
                slots[index]["fallback_reason"] = "player_stats_request_failed"
                continue
            if policy == "missing":
                slots[index]["fallback_reason"] = "player_hero_stats_missing"
                continue
            single = build_player_highlights_query([players[index]])
            try:
                response = self._request(single, allow_partial=True)
            except StratzRoshError as error:
                if error.category == "request_cancelled":
                    raise
                slots[index]["fallback_reason"] = "player_stats_request_failed"
                continue
            response_hashes[f"slot_{index}"] = _payload_hash(response)
            normalized = normalize_player_highlights_response(single, response)
            highlights[index] = normalized.get(0)
            if highlights[index] is None:
                retry_policy = _player_error_policy(single, response).get(0, "missing")
                slots[index]["fallback_reason"] = {
                    "missing_or_anonymous": "player_missing_or_anonymous_in_stratz",
                    "unsupported": "player_stats_request_failed",
                    "missing": "player_hero_stats_missing",
                }.get(retry_policy, "player_stats_request_failed")
        return highlights, response_hashes, _utc(self._clock())

    def fetch_historical_match_score(
        self,
        match_id: int,
        *,
        include_current_player_adjustment: bool = False,
    ) -> FetchedHistoricalRoshScore:
        """Score a past match without presenting current player data as historical."""
        match_request = build_rosh_match_query_request(match_id)
        match_response = self._request(match_request)
        context = build_rosh_match_context(match_id, match_response)
        radiant_picks = context["radiant_picks"]
        dire_picks = context["dire_picks"]
        identity = historical_rosh_lineup_identity(context)
        week = int(context["week"])
        source_as_of = datetime.fromtimestamp(week, tz=timezone.utc)
        response_hashes = {"match": _payload_hash(match_response)}
        responses: dict[str, Mapping[str, Any]] = {}
        for key, request in build_rosh_query_requests(
            context["hero_ids"],
            week,
            str(context["bracket_basic"]),
        ).items():
            response = self._request(request)
            responses[key] = response
            response_hashes[key] = _payload_hash(response)

        slots: list[dict[str, Any]] = []
        highlights: dict[int, Mapping[str, Any] | None] = {}
        player_response_hashes: dict[str, str] = {}
        player_stats_as_of: datetime | None = None
        if include_current_player_adjustment:
            players, slots = _player_slots(
                identity["radiant_hero_ids"],
                identity["dire_hero_ids"],
                identity["radiant_player_ids"],
                identity["dire_player_ids"],
            )
            highlights, player_response_hashes, player_stats_as_of = (
                self._fetch_player_highlights(players, slots)
            )
            for index, slot in enumerate(slots):
                resolved = highlights.get(index) is not None
                slot["resolved"] = resolved
                if resolved:
                    slot["fallback_reason"] = None

        coverage_count = sum(bool(slot["resolved"]) for slot in slots)
        use_current_player_adjustment = coverage_count == 10
        scored = score_rosh_picks(
            radiant_picks,
            dire_picks,
            normalize_rosh_analysis(responses),
            radiant_player_highlights=(
                [highlights[index] for index in range(5)]
                if use_current_player_adjustment
                else None
            ),
            dire_player_highlights=(
                [highlights[index] for index in range(5, 10)]
                if use_current_player_adjustment
                else None
            ),
            player_slot_statuses=slots or None,
        )
        raw_pure_score = scored.get("pure_lineup_score")
        pure_minute_table = tuple(scored.get("pure_minute_table") or ())
        if raw_pure_score is None:
            return FetchedHistoricalRoshScore(
                context=context,
                score=None,
                minute_table=pure_minute_table,
            )
        pure_score = _required_score(raw_pure_score, "pure")
        current_adjusted_score = (
            _required_score(
                scored.get("player_adjusted_lineup_score"),
                "current player-adjusted",
            )
            if use_current_player_adjustment
            else None
        )
        mode = (
            "current_player_adjusted"
            if current_adjusted_score is not None
            else "pure"
        )
        effective_score = (
            current_adjusted_score
            if current_adjusted_score is not None
            else pure_score
        )
        minute_table = tuple(
            scored.get("minute_table")
            if use_current_player_adjustment
            else pure_minute_table
        )
        evidence: dict[str, Any] = {
            "source": ROSH_SOURCE_NAME,
            "source_week": week,
            "source_as_of": source_as_of.isoformat(),
            "cache_week_start": rosh_cache_week_start(source_as_of),
            "formula_version": ROSH_FORMULA_VERSION,
            "historical_match_id": match_id,
            "response_hashes": response_hashes,
            "player_response_hashes": player_response_hashes,
            "player_slots": slots,
            "player_stats_as_of": (
                player_stats_as_of.isoformat()
                if player_stats_as_of is not None
                else None
            ),
            "retrospective": True,
            "current_player_adjustment_only": True,
            "backtest_eligible": False,
            "pure_minute_table": list(pure_minute_table),
            "score": {
                "pure_lineup_score": pure_score,
                "current_player_adjusted_lineup_score": current_adjusted_score,
                "effective_lineup_score": effective_score,
                "scoring_mode": mode,
                "player_coverage_count": coverage_count,
            },
        }
        if use_current_player_adjustment:
            evidence["minute_table"] = list(minute_table)
        result = FetchedHistoricalRoshLineupScore(
            pure_lineup_score=pure_score,
            current_player_adjusted_lineup_score=current_adjusted_score,
            effective_lineup_score=effective_score,
            scoring_mode=mode,
            player_coverage_count=coverage_count,
            formula_version=ROSH_FORMULA_VERSION,
            source_name=ROSH_SOURCE_NAME,
            source_week=week,
            cache_week_start=rosh_cache_week_start(source_as_of),
            source_as_of=source_as_of,
            player_stats_as_of=player_stats_as_of,
            evidence=evidence,
            evidence_hash=canonical_evidence_hash(evidence),
        )
        return FetchedHistoricalRoshScore(
            context=context,
            score=result,
            minute_table=minute_table,
        )

    def _request(
        self,
        request: Mapping[str, Any],
        *,
        allow_partial: bool = False,
    ) -> Mapping[str, Any]:
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
                json={
                    "operationName": request.get("operation_name"),
                    "query": query,
                    "variables": dict(request.get("variables", {})),
                },
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
            retry_after = _retry_after_seconds(response)
            retryable = status == 429 or (
                type(status) is int and 500 <= status <= 599
            )
            category = (
                "http_auth_failure"
                if status in {401, 403}
                else "http_429"
                if status == 429
                else "http_5xx"
                if type(status) is int and 500 <= status <= 599
                else "http_failure"
            )
            raise StratzRoshError(
                f"STRATZ request returned HTTP {status}",
                retryable=retryable,
                retry_after_seconds=retry_after,
                category=category,
            )
        try:
            payload = response.json()
        except Exception as error:
            raise StratzRoshError(
                f"STRATZ returned invalid JSON ({type(error).__name__})",
                category="invalid_json",
            ) from None
        if not isinstance(payload, Mapping):
            raise StratzRoshError(
                "STRATZ returned a non-object response",
                category="invalid_response",
            )
        if payload.get("errors"):
            retryable, category = _graphql_retry_policy(payload.get("errors"))
            if category == "graphql_auth_failure" or not allow_partial:
                raise StratzRoshError(
                    "STRATZ GraphQL request failed",
                    retryable=retryable,
                    retry_after_seconds=_retry_after_seconds(response),
                    category=category,
                )
        if not isinstance(payload.get("data"), Mapping) and not (
            allow_partial and payload.get("errors")
        ):
            raise StratzRoshError(
                "STRATZ GraphQL response has no data",
                category="invalid_response",
            )
        return payload

    def _request_exact_bytes(
        self,
        request: Mapping[str, Any],
        *,
        request_body: bytes,
    ) -> bytes:
        """Execute one legacy request and return the exact validated response bytes."""

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
            retry_after = _retry_after_seconds(response)
            retryable = status == 429 or (
                type(status) is int and 500 <= status <= 599
            )
            category = (
                "http_auth_failure"
                if status in {401, 403}
                else "http_429"
                if status == 429
                else "http_5xx"
                if type(status) is int and 500 <= status <= 599
                else "http_failure"
            )
            raise StratzRoshError(
                f"STRATZ request returned HTTP {status}",
                retryable=retryable,
                retry_after_seconds=retry_after,
                category=category,
            )
        raw = getattr(response, "content", None)
        if not isinstance(raw, (bytes, bytearray)):
            raise StratzRoshError(
                "STRATZ response body is unavailable",
                category="invalid_response",
            )
        response_body = bytes(raw)
        try:
            payload = json.loads(
                response_body.decode("utf-8"),
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
            retryable, category = _graphql_retry_policy(payload.get("errors"))
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
        return response_body


def _validate_official_plan_and_endpoint(
    plan: RoshRequestPlan,
    endpoint: str,
) -> None:
    try:
        official_profile.validate_canonical_request_plan(plan)
    except Exception:
        raise StratzRoshError(
            "STRATZ official request plan failed canonical validation",
            category="profile_drift",
        ) from None
    registered_endpoint = official_profile.ENDPOINT
    if (
        registered_endpoint != STRATZ_GRAPHQL_ENDPOINT
        or not registered_endpoint.startswith("https://")
        or endpoint != registered_endpoint
    ):
        raise StratzRoshError(
            "STRATZ official endpoint failed profile validation",
            category="profile_drift",
        )


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


def _official_request_body(plan: RoshRequestPlan) -> bytes:
    payload = [
        {
            "operationName": operation.operation_name,
            "variables": _transport_thaw(operation.variables),
            "query": operation.query,
        }
        for operation in plan.operations
    ]
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise StratzRoshError(
            "STRATZ official request serialization failed",
            category="profile_drift",
        ) from None


def _parse_official_response(
    response: Any,
    expected_count: int,
) -> tuple[bytes, tuple[Mapping[str, Any], ...]]:
    raw = getattr(response, "content", None)
    if not isinstance(raw, (bytes, bytearray)):
        raise StratzRoshError(
            "STRATZ official response body is unavailable",
            category="invalid_response",
        )
    response_body = bytes(raw)
    try:
        payload = json.loads(
            response_body.decode("utf-8"),
            parse_constant=_reject_non_finite_json_constant,
        )
    except (UnicodeError, ValueError):
        raise StratzRoshError(
            "STRATZ official response is invalid JSON",
            category="invalid_json",
        ) from None
    if not isinstance(payload, list):
        raise StratzRoshError(
            "STRATZ official response is not a batch",
            category="invalid_response",
        )
    if len(payload) != expected_count:
        raise StratzRoshError(
            "STRATZ official response count does not match the request plan",
            category="invalid_response",
        )

    errors: list[Any] = []
    missing_data = False
    for item in payload:
        if not isinstance(item, Mapping):
            raise StratzRoshError(
                "STRATZ official batch item is invalid",
                category="invalid_response",
            )
        item_errors = item.get("errors")
        if item_errors is not None:
            if not isinstance(item_errors, list):
                raise StratzRoshError(
                    "STRATZ official GraphQL errors are invalid",
                    category="invalid_response",
                )
            errors.extend(item_errors)
        if not isinstance(item.get("data"), Mapping):
            missing_data = True
    if errors:
        retryable, category = _graphql_retry_policy(errors)
        raise StratzRoshError(
            "STRATZ official GraphQL batch failed",
            retryable=retryable,
            category=category,
        )
    if missing_data:
        raise StratzRoshError(
            "STRATZ official batch item has no data",
            category="invalid_response",
        )
    return response_body, tuple(_transport_freeze(item) for item in payload)


def _reject_non_finite_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constants are forbidden")


def _transport_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _transport_thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_transport_thaw(item) for item in value]
    return value


def _transport_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _transport_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_transport_freeze(item) for item in value)
    return value


def _official_retry_after_seconds(
    response: Any,
    clock: Callable[[], datetime],
) -> float | None:
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    raw = getter("Retry-After")
    try:
        seconds = float(raw)
    except (TypeError, ValueError, OverflowError):
        if not isinstance(raw, str):
            return None
        try:
            retry_at = parsedate_to_datetime(raw)
            seconds = (_utc(retry_at) - _utc(clock())).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not _is_non_negative_finite(seconds):
        return None
    return min(float(seconds), 60.0)


def _is_non_negative_finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return number == number and number >= 0 and number != float("inf")


def historical_rosh_lineup_identity(
    context: Mapping[str, Any],
) -> dict[str, tuple[int | None, ...] | tuple[int, ...]]:
    """Return position-ordered STRATZ heroes and available player identities."""
    radiant_picks = context.get("radiant_picks")
    dire_picks = context.get("dire_picks")
    if not isinstance(radiant_picks, Sequence) or isinstance(radiant_picks, (str, bytes)):
        raise ValueError("historical radiant picks are unavailable")
    if not isinstance(dire_picks, Sequence) or isinstance(dire_picks, (str, bytes)):
        raise ValueError("historical dire picks are unavailable")
    radiant_heroes = position_ordered_rosh_heroes(radiant_picks)
    dire_heroes = position_ordered_rosh_heroes(dire_picks)

    match = context.get("match")
    raw_players = match.get("players") if isinstance(match, Mapping) else None
    players = raw_players if isinstance(raw_players, list) else []
    by_hero: dict[int, list[Mapping[str, Any]]] = {}
    for player in players:
        if not isinstance(player, Mapping):
            continue
        hero_id = player.get("heroId")
        if type(hero_id) is int and hero_id > 0:
            by_hero.setdefault(hero_id, []).append(player)

    def player_ids(hero_ids: Sequence[int]) -> tuple[int | None, ...]:
        result: list[int | None] = []
        for hero_id in hero_ids:
            candidates = by_hero.get(hero_id, [])
            candidate = candidates[0] if len(candidates) == 1 else None
            account_id = (
                candidate.get("steamAccountId")
                if isinstance(candidate, Mapping)
                else None
            )
            result.append(
                account_id
                if type(account_id) is int and account_id > 0
                else None
            )
        return tuple(result)

    radiant_players = player_ids(radiant_heroes)
    dire_players = player_ids(dire_heroes)
    all_players = (*radiant_players, *dire_players)
    duplicate_ids = {
        player_id
        for player_id in all_players
        if player_id is not None and all_players.count(player_id) > 1
    }
    if duplicate_ids:
        radiant_players = tuple(
            None if player_id in duplicate_ids else player_id
            for player_id in radiant_players
        )
        dire_players = tuple(
            None if player_id in duplicate_ids else player_id
            for player_id in dire_players
        )
    return {
        "radiant_hero_ids": radiant_heroes,
        "dire_hero_ids": dire_heroes,
        "radiant_player_ids": radiant_players,
        "dire_player_ids": dire_players,
    }


def _player_slots(
    radiant_heroes: Sequence[int],
    dire_heroes: Sequence[int],
    radiant_player_ids: Sequence[int | None] | None,
    dire_player_ids: Sequence[int | None] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    radiant_players = _optional_player_ids(radiant_player_ids, "radiant")
    dire_players = _optional_player_ids(dire_player_ids, "dire")
    players: list[dict[str, Any]] = []
    slots: list[dict[str, Any]] = []
    for index, (side, hero_id, player_id) in enumerate(
        [
            *(("radiant", hero, radiant_players[i]) for i, hero in enumerate(radiant_heroes)),
            *(("dire", hero, dire_players[i]) for i, hero in enumerate(dire_heroes)),
        ]
    ):
        selected = type(player_id) is int and player_id > 0
        players.append(
            {
                "steamAccountId": player_id if selected else None,
                "heroId": hero_id,
                "isAnonymous": False,
            }
        )
        slots.append(
            {
                "slot": index,
                "side": side,
                "position": (index % 5) + 1,
                "hero_id": hero_id,
                "steam_account_id": player_id if selected else None,
                "selected": selected,
                "resolved": False,
                "fallback_reason": None if selected else "player_identity_unavailable",
            }
        )
    return players, slots


def _trusted_hero_slots(values: Sequence[int], side: str) -> tuple[int, ...]:
    heroes = tuple(values)
    if len(heroes) != 5 or any(type(hero_id) is not int or hero_id <= 0 for hero_id in heroes):
        raise ValueError(f"{side} must contain five positive hero IDs")
    if len(set(heroes)) != 5:
        raise ValueError(f"{side} hero IDs must be unique")
    return heroes


def _optional_player_ids(
    values: Sequence[int | None] | None,
    side: str,
) -> tuple[int | None, ...]:
    if values is None:
        return (None,) * 5
    result = tuple(values)
    if len(result) != 5:
        raise ValueError(f"{side} player IDs must contain five slots")
    if any(value is not None and (type(value) is not int or value <= 0) for value in result):
        raise ValueError(f"{side} player IDs must be positive integers or null")
    return result


def _required_score(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StratzRoshError(f"STRATZ Rosh {label} score is unavailable")
    result = float(value)
    if result != result or result in {float("inf"), float("-inf")}:
        raise StratzRoshError(f"STRATZ Rosh {label} score is invalid")
    return result


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _retry_after_seconds(response: Any) -> float | None:
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    raw = getter("Retry-After")
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if value != value or value < 0 or value == float("inf"):
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
    auth_codes = {"UNAUTHENTICATED", "FORBIDDEN", "PERMISSION_DENIED"}
    if any(code in auth_codes for code in codes):
        return False, "graphql_auth_failure"
    retryable_codes = {"RATE_LIMITED", "INTERNAL_SERVER_ERROR"}
    if any(code not in retryable_codes for code in codes):
        return False, "graphql_failure"
    category = (
        "graphql_rate_limited"
        if "RATE_LIMITED" in codes
        else "graphql_internal_server_error"
    )
    return True, category


def _player_error_policy(
    request: Mapping[str, Any],
    response: Mapping[str, Any],
) -> dict[int, str]:
    """Classify unresolved aliases according to upstream STRATZ behavior."""
    aliases = {
        str(alias): int(index)
        for alias, index in request.get("aliases", {}).items()
    }
    errors = response.get("errors")
    if not isinstance(errors, list):
        return {}
    policies: dict[int, str] = {}
    first_message = ""
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        message = str(error.get("message") or "").lower()
        if not first_message and message:
            first_message = message
        path = error.get("path")
        alias = next(
            (
                str(part)
                for part in path
                if str(part) in aliases
            ),
            None,
        ) if isinstance(path, list) else None
        if alias is None:
            continue
        policies[aliases[alias]] = _graphql_player_error_policy(message)
    if first_message:
        generic_policy = _graphql_player_error_policy(first_message)
        for index in aliases.values():
            policies.setdefault(index, generic_policy)
    return policies


def _normalize_player_identity_evidence(
    value: Mapping[str, Any] | None,
    *,
    radiant_heroes: Sequence[int],
    dire_heroes: Sequence[int],
    radiant_player_ids: Sequence[int | None],
    dire_player_ids: Sequence[int | None],
    available_at: datetime,
) -> dict[str, Any]:
    from .live_player_identity import canonical_live_player_identity_evidence_hash

    if not isinstance(value, Mapping):
        raise ValueError("player identity evidence is required")
    source_name = value.get("source_name")
    source_match_id = value.get("source_match_id")
    fetched_at = value.get("fetched_at")
    evidence_hash = value.get("evidence_hash")
    radiant_team_id = value.get("radiant_team_id")
    dire_team_id = value.get("dire_team_id")
    if not isinstance(source_name, str) or not source_name.strip():
        raise ValueError("player identity source_name is required")
    if type(source_match_id) is not int or source_match_id <= 0:
        raise ValueError("player identity source_match_id must be positive")
    if not isinstance(fetched_at, datetime):
        raise ValueError("player identity fetched_at must be a datetime")
    fetched_at_utc = _utc(fetched_at)
    if fetched_at_utc > _utc(available_at):
        raise ValueError("player identity evidence cannot be from the future")
    if (
        not isinstance(evidence_hash, str)
        or len(evidence_hash) != 64
        or any(character not in "0123456789abcdef" for character in evidence_hash)
    ):
        raise ValueError("player identity evidence_hash must be lowercase SHA-256")
    if any(player_id is None for player_id in (*radiant_player_ids, *dire_player_ids)):
        raise ValueError("player identity evidence requires ten player IDs")
    calculated_hash = canonical_live_player_identity_evidence_hash(
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
        radiant_hero_ids=radiant_heroes,
        dire_hero_ids=dire_heroes,
        radiant_player_ids=[int(value) for value in radiant_player_ids],
        dire_player_ids=[int(value) for value in dire_player_ids],
        source_match_id=source_match_id,
        source_name=source_name,
        fetched_at=fetched_at_utc,
    )
    if calculated_hash != evidence_hash:
        raise ValueError("player identity evidence hash does not match slots")
    return {
        "source_name": source_name.strip(),
        "source_match_id": source_match_id,
        "radiant_team_id": radiant_team_id,
        "dire_team_id": dire_team_id,
        "fetched_at": fetched_at_utc.isoformat(),
        "evidence_hash": evidence_hash,
    }


def _graphql_player_error_policy(message: str) -> str:
    normalized = message.lower()
    if "player id is missing or anonymous" in normalized:
        return "missing_or_anonymous"
    if "unsupported value" in normalized:
        return "unsupported"
    return "retry"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return value.astimezone(timezone.utc)
