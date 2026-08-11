"""Optional STRATZ enrichment for an already verified Valve match ID."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

import httpx


STRATZ_GRAPHQL_ENDPOINT = "https://api.stratz.com/graphql"
_POSITION_PATTERN = re.compile(r"^POSITION_([1-5])$")
_MATCH_ENRICHMENT_QUERY = """\
query MatchDetailEnrichment($matchId: Long!) {
  match(id: $matchId) {
    id
    players {
      steamAccountId
      heroId
      position
    }
  }
}
"""


class StratzDetailError(RuntimeError):
    """A sanitized optional-enrichment failure."""


def resolve_stratz_detail_token(
    environment: Mapping[str, str] | None = None,
) -> str | None:
    source = os.environ if environment is None else environment
    value = str(source.get("STRATZ_API_TOKEN", "")).strip()
    return value or None


class StratzMatchDetailClient:
    def __init__(
        self,
        token: str,
        *,
        client: Any | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        resolved = token.strip()
        if not resolved:
            raise ValueError("STRATZ detail token is required")
        self._token = resolved
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def get_match(self, match_id: int) -> dict[str, Any]:
        if type(match_id) is not int or match_id <= 0:
            raise ValueError("match_id must be a positive integer")
        try:
            response = await self._client.post(
                STRATZ_GRAPHQL_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                json={
                    "operationName": "MatchDetailEnrichment",
                    "query": _MATCH_ENRICHMENT_QUERY,
                    "variables": {"matchId": match_id},
                },
            )
        except Exception as error:
            raise StratzDetailError(
                f"STRATZ detail request failed ({type(error).__name__})"
            ) from None
        if response.status_code != 200:
            raise StratzDetailError(
                f"STRATZ detail request returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise StratzDetailError("STRATZ detail response is invalid JSON") from None
        _validate_match_payload(payload, match_id)
        return payload

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def stratz_player_positions(
    payload: Mapping[str, Any],
    *,
    expected_match_id: int,
) -> dict[tuple[int, int], int]:
    match = _validate_match_payload(payload, expected_match_id)
    output: dict[tuple[int, int], int] = {}
    players = match.get("players")
    assert isinstance(players, list)
    for player in players:
        if not isinstance(player, Mapping):
            continue
        account_id = player.get("steamAccountId")
        hero_id = player.get("heroId")
        position = _position(player.get("position"))
        if (
            type(account_id) is int
            and account_id > 0
            and type(hero_id) is int
            and hero_id > 0
            and position is not None
        ):
            output[(account_id, hero_id)] = position
    return output


def _validate_match_payload(
    payload: object,
    expected_match_id: int,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or payload.get("errors"):
        raise StratzDetailError("STRATZ detail GraphQL response failed")
    data = payload.get("data")
    match = data.get("match") if isinstance(data, Mapping) else None
    if not isinstance(match, Mapping) or match.get("id") != expected_match_id:
        raise StratzDetailError("STRATZ detail match identity is invalid")
    players = match.get("players")
    if not isinstance(players, list):
        raise StratzDetailError("STRATZ detail players are unavailable")
    return match


def _position(value: object) -> int | None:
    if type(value) is int and 1 <= value <= 5:
        return value
    if isinstance(value, str):
        matched = _POSITION_PATTERN.fullmatch(value.strip().upper())
        if matched is not None:
            return int(matched.group(1))
    return None


__all__ = [
    "STRATZ_GRAPHQL_ENDPOINT",
    "StratzDetailError",
    "StratzMatchDetailClient",
    "resolve_stratz_detail_token",
    "stratz_player_positions",
]
