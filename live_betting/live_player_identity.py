"""Fail-closed live player-to-hero identity from OpenDota."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from fetch.client import OpenDotaClient


logger = logging.getLogger(__name__)
OPENDOTA_LIVE_ENDPOINT = f"{OpenDotaClient.BASE_URL}/api/live"
LIVE_IDENTITY_SOURCE = "opendota_live"
LIVE_IDENTITY_TTL = timedelta(minutes=5)
LIVE_IDENTITY_NEGATIVE_TTL = timedelta(seconds=30)


class OpenDotaLiveError(RuntimeError):
    """A sanitized OpenDota live transport failure."""


@dataclass(frozen=True)
class LivePlayerIdentity:
    radiant_team_id: int
    dire_team_id: int
    radiant_hero_ids: tuple[int, ...]
    dire_hero_ids: tuple[int, ...]
    radiant_player_ids: tuple[int, ...]
    dire_player_ids: tuple[int, ...]
    source_match_id: int
    source_name: str
    fetched_at: datetime
    evidence_hash: str


class OpenDotaLiveClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 3.0,
        get: Callable[..., Any] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._get = get or httpx.get

    def fetch_live_matches(self) -> Sequence[Mapping[str, Any]]:
        try:
            response = self._get(
                OPENDOTA_LIVE_ENDPOINT,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as error:
            raise OpenDotaLiveError(
                f"OpenDota live request failed ({type(error).__name__})"
            ) from None
        if not isinstance(payload, list) or any(
            not isinstance(match, Mapping) for match in payload
        ):
            raise OpenDotaLiveError("OpenDota live response has an invalid shape")
        return payload


class LivePlayerIdentityResolver:
    """Resolve only one exact team-and-draft live candidate.

    A response fetched during the current run is cached but never returned from
    that same call. It becomes eligible only after a later transport cutoff.
    """

    def __init__(
        self,
        *,
        fetch_live: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = LIVE_IDENTITY_TTL,
        negative_ttl: timedelta = LIVE_IDENTITY_NEGATIVE_TTL,
    ) -> None:
        self._fetch_live = fetch_live or OpenDotaLiveClient().fetch_live_matches
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ttl = ttl
        self._negative_ttl = negative_ttl
        self._cache: list[LivePlayerIdentity] = []
        self._last_fetch_by_request: dict[
            tuple[int, int, tuple[int, ...], tuple[int, ...]], datetime
        ] = {}

    def resolve(
        self,
        *,
        radiant_team_id: int,
        dire_team_id: int,
        radiant_hero_ids: Sequence[int],
        dire_hero_ids: Sequence[int],
        as_of: datetime,
    ) -> LivePlayerIdentity | None:
        request = _validated_request(
            radiant_team_id,
            dire_team_id,
            radiant_hero_ids,
            dire_hero_ids,
            as_of,
        )
        if request is None:
            return None
        radiant, dire, cutoff = request
        request_key = (radiant_team_id, dire_team_id, radiant, dire)
        matching = [
            identity
            for identity in self._cache
            if _matches_request(
                identity,
                radiant_team_id,
                dire_team_id,
                radiant,
                dire,
            )
        ]
        eligible = [
            identity
            for identity in matching
            if identity.fetched_at <= cutoff
            and cutoff - identity.fetched_at <= self._ttl
        ]
        if eligible:
            identities = {
                (row.radiant_player_ids, row.dire_player_ids) for row in eligible
            }
            if len(identities) != 1:
                return None
            return max(eligible, key=lambda row: row.fetched_at)
        if any(identity.fetched_at > cutoff for identity in matching):
            return None
        last_fetch = self._last_fetch_by_request.get(request_key)
        if (
            last_fetch is not None
            and cutoff - last_fetch < self._negative_ttl
        ):
            return None

        try:
            matches = self._fetch_live()
            fetched_at = _utc(self._clock())
        except Exception as error:
            try:
                fetched_at = _utc(self._clock())
            except (TypeError, ValueError):
                fetched_at = None
            if fetched_at is not None:
                self._last_fetch_by_request[request_key] = fetched_at
            logger.warning(
                "OpenDota live identity unavailable (%s)", type(error).__name__
            )
            return None
        self._last_fetch_by_request[request_key] = fetched_at
        self._last_fetch_by_request = {
            key: value
            for key, value in self._last_fetch_by_request.items()
            if fetched_at - value <= self._ttl
        }
        identity = _resolve_snapshot(
            matches,
            radiant_team_id=radiant_team_id,
            dire_team_id=dire_team_id,
            radiant_hero_ids=radiant,
            dire_hero_ids=dire,
            fetched_at=fetched_at,
        )
        if identity is not None:
            self._cache.append(identity)
            self._cache = [
                row
                for row in self._cache
                if fetched_at - row.fetched_at <= self._ttl
            ]
        return None


def _validated_request(
    radiant_team_id: int,
    dire_team_id: int,
    radiant_hero_ids: Sequence[int],
    dire_hero_ids: Sequence[int],
    as_of: datetime,
) -> tuple[tuple[int, ...], tuple[int, ...], datetime] | None:
    radiant = tuple(radiant_hero_ids)
    dire = tuple(dire_hero_ids)
    heroes = (*radiant, *dire)
    if (
        type(radiant_team_id) is not int
        or radiant_team_id <= 0
        or type(dire_team_id) is not int
        or dire_team_id <= 0
        or radiant_team_id == dire_team_id
        or len(radiant) != 5
        or len(dire) != 5
        or any(type(hero_id) is not int or hero_id <= 0 for hero_id in heroes)
        or len(set(heroes)) != 10
        or as_of.tzinfo is None
        or as_of.utcoffset() is None
    ):
        return None
    return radiant, dire, as_of.astimezone(timezone.utc)


def _matches_request(
    identity: LivePlayerIdentity,
    radiant_team_id: int,
    dire_team_id: int,
    radiant_hero_ids: tuple[int, ...],
    dire_hero_ids: tuple[int, ...],
) -> bool:
    return (
        identity.radiant_team_id == radiant_team_id
        and identity.dire_team_id == dire_team_id
        and identity.radiant_hero_ids == radiant_hero_ids
        and identity.dire_hero_ids == dire_hero_ids
    )


def _resolve_snapshot(
    matches: Sequence[Mapping[str, Any]],
    *,
    radiant_team_id: int,
    dire_team_id: int,
    radiant_hero_ids: tuple[int, ...],
    dire_hero_ids: tuple[int, ...],
    fetched_at: datetime,
) -> LivePlayerIdentity | None:
    if not isinstance(matches, Sequence) or isinstance(matches, (str, bytes)):
        return None
    candidates: list[Mapping[str, Any]] = []
    for match in matches:
        if not isinstance(match, Mapping):
            continue
        candidate_radiant_team_id = match.get("team_id_radiant")
        candidate_dire_team_id = match.get("team_id_dire")
        if (
            type(candidate_radiant_team_id) is not int
            or type(candidate_dire_team_id) is not int
            or candidate_radiant_team_id != radiant_team_id
            or candidate_dire_team_id != dire_team_id
        ):
            continue
        side_players = _players_by_side(match.get("players"), require_accounts=False)
        if side_players is None:
            continue
        if (
            {hero_id for hero_id, _ in side_players[0]} == set(radiant_hero_ids)
            and {hero_id for hero_id, _ in side_players[1]} == set(dire_hero_ids)
        ):
            candidates.append(match)
    if len(candidates) != 1:
        return None

    match = candidates[0]
    source_match_id = match.get("match_id")
    if type(source_match_id) is not int or source_match_id <= 0:
        return None
    side_players = _players_by_side(match.get("players"), require_accounts=True)
    if side_players is None:
        return None
    hero_to_account = {
        hero_id: account_id
        for players in side_players.values()
        for hero_id, account_id in players
    }
    accounts = tuple(hero_to_account.values())
    if len(hero_to_account) != 10 or len(set(accounts)) != 10:
        return None
    radiant_players = tuple(hero_to_account[hero_id] for hero_id in radiant_hero_ids)
    dire_players = tuple(hero_to_account[hero_id] for hero_id in dire_hero_ids)
    return LivePlayerIdentity(
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
        radiant_hero_ids=radiant_hero_ids,
        dire_hero_ids=dire_hero_ids,
        radiant_player_ids=radiant_players,
        dire_player_ids=dire_players,
        source_match_id=source_match_id,
        source_name=LIVE_IDENTITY_SOURCE,
        fetched_at=fetched_at,
        evidence_hash=canonical_live_player_identity_evidence_hash(
            radiant_team_id=radiant_team_id,
            dire_team_id=dire_team_id,
            radiant_hero_ids=radiant_hero_ids,
            dire_hero_ids=dire_hero_ids,
            radiant_player_ids=radiant_players,
            dire_player_ids=dire_players,
            source_match_id=source_match_id,
            source_name=LIVE_IDENTITY_SOURCE,
            fetched_at=fetched_at,
        ),
    )


def _players_by_side(
    raw_players: Any,
    *,
    require_accounts: bool,
) -> dict[int, list[tuple[int, int]]] | None:
    if not isinstance(raw_players, list) or len(raw_players) != 10:
        return None
    result: dict[int, list[tuple[int, int]]] = {0: [], 1: []}
    for player in raw_players:
        if not isinstance(player, Mapping):
            return None
        team = player.get("team")
        hero_id = player.get("hero_id")
        account_id = player.get("account_id")
        if (
            type(team) is not int
            or team not in {0, 1}
            or type(hero_id) is not int
            or hero_id <= 0
            or (
                require_accounts
                and (type(account_id) is not int or account_id <= 0)
            )
        ):
            return None
        result[team].append((hero_id, account_id if type(account_id) is int else 0))
    if any(len(players) != 5 for players in result.values()):
        return None
    if any(
        len({hero_id for hero_id, _ in players}) != 5
        for players in result.values()
    ):
        return None
    return result


def _evidence_hash(evidence: Mapping[str, Any]) -> str:
    payload = json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_live_player_identity_evidence_hash(
    *,
    radiant_team_id: int,
    dire_team_id: int,
    radiant_hero_ids: Sequence[int],
    dire_hero_ids: Sequence[int],
    radiant_player_ids: Sequence[int],
    dire_player_ids: Sequence[int],
    source_match_id: int,
    source_name: str,
    fetched_at: datetime,
) -> str:
    radiant_heroes = tuple(radiant_hero_ids)
    dire_heroes = tuple(dire_hero_ids)
    radiant_players = tuple(radiant_player_ids)
    dire_players = tuple(dire_player_ids)
    if (
        type(radiant_team_id) is not int
        or radiant_team_id <= 0
        or type(dire_team_id) is not int
        or dire_team_id <= 0
        or radiant_team_id == dire_team_id
        or type(source_match_id) is not int
        or source_match_id <= 0
        or source_name != LIVE_IDENTITY_SOURCE
        or len(radiant_heroes) != 5
        or len(dire_heroes) != 5
        or len(radiant_players) != 5
        or len(dire_players) != 5
        or any(
            type(value) is not int or value <= 0
            for value in (
                *radiant_heroes,
                *dire_heroes,
                *radiant_players,
                *dire_players,
            )
        )
        or len(set((*radiant_heroes, *dire_heroes))) != 10
        or len(set((*radiant_players, *dire_players))) != 10
    ):
        raise ValueError("live player identity evidence is invalid")
    evidence = {
        "dire": [
            {"account_id": account_id, "hero_id": hero_id}
            for hero_id, account_id in zip(dire_heroes, dire_players)
        ],
        "dire_team_id": dire_team_id,
        "fetched_at": _utc(fetched_at).isoformat(),
        "radiant": [
            {"account_id": account_id, "hero_id": hero_id}
            for hero_id, account_id in zip(radiant_heroes, radiant_players)
        ],
        "radiant_team_id": radiant_team_id,
        "source": source_name,
        "source_match_id": source_match_id,
    }
    return _evidence_hash(evidence)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


__all__ = [
    "canonical_live_player_identity_evidence_hash",
    "LivePlayerIdentity",
    "LivePlayerIdentityResolver",
    "OpenDotaLiveClient",
    "OpenDotaLiveError",
]
