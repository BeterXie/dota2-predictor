"""Read-only RayBet Dota 2 match and odds client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from curl_cffi import requests


BASE_URL = "https://cfinfo.365raylinks.com/v2"
SITE_URL = "https://www.ray086.com/"
DOTA2_GAME_ID = 151


@dataclass(frozen=True)
class RayBetHTTPResponse:
    """One successful HTTP response with its receipt-time transport identity."""

    payload: Any
    endpoint: str
    request_identity: str
    received_at: datetime
    http_status: int | None
    provider_code: int | None
    request_started_at: datetime | None = None
    transport_duration_ms: float | None = None


class RayBetProviderResponseError(RuntimeError):
    """A parsed HTTP response carries a provider-level failure code."""

    def __init__(self, message: str, response: RayBetHTTPResponse) -> None:
        super().__init__(message)
        self.raybet_response = response


class RayBetProviderPayloadError(RuntimeError):
    """A parsed provider response is JSON but not the required object envelope."""

    def __init__(self, message: str, response: RayBetHTTPResponse) -> None:
        super().__init__(message)
        self.raybet_response = response


def _annotate_transport_error(
    error: Exception,
    *,
    endpoint: str,
    request_identity: str,
    received_at: datetime,
    http_status: int | None,
    request_started_at: datetime,
    transport_duration_ms: float,
    transport_error: bool,
) -> None:
    try:
        error.raybet_endpoint = endpoint
        error.raybet_request_identity = request_identity
        error.raybet_received_at = received_at
        error.raybet_http_status = http_status
        error.raybet_request_started_at = request_started_at
        error.raybet_transport_duration_ms = transport_duration_ms
        error.raybet_transport_error = transport_error
    except (AttributeError, TypeError):
        pass


@dataclass(frozen=True)
class RayBetMapFinal:
    status: str
    winner_side: str | None
    score_winner_side: str | None
    market_winner_side: str | None
    settled_outcomes: tuple[tuple[str, bool], ...]
    reason: str
    evidence_ref: str
    observed_at: datetime | None
    audit_key: str | None = None
    transport_key: str | None = None
    response_state_hash: str | None = None
    response_artifact_hash: str | None = None

    def selection_won(self, odds_id: str) -> bool | None:
        return next(
            (won for stored_odds_id, won in self.settled_outcomes
             if stored_odds_id == odds_id),
            None,
        )

    def facts(self) -> dict[str, object]:
        return {
            "status": self.status,
            "winner_side": self.winner_side,
            "score_winner_side": self.score_winner_side,
            "market_winner_side": self.market_winner_side,
            "settled_outcomes": [
                {"odds_id": odds_id, "won": won}
                for odds_id, won in self.settled_outcomes
            ],
            "reason": self.reason,
        }


def _raybet_evidence_ref(payload: dict[str, Any], map_number: int) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"raybet:{payload.get('id')}:map:{map_number}:sha256:{digest}"


def _strict_result_flag(value: object) -> int | None:
    if type(value) is int and value in (0, 1):
        return value
    return None


def parse_raybet_map_final(
    payload: dict[str, Any],
    map_number: int,
    *,
    observed_at: datetime | None = None,
    expected_match_id: str | None = None,
    expected_team_ids: tuple[int, int] | None = None,
) -> RayBetMapFinal:
    """Normalize only explicit, settled RayBet map-winner evidence."""
    evidence_ref = _raybet_evidence_ref(payload, map_number)

    def final(
        status: str,
        reason: str,
        *,
        winner: str | None = None,
        score_winner: str | None = None,
        market_winner: str | None = None,
        outcomes: tuple[tuple[str, bool], ...] = (),
    ) -> RayBetMapFinal:
        return RayBetMapFinal(
            status,
            winner,
            score_winner,
            market_winner,
            outcomes,
            reason,
            evidence_ref,
            observed_at,
        )

    if type(map_number) is not int or map_number <= 0:
        raise ValueError("map_number must be positive")
    match_id = str(payload.get("id") or "")
    if not match_id.isdigit() or (
        expected_match_id is not None and match_id != expected_match_id
    ):
        return final("conflict", "raybet_match_identity_invalid")
    if type(payload.get("game_id")) is not int or payload["game_id"] != DOTA2_GAME_ID:
        return final("conflict", "raybet_game_identity_invalid")
    raw_teams = payload.get("team")
    if not isinstance(raw_teams, list):
        return final("pending", "raybet_team_identity_missing")
    teams = [row for row in raw_teams if isinstance(row, dict)]
    try:
        teams.sort(key=lambda row: int(row.get("pos") or 0))
    except (TypeError, ValueError):
        return final("conflict", "raybet_team_identity_invalid")
    if len(teams) != 2 or [int(row.get("pos") or 0) for row in teams] != [1, 2]:
        return final("conflict", "raybet_team_identity_invalid")

    side_by_team_id: dict[str, str] = {}
    ordered_team_ids: list[int] = []
    for index, team in enumerate(teams):
        raw_team_id = team.get("team_id")
        if type(raw_team_id) is not int or raw_team_id <= 0:
            return final("conflict", "raybet_team_identity_invalid")
        team_id = str(raw_team_id)
        if team_id in side_by_team_id:
            return final("conflict", "raybet_team_identity_invalid")
        ordered_team_ids.append(raw_team_id)
        side_by_team_id[team_id] = "team_one" if index == 0 else "team_two"
    if expected_team_ids is not None and tuple(ordered_team_ids) != expected_team_ids:
        return final("conflict", "raybet_team_identity_conflict")

    score_key = f"r{map_number}"
    score_values: dict[str, int | None] = {}
    score_present = False
    for index, team in enumerate(teams):
        score = team.get("score")
        value = score.get(score_key) if isinstance(score, dict) else None
        score_present = score_present or value is not None
        side = "team_one" if index == 0 else "team_two"
        score_values[side] = _strict_result_flag(value)

    score_winner: str | None = None
    if score_present:
        if any(value is None for value in score_values.values()):
            return final("conflict", "raybet_map_score_invalid")
        if set(score_values.values()) == {0, 1}:
            score_winner = next(
                side for side, value in score_values.items() if value == 1
            )
        elif set(score_values.values()) != {0}:
            return final("conflict", "raybet_map_score_invalid")

    raw_odds = payload.get("odds")
    odds = raw_odds if isinstance(raw_odds, list) else []
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in odds:
        if not isinstance(row, dict):
            continue
        group_name = str(
            row.get("group_short_name") or row.get("group_name") or ""
        ).strip().casefold()
        if (
            str(row.get("match_stage") or "").casefold() != f"r{map_number}"
            or group_name not in {"winner", "获胜者"}
            or str(row.get("tag") or "").casefold() != "win"
        ):
            continue
        group_id = str(row.get("odds_group_id") or "")
        if group_id:
            groups.setdefault(group_id, []).append(row)

    market_winners: set[str] = set()
    settled_outcomes: dict[str, bool] = {}
    for rows in groups.values():
        by_side: dict[str, dict[str, Any]] = {}
        duplicate_side = False
        unknown_side = False
        for row in rows:
            side = side_by_team_id.get(str(row.get("team_id") or ""))
            if side is None:
                unknown_side = True
                continue
            if side in by_side:
                duplicate_side = True
            by_side[side] = row
        if duplicate_side or unknown_side:
            return final(
                "conflict",
                "raybet_winner_market_invalid",
                score_winner=score_winner,
            )
        if set(by_side) != {"team_one", "team_two"}:
            continue
        flags = {
            side: _strict_result_flag(row.get("win"))
            for side, row in by_side.items()
        }
        if (
            any(str(row.get("status") or "") != "5" for row in by_side.values())
            or any(value is None for value in flags.values())
        ):
            continue
        if set(flags.values()) != {0, 1}:
            return final(
                "conflict",
                "raybet_winner_market_invalid",
                score_winner=score_winner,
            )
        winner = next(side for side, value in flags.items() if value == 1)
        market_winners.add(winner)
        for side, row in by_side.items():
            odds_id = str(row.get("odds_id") or row.get("id") or "")
            if not odds_id:
                return final(
                    "conflict",
                    "raybet_winner_market_invalid",
                    score_winner=score_winner,
                )
            won = bool(flags[side])
            if odds_id in settled_outcomes and settled_outcomes[odds_id] != won:
                return final(
                    "conflict",
                    "raybet_winner_market_invalid",
                    score_winner=score_winner,
                )
            settled_outcomes[odds_id] = won

    if not market_winners:
        return final(
            "pending",
            "raybet_winner_market_not_settled",
            score_winner=score_winner,
        )
    if len(market_winners) != 1:
        return final(
            "conflict",
            "raybet_winner_market_conflict",
            score_winner=score_winner,
        )
    market_winner = next(iter(market_winners))
    outcomes = tuple(sorted(settled_outcomes.items()))
    if score_winner is not None and score_winner != market_winner:
        return final(
            "conflict",
            "raybet_score_market_conflict",
            score_winner=score_winner,
            market_winner=market_winner,
            outcomes=outcomes,
        )
    return final(
        "confirmed",
        "raybet_final_confirmed",
        winner=market_winner,
        score_winner=score_winner,
        market_winner=market_winner,
        outcomes=outcomes,
    )


class RayBetClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        client: requests.Session | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self.timeout = timeout
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._owns_client = client is None
        self.client = client or requests.Session(impersonate="chrome120")
        self.client.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.ray086.com",
                "Referer": SITE_URL,
            }
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "RayBetClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _get_response(
        self, path: str, params: dict[str, Any] | None = None
    ) -> RayBetHTTPResponse:
        endpoint = f"{BASE_URL}/{path.lstrip('/')}"
        query = urlencode(
            sorted((str(key), str(value)) for key, value in (params or {}).items())
        )
        request_identity = f"{endpoint}?{query}" if query else endpoint
        request_started_at = self._wall_clock()
        monotonic_started_at = self._monotonic_clock()
        try:
            response = self.client.get(endpoint, params=params, timeout=self.timeout)
        except Exception as error:
            received_at = self._wall_clock()
            transport_duration_ms = max(
                0.0, (self._monotonic_clock() - monotonic_started_at) * 1000.0
            )
            _annotate_transport_error(
                error,
                endpoint=endpoint,
                request_identity=request_identity,
                received_at=received_at,
                http_status=None,
                request_started_at=request_started_at,
                transport_duration_ms=transport_duration_ms,
                transport_error=True,
            )
            raise
        received_at = self._wall_clock()
        transport_duration_ms = max(
            0.0, (self._monotonic_clock() - monotonic_started_at) * 1000.0
        )
        raw_status = getattr(response, "status_code", None)
        http_status = (
            int(raw_status)
            if isinstance(raw_status, int) and not isinstance(raw_status, bool)
            else None
        )
        try:
            payload = response.json()
        except Exception as parse_error:
            try:
                response.raise_for_status()
            except Exception as http_error:
                _annotate_transport_error(
                    http_error,
                    endpoint=endpoint,
                    request_identity=request_identity,
                    received_at=received_at,
                    http_status=http_status,
                    request_started_at=request_started_at,
                    transport_duration_ms=transport_duration_ms,
                    transport_error=False,
                )
                raise
            _annotate_transport_error(
                parse_error,
                endpoint=endpoint,
                request_identity=request_identity,
                received_at=received_at,
                http_status=http_status,
                request_started_at=request_started_at,
                transport_duration_ms=transport_duration_ms,
                transport_error=False,
            )
            raise
        raw_code = payload.get("code") if isinstance(payload, dict) else None
        provider_code = (
            int(raw_code)
            if isinstance(raw_code, int) and not isinstance(raw_code, bool)
            else None
        )
        receipt = RayBetHTTPResponse(
            payload=payload,
            endpoint=endpoint,
            request_identity=request_identity,
            received_at=received_at,
            http_status=http_status,
            provider_code=provider_code,
            request_started_at=request_started_at,
            transport_duration_ms=transport_duration_ms,
        )
        try:
            response.raise_for_status()
        except Exception as error:
            try:
                error.raybet_response = receipt
            except (AttributeError, TypeError):
                pass
            raise
        if not isinstance(payload, dict):
            raise RayBetProviderPayloadError(
                f"RayBet returned a non-object response for {path}", receipt
            )
        if raw_code != 200:
            raise RayBetProviderResponseError(
                f"RayBet returned code={raw_code} for {path}", receipt
            )
        return receipt

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._get_response(path, params).payload

    def games(self) -> list[dict[str, Any]]:
        return list(self._get("/game").get("result") or [])

    def match_page(self, match_type: int, page: int = 1) -> list[dict[str, Any]]:
        result = self.match_page_response(match_type, page).payload.get("result")
        return list(result or [])

    def match_page_response(
        self, match_type: int, page: int = 1
    ) -> RayBetHTTPResponse:
        return self._get_response(
            "/match", {"match_type": match_type, "page": page}
        )

    def matches(self, *, match_type: int, max_pages: int = 10) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            page_rows = self.match_page(match_type, page)
            if not page_rows:
                break
            for row in page_rows:
                if not isinstance(row, dict):
                    continue
                try:
                    game_id = int(row.get("game_id") or 0)
                except (TypeError, ValueError):
                    continue
                if game_id != DOTA2_GAME_ID:
                    continue
                match_id = str(row.get("id") or "")
                if not match_id.isdigit():
                    continue
                if match_id not in seen:
                    rows.append(row)
                    seen.add(match_id)
        return rows

    def live_matches(self, *, max_pages: int = 10) -> list[dict[str, Any]]:
        combined: dict[str, dict[str, Any]] = {}
        for match_type in (1, 2):
            for row in self.matches(match_type=match_type, max_pages=max_pages):
                combined[str(row.get("id"))] = row
        return sorted(combined.values(), key=lambda row: str(row.get("start_time") or ""))

    def completed_matches(self, *, max_pages: int = 10) -> list[dict[str, Any]]:
        """Return Dota 2 rows from RayBet's completed-match list (type 4).

        Completed rows are intentionally kept separate from ``live_matches``:
        they are used for final-result/odds evidence and must never become live
        strategy inputs merely because the provider still exposes the row.
        """
        return self.matches(match_type=4, max_pages=max_pages)

    def match_odds(self, match_id: int | str) -> dict[str, Any]:
        payload = self.match_odds_response(match_id).payload
        if not isinstance(payload.get("result"), dict):
            raise RuntimeError(f"RayBet odds missing result for match_id={match_id}")
        return payload

    def match_odds_response(self, match_id: int | str) -> RayBetHTTPResponse:
        response = self._get_response("/odds", {"match_id": str(match_id)})
        if not isinstance(response.payload.get("result"), dict):
            raise RuntimeError(f"RayBet odds missing result for match_id={match_id}")
        return response
