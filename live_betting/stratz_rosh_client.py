"""Authenticated STRATZ transport for the dematus Rosh scoring flow."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from curl_cffi import requests as cffi_requests

from prematch.stratz_rosh import (
    build_player_highlights_query,
    build_rosh_match_context,
    build_rosh_match_query_request,
    build_rosh_query_requests,
    normalize_player_highlights_response,
    normalize_rosh_analysis,
    score_rosh_picks,
    score_rosh_lineups,
)


STRATZ_GRAPHQL_ENDPOINT = "https://api.stratz.com/graphql"
ROSH_FORMULA_VERSION = "dematus-rosh-0e1e6651dd932055dee69c4fb44435774f619793"
ROSH_SOURCE_NAME = "stratz"


class StratzRoshError(RuntimeError):
    """A sanitized transport or GraphQL failure."""


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
    score: FetchedRoshLineupScore | None
    minute_table: Sequence[Mapping[str, Any]]


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
    ) -> None:
        resolved = token.strip() if isinstance(token, str) else None
        self._token = resolved or resolve_stratz_api_token()
        if not self._token:
            raise StratzRoshError("STRATZ API token is not configured")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._post = post or cffi_requests.post
        self._clock = clock or (lambda: datetime.now(timezone.utc))

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
            batch = build_player_highlights_query(players)
            batch_response = self._request(batch, allow_partial=True)
            player_response_hashes["batch"] = _payload_hash(batch_response)
            highlights.update(normalize_player_highlights_response(batch, batch_response))
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
                except StratzRoshError:
                    slots[index]["fallback_reason"] = "player_stats_request_failed"
                    continue
                player_response_hashes[f"slot_{index}"] = _payload_hash(response)
                normalized = normalize_player_highlights_response(single, response)
                highlights[index] = normalized.get(0)
                if highlights[index] is None:
                    retry_policy = _player_error_policy(single, response).get(
                        0, "missing"
                    )
                    slots[index]["fallback_reason"] = {
                        "missing_or_anonymous": (
                            "player_missing_or_anonymous_in_stratz"
                        ),
                        "unsupported": "player_stats_request_failed",
                        "missing": "player_hero_stats_missing",
                    }.get(retry_policy, "player_stats_request_failed")

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

    def fetch_historical_match_score(
        self,
        match_id: int,
    ) -> FetchedHistoricalRoshScore:
        """Run the upstream GetMatchPicksBans historical Rosh entry point."""
        match_request = build_rosh_match_query_request(match_id)
        match_response = self._request(match_request)
        context = build_rosh_match_context(match_id, match_response)
        radiant_picks = context["radiant_picks"]
        dire_picks = context["dire_picks"]
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
        scored = score_rosh_picks(
            radiant_picks,
            dire_picks,
            normalize_rosh_analysis(responses),
        )
        raw_pure_score = scored.get("pure_lineup_score")
        minute_table = tuple(scored.get("pure_minute_table") or ())
        if raw_pure_score is None:
            return FetchedHistoricalRoshScore(
                context=context,
                score=None,
                minute_table=minute_table,
            )
        pure_score = _required_score(raw_pure_score, "pure")
        evidence: dict[str, Any] = {
            "source": ROSH_SOURCE_NAME,
            "source_week": week,
            "cache_week_start": rosh_cache_week_start(source_as_of),
            "formula_version": ROSH_FORMULA_VERSION,
            "historical_match_id": match_id,
            "response_hashes": response_hashes,
            "player_response_hashes": {},
            "player_slots": [],
            "pure_minute_table": list(minute_table),
            "score": {
                "pure_lineup_score": pure_score,
                "player_adjusted_lineup_score": None,
                "effective_lineup_score": pure_score,
                "scoring_mode": "pure",
                "player_coverage_count": 0,
            },
        }
        result = FetchedRoshLineupScore(
            pure_lineup_score=pure_score,
            player_adjusted_lineup_score=None,
            effective_lineup_score=pure_score,
            scoring_mode="pure",
            player_coverage_count=0,
            stake_multiplier=0.5,
            formula_version=ROSH_FORMULA_VERSION,
            source_name=ROSH_SOURCE_NAME,
            source_week=week,
            cache_week_start=rosh_cache_week_start(source_as_of),
            source_as_of=source_as_of,
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
                f"STRATZ request failed ({type(error).__name__})"
            ) from None
        status = getattr(response, "status_code", None)
        if status != 200:
            raise StratzRoshError(f"STRATZ request returned HTTP {status}")
        try:
            payload = response.json()
        except Exception as error:
            raise StratzRoshError(
                f"STRATZ returned invalid JSON ({type(error).__name__})"
            ) from None
        if not isinstance(payload, Mapping):
            raise StratzRoshError("STRATZ returned a non-object response")
        if payload.get("errors") and not allow_partial:
            raise StratzRoshError("STRATZ GraphQL request failed")
        if not isinstance(payload.get("data"), Mapping) and not (
            allow_partial and payload.get("errors")
        ):
            raise StratzRoshError("STRATZ GraphQL response has no data")
        return payload


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
