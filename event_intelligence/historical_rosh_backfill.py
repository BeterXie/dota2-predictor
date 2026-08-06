"""Backfill retrospective Rosh scores for approved T1/T2 OpenDota maps."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from database.session import PostgresSession

from live_betting.stratz_rosh_client import (
    ROSH_FORMULA_VERSION,
    FetchedHistoricalRoshLineupScore,
    FetchedHistoricalRoshScore,
    StratzRoshClient,
    StratzRoshError,
    historical_rosh_lineup_identity,
)

from .storage import query_historical_rosh_lineup_score


EXPECTED_PLAYER_SLOTS = frozenset((*range(5), *range(128, 133)))


class HistoricalRoshIdentityError(ValueError):
    """OpenDota and STRATZ do not prove the same ten-player lineup."""


class HistoricalRoshStorage(Protocol):
    connection: PostgresSession

    def insert_historical_rosh_lineup_score(self, **values: Any) -> Any: ...


@dataclass(frozen=True)
class OpenDotaHistoricalPlayer:
    player_slot: int
    hero_id: int
    account_id: int
    is_radiant: bool


@dataclass(frozen=True)
class OpenDotaHistoricalMatch:
    match_id: int
    players: tuple[OpenDotaHistoricalPlayer, ...]


@dataclass(frozen=True)
class HistoricalRoshBackfillFailure:
    match_id: int
    error: str


@dataclass(frozen=True)
class HistoricalRoshBackfillReport:
    selected: int
    inserted: int
    skipped: int
    failed: int
    failures: tuple[HistoricalRoshBackfillFailure, ...]


ExistingScoreQuery = Callable[
    [PostgresSession, OpenDotaHistoricalMatch, str],
    object | None,
]
Sleep = Callable[[float], None]
PersistScore = Callable[
    [
        HistoricalRoshStorage,
        int,
        Mapping[str, Sequence[int]],
        FetchedHistoricalRoshLineupScore,
        datetime,
    ],
    bool,
]


def load_formal_match_ids(
    connection: PostgresSession,
    *,
    match_id: int | None = None,
) -> tuple[int, ...]:
    if match_id is not None and (type(match_id) is not int or match_id <= 0):
        raise ValueError("match_id must be a positive integer")
    parameters: list[Any] = []
    predicate = ""
    if match_id is not None:
        predicate = "WHERE match_id=?"
        parameters.append(match_id)
    rows = connection.execute(
        f"""SELECT match_id FROM formal_map_eligibility
             {predicate}
             ORDER BY match_id""",
        tuple(parameters),
    ).fetchall()
    if match_id is not None and not rows:
        raise ValueError(f"formal T1/T2 match {match_id} was not found")
    return tuple(int(row[0]) for row in rows)


def load_opendota_historical_match(
    connection: PostgresSession,
    match_id: int,
) -> OpenDotaHistoricalMatch:
    rows = connection.execute(
        """SELECT player_slot, hero_id, account_id, is_radiant
             FROM match_players
            WHERE match_id=?
            ORDER BY player_slot""",
        (match_id,),
    ).fetchall()
    if len(rows) != 10:
        raise HistoricalRoshIdentityError(
            f"OpenDota match {match_id} does not contain exactly ten players"
        )
    players: list[OpenDotaHistoricalPlayer] = []
    for row in rows:
        player_slot, hero_id, account_id, is_radiant = row
        if (
            type(player_slot) is not int
            or player_slot not in EXPECTED_PLAYER_SLOTS
            or type(hero_id) is not int
            or hero_id <= 0
            or type(account_id) is not int
            or account_id <= 0
            or is_radiant not in (0, 1)
        ):
            raise HistoricalRoshIdentityError(
                f"OpenDota match {match_id} has an invalid player identity"
            )
        radiant = bool(is_radiant)
        if radiant != (player_slot < 128):
            raise HistoricalRoshIdentityError(
                f"OpenDota match {match_id} has inconsistent player sides"
            )
        players.append(
            OpenDotaHistoricalPlayer(
                player_slot=player_slot,
                hero_id=hero_id,
                account_id=account_id,
                is_radiant=radiant,
            )
        )
    if {player.player_slot for player in players} != EXPECTED_PLAYER_SLOTS:
        raise HistoricalRoshIdentityError(
            f"OpenDota match {match_id} has duplicate or missing player slots"
        )
    if len({player.hero_id for player in players}) != 10:
        raise HistoricalRoshIdentityError(
            f"OpenDota match {match_id} has duplicate hero IDs"
        )
    if len({player.account_id for player in players}) != 10:
        raise HistoricalRoshIdentityError(
            f"OpenDota match {match_id} has duplicate account IDs"
        )
    return OpenDotaHistoricalMatch(match_id=match_id, players=tuple(players))


def verify_historical_rosh_identity(
    local: OpenDotaHistoricalMatch,
    fetched: FetchedHistoricalRoshScore,
) -> dict[str, tuple[int, ...]]:
    match = fetched.context.get("match")
    source_match_id = match.get("id") if isinstance(match, Mapping) else None
    if source_match_id != local.match_id:
        raise HistoricalRoshIdentityError(
            f"STRATZ returned a different match for {local.match_id}"
        )
    identity = historical_rosh_lineup_identity(fetched.context)
    raw_radiant_players = identity["radiant_player_ids"]
    raw_dire_players = identity["dire_player_ids"]
    if any(
        type(player_id) is not int or player_id <= 0
        for player_id in (*raw_radiant_players, *raw_dire_players)
    ):
        raise HistoricalRoshIdentityError(
            f"STRATZ match {local.match_id} does not expose ten player identities"
        )
    stratz_identity = {
        "radiant_hero_ids": tuple(int(value) for value in identity["radiant_hero_ids"]),
        "dire_hero_ids": tuple(int(value) for value in identity["dire_hero_ids"]),
        "radiant_player_ids": tuple(int(value) for value in raw_radiant_players),
        "dire_player_ids": tuple(int(value) for value in raw_dire_players),
    }
    local_pairs = {
        side: {
            (player.hero_id, player.account_id)
            for player in local.players
            if player.is_radiant is side
        }
        for side in (True, False)
    }
    stratz_pairs = {
        True: set(
            zip(
                stratz_identity["radiant_hero_ids"],
                stratz_identity["radiant_player_ids"],
            )
        ),
        False: set(
            zip(
                stratz_identity["dire_hero_ids"],
                stratz_identity["dire_player_ids"],
            )
        ),
    }
    if local_pairs != stratz_pairs:
        raise HistoricalRoshIdentityError(
            f"OpenDota and STRATZ identities disagree for match {local.match_id}"
        )
    # The score is calculated in STRATZ position order, which remains in the
    # fetched context/evidence. Persist identities in canonical OpenDota slot
    # order because the historical API performs exact match_players lookups.
    local_sides = {
        side: tuple(
            sorted(
                (player for player in local.players if player.is_radiant is side),
                key=lambda player: player.player_slot,
            )
        )
        for side in (True, False)
    }
    return {
        "radiant_hero_ids": tuple(
            player.hero_id for player in local_sides[True]
        ),
        "dire_hero_ids": tuple(player.hero_id for player in local_sides[False]),
        "radiant_player_ids": tuple(
            player.account_id for player in local_sides[True]
        ),
        "dire_player_ids": tuple(
            player.account_id for player in local_sides[False]
        ),
    }


def _default_existing_query(
    connection: PostgresSession,
    local: OpenDotaHistoricalMatch,
    formula_version: str,
) -> object | None:
    players = tuple(sorted(local.players, key=lambda player: player.player_slot))
    radiant = tuple(player for player in players if player.is_radiant)
    dire = tuple(player for player in players if not player.is_radiant)
    return query_historical_rosh_lineup_score(
        connection,
        match_id=local.match_id,
        formula_version=formula_version,
        radiant_hero_ids=tuple(player.hero_id for player in radiant),
        dire_hero_ids=tuple(player.hero_id for player in dire),
        radiant_player_ids=tuple(player.account_id for player in radiant),
        dire_player_ids=tuple(player.account_id for player in dire),
    )


def load_existing_historical_rosh_score(
    connection: PostgresSession,
    match_id: int,
    *,
    formula_version: str = ROSH_FORMULA_VERSION,
) -> object | None:
    local = load_opendota_historical_match(connection, match_id)
    return _default_existing_query(connection, local, formula_version)


def existing_historical_rosh_score_for_identity(
    connection: PostgresSession,
    *,
    match_id: int,
    formula_version: str,
    identity: Mapping[str, Sequence[int]],
) -> object | None:
    return query_historical_rosh_lineup_score(
        connection,
        match_id=match_id,
        formula_version=formula_version,
        radiant_hero_ids=identity["radiant_hero_ids"],
        dire_hero_ids=identity["dire_hero_ids"],
        radiant_player_ids=identity["radiant_player_ids"],
        dire_player_ids=identity["dire_player_ids"],
    )


def historical_rosh_score_is_complete(
    existing: object | None,
    *,
    include_current_player_adjustment: bool,
) -> bool:
    if existing is None:
        return False
    if not include_current_player_adjustment:
        return True
    return bool(
        getattr(existing, "scoring_mode", None) == "current_player_adjusted"
        and getattr(existing, "player_coverage_count", None) == 10
    )


def historical_rosh_score_should_append(
    existing: object | None,
    score: FetchedHistoricalRoshLineupScore,
) -> bool:
    """Append only when player evidence strictly improves for this identity."""
    if existing is None:
        return True
    existing_coverage = getattr(existing, "player_coverage_count", None)
    if type(existing_coverage) is not int:
        return True
    return score.player_coverage_count > existing_coverage


def persist_historical_rosh_score(
    storage: HistoricalRoshStorage,
    match_id: int,
    identity: Mapping[str, Sequence[int]],
    score: FetchedHistoricalRoshLineupScore,
    created_at: datetime,
) -> bool:
    existing = existing_historical_rosh_score_for_identity(
        storage.connection,
        match_id=match_id,
        formula_version=score.formula_version,
        identity=identity,
    )
    if not historical_rosh_score_should_append(existing, score):
        return False
    storage.insert_historical_rosh_lineup_score(
        match_id=match_id,
        **identity,
        pure_lineup_score=score.pure_lineup_score,
        current_player_adjusted_lineup_score=(
            score.current_player_adjusted_lineup_score
        ),
        effective_lineup_score=score.effective_lineup_score,
        scoring_mode=score.scoring_mode,
        player_coverage_count=score.player_coverage_count,
        source_week=score.source_week,
        source_as_of=score.source_as_of,
        player_stats_as_of=score.player_stats_as_of,
        formula_version=score.formula_version,
        evidence=score.evidence,
        evidence_hash=score.evidence_hash,
        source_name=score.source_name,
        created_at=created_at,
    )
    return True


def _safe_failure(error: Exception) -> str:
    if isinstance(error, StratzRoshError):
        return f"StratzRoshError: {error.category}"
    if isinstance(error, (HistoricalRoshIdentityError, ValueError)):
        return f"{type(error).__name__}: {error}"
    return type(error).__name__


def _fetch_with_retry(
    client: StratzRoshClient,
    match_id: int,
    *,
    include_current_player_adjustment: bool,
    max_attempts: int,
    initial_backoff_seconds: float,
    sleep: Sleep,
) -> FetchedHistoricalRoshScore:
    for attempt in range(max_attempts):
        try:
            return client.fetch_historical_match_score(
                match_id,
                include_current_player_adjustment=(
                    include_current_player_adjustment
                ),
            )
        except StratzRoshError as error:
            if attempt + 1 >= max_attempts or not error.retryable:
                raise
            delay = initial_backoff_seconds * (2**attempt)
            if error.retry_after_seconds is not None:
                delay = max(delay, error.retry_after_seconds)
            sleep(delay)
    raise AssertionError("positive max_attempts must execute at least once")


def backfill_historical_rosh_scores(
    storage: HistoricalRoshStorage,
    client: StratzRoshClient,
    *,
    match_id: int | None = None,
    limit: int | None = None,
    include_current_player_adjustment: bool = True,
    clock: Callable[[], datetime] | None = None,
    existing_query: ExistingScoreQuery = _default_existing_query,
    persist_score: PersistScore = persist_historical_rosh_score,
    max_attempts: int = 3,
    initial_backoff_seconds: float = 1.0,
    throttle_seconds: float = 0.25,
    sleep: Sleep = time.sleep,
) -> HistoricalRoshBackfillReport:
    """Process formal maps independently so a failure never loses prior writes."""
    if type(max_attempts) is not int or max_attempts <= 0:
        raise ValueError("max_attempts must be a positive integer")
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("limit must be a positive integer")
    if initial_backoff_seconds < 0:
        raise ValueError("initial_backoff_seconds cannot be negative")
    if throttle_seconds < 0:
        raise ValueError("throttle_seconds cannot be negative")
    match_ids = load_formal_match_ids(
        storage.connection,
        match_id=match_id,
    )
    now = clock or (lambda: datetime.now(timezone.utc))
    inserted = 0
    skipped = 0
    failures: list[HistoricalRoshBackfillFailure] = []
    has_requested = False
    attempted = 0
    inspected = 0
    for selected_match_id in match_ids:
        if limit is not None and attempted >= limit:
            break
        inspected += 1
        try:
            local = load_opendota_historical_match(
                storage.connection,
                selected_match_id,
            )
            existing = existing_query(
                storage.connection,
                local,
                ROSH_FORMULA_VERSION,
            )
        except Exception as error:
            attempted += 1
            failures.append(
                HistoricalRoshBackfillFailure(
                    match_id=selected_match_id,
                    error=_safe_failure(error),
                )
            )
            continue
        if historical_rosh_score_is_complete(
            existing,
            include_current_player_adjustment=include_current_player_adjustment,
        ):
            skipped += 1
            continue

        attempted += 1
        try:
            if has_requested and throttle_seconds:
                sleep(throttle_seconds)
            has_requested = True
            fetched = _fetch_with_retry(
                client,
                selected_match_id,
                include_current_player_adjustment=(
                    include_current_player_adjustment
                ),
                max_attempts=max_attempts,
                initial_backoff_seconds=initial_backoff_seconds,
                sleep=sleep,
            )
            identity = verify_historical_rosh_identity(local, fetched)
            score = fetched.score
            if score is None:
                raise ValueError("STRATZ did not produce a historical Rosh score")
            created_at = now().astimezone(timezone.utc)
            if score.player_stats_as_of is not None:
                created_at = max(created_at, score.player_stats_as_of)
            if persist_score(
                storage,
                selected_match_id,
                identity,
                score,
                created_at,
            ):
                inserted += 1
            else:
                skipped += 1
        except Exception as error:
            failures.append(
                HistoricalRoshBackfillFailure(
                    match_id=selected_match_id,
                    error=_safe_failure(error),
                )
            )
    return HistoricalRoshBackfillReport(
        selected=inspected,
        inserted=inserted,
        skipped=skipped,
        failed=len(failures),
        failures=tuple(failures),
    )
