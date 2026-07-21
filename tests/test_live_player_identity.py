from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from live_betting.live_player_identity import (
    LivePlayerIdentityResolver,
    OpenDotaLiveClient,
    OpenDotaLiveError,
)
from live_betting.shadow_monitor import _canonical_live_team_ids


RADIANT_HEROES = (3, 1, 5, 2, 4)
DIRE_HEROES = (8, 6, 10, 7, 9)


def live_match(
    *,
    match_id: int = 7001,
    radiant_team_id: int = 10,
    dire_team_id: int = 20,
    radiant_heroes: tuple[int, ...] = (1, 2, 3, 4, 5),
    dire_heroes: tuple[int, ...] = (6, 7, 8, 9, 10),
) -> dict[str, object]:
    return {
        "match_id": match_id,
        "team_id_radiant": radiant_team_id,
        "team_id_dire": dire_team_id,
        "players": [
            *[
                {"account_id": 1000 + hero_id, "hero_id": hero_id, "team": 0}
                for hero_id in radiant_heroes
            ],
            *[
                {"account_id": 1000 + hero_id, "hero_id": hero_id, "team": 1}
                for hero_id in dire_heroes
            ],
        ],
    }


def resolve_once(
    matches: list[dict[str, object]],
    *,
    fetched_at: datetime,
    radiant_team_id: int = 10,
    dire_team_id: int = 20,
    radiant_heroes: tuple[int, ...] = RADIANT_HEROES,
    dire_heroes: tuple[int, ...] = DIRE_HEROES,
) -> tuple[LivePlayerIdentityResolver, object | None]:
    resolver = LivePlayerIdentityResolver(
        fetch_live=lambda: matches,
        clock=lambda: fetched_at,
    )
    result = resolver.resolve(
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
        radiant_hero_ids=radiant_heroes,
        dire_hero_ids=dire_heroes,
        as_of=fetched_at - timedelta(seconds=1),
    )
    return resolver, result


def test_unique_exact_candidate_becomes_available_to_later_transport() -> None:
    fetched_at = datetime(2026, 7, 22, 10, 0, 1, tzinfo=timezone.utc)
    calls = 0

    def fetch() -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return [live_match()]

    resolver = LivePlayerIdentityResolver(fetch_live=fetch, clock=lambda: fetched_at)
    fresh = resolver.resolve(
        radiant_team_id=10,
        dire_team_id=20,
        radiant_hero_ids=RADIANT_HEROES,
        dire_hero_ids=DIRE_HEROES,
        as_of=fetched_at - timedelta(seconds=1),
    )
    resolved = resolver.resolve(
        radiant_team_id=10,
        dire_team_id=20,
        radiant_hero_ids=RADIANT_HEROES,
        dire_hero_ids=DIRE_HEROES,
        as_of=fetched_at + timedelta(seconds=1),
    )

    assert fresh is None
    assert resolved is not None
    assert calls == 1
    assert resolved.fetched_at == fetched_at
    assert resolved.radiant_player_ids == (1003, 1001, 1005, 1002, 1004)
    assert resolved.dire_player_ids == (1008, 1006, 1010, 1007, 1009)
    assert len(resolved.evidence_hash) == 64


def test_ambiguous_exact_candidates_fail_closed() -> None:
    fetched_at = datetime(2026, 7, 22, 10, 0, 1, tzinfo=timezone.utc)
    calls = 0

    def fetch() -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return [live_match(match_id=7001), live_match(match_id=7002)]

    resolver = LivePlayerIdentityResolver(fetch_live=fetch, clock=lambda: fetched_at)
    request = {
        "radiant_team_id": 10,
        "dire_team_id": 20,
        "radiant_hero_ids": RADIANT_HEROES,
        "dire_hero_ids": DIRE_HEROES,
        "as_of": fetched_at - timedelta(seconds=1),
    }
    first = resolver.resolve(**request)
    repeated = resolver.resolve(**request)

    assert first is None
    assert repeated is None
    assert calls == 1
    assert resolver.resolve(
        **{**request, "as_of": fetched_at + timedelta(seconds=29)}
    ) is None
    assert calls == 1
    assert resolver.resolve(
        **{**request, "as_of": fetched_at + timedelta(seconds=30)}
    ) is None
    assert calls == 2


def test_missing_account_id_fails_closed() -> None:
    fetched_at = datetime(2026, 7, 22, 10, 0, 1, tzinfo=timezone.utc)
    candidate = live_match()
    candidate["players"][0]["account_id"] = None  # type: ignore[index]
    resolver, result = resolve_once([candidate], fetched_at=fetched_at)

    assert result is None
    assert resolver.resolve(
        radiant_team_id=10,
        dire_team_id=20,
        radiant_hero_ids=RADIANT_HEROES,
        dire_hero_ids=DIRE_HEROES,
        as_of=fetched_at + timedelta(seconds=1),
    ) is None


def test_hero_set_mismatch_fails_closed() -> None:
    fetched_at = datetime(2026, 7, 22, 10, 0, 1, tzinfo=timezone.utc)
    _, result = resolve_once(
        [live_match(radiant_heroes=(1, 2, 3, 4, 11))],
        fetched_at=fetched_at,
    )
    assert result is None


def test_radiant_team_side_reverses_canonical_team_ids() -> None:
    mapping = SimpleNamespace(
        canonical_team_one_id=10,
        canonical_team_two_id=20,
    )
    assert _canonical_live_team_ids(mapping, "team_one") == (10, 20)
    assert _canonical_live_team_ids(mapping, "team_two") == (20, 10)
    assert _canonical_live_team_ids(mapping, None) is None

    fetched_at = datetime(2026, 7, 22, 10, 0, 1, tzinfo=timezone.utc)
    resolver, fresh = resolve_once(
        [live_match(radiant_team_id=20, dire_team_id=10)],
        fetched_at=fetched_at,
        radiant_team_id=20,
        dire_team_id=10,
    )
    assert fresh is None
    assert resolver.resolve(
        radiant_team_id=20,
        dire_team_id=10,
        radiant_hero_ids=RADIANT_HEROES,
        dire_hero_ids=DIRE_HEROES,
        as_of=fetched_at + timedelta(seconds=1),
    ) is not None


def test_transport_error_does_not_leak_message(caplog: object) -> None:
    secret = "do-not-log-this-response-body"

    def fetch() -> list[dict[str, object]]:
        raise RuntimeError(secret)

    resolver = LivePlayerIdentityResolver(fetch_live=fetch)
    with caplog.at_level(logging.WARNING):  # type: ignore[attr-defined]
        result = resolver.resolve(
            radiant_team_id=10,
            dire_team_id=20,
            radiant_hero_ids=RADIANT_HEROES,
            dire_hero_ids=DIRE_HEROES,
            as_of=datetime.now(timezone.utc),
        )

    assert result is None
    assert secret not in caplog.text  # type: ignore[attr-defined]


def test_opendota_live_client_uses_three_second_total_timeout() -> None:
    seen_timeout = None

    def get(_url: str, *, timeout: float) -> object:
        nonlocal seen_timeout
        seen_timeout = timeout
        raise TimeoutError("timed out")

    client = OpenDotaLiveClient(get=get)

    with pytest.raises(OpenDotaLiveError):
        client.fetch_live_matches()

    assert seen_timeout == 3.0


def test_timeout_failure_is_negative_cached_for_thirty_seconds() -> None:
    fetched_at = datetime(2026, 7, 22, 10, 0, 1, tzinfo=timezone.utc)
    calls = 0

    def fetch() -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        raise TimeoutError("timed out")

    resolver = LivePlayerIdentityResolver(fetch_live=fetch, clock=lambda: fetched_at)
    request = {
        "radiant_team_id": 10,
        "dire_team_id": 20,
        "radiant_hero_ids": RADIANT_HEROES,
        "dire_hero_ids": DIRE_HEROES,
        "as_of": fetched_at - timedelta(seconds=1),
    }

    assert resolver.resolve(**request) is None
    assert resolver.resolve(
        **{**request, "as_of": fetched_at + timedelta(seconds=29)}
    ) is None
    assert calls == 1
    assert resolver.resolve(
        **{**request, "as_of": fetched_at + timedelta(seconds=30)}
    ) is None
    assert calls == 2
