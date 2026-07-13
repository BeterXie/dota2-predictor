"""Causal, auditable Dota 2 position assignment."""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Iterable, Sequence

from .models import RolePurpose
from .raw_archive import canonical_json_bytes


ASSIGNMENT_VERSION = "role-assignment-v1"
ROLE_DEPENDENT_CONFIDENCE = 0.7
POSITIONS = (1, 2, 3, 4, 5)


class RoleSource(str, Enum):
    AUDITED_ROSTER = "audited_roster"
    HISTORICAL_PATTERN = "historical_pattern"
    SINGLE_MAP = "single_map"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuditedRosterPosition:
    player_id: int
    position: int
    audited_at: datetime
    first_usable_at: datetime | None


@dataclass(frozen=True)
class HistoricalPositionEvidence:
    player_id: int
    match_id: int
    position: int
    confidence: float
    completed_at: datetime
    first_usable_at: datetime | None


@dataclass(frozen=True)
class SingleMapRoleEvidence:
    player_id: int
    first_usable_at: datetime | None
    lane_role: int | None = None
    gold_at_10: int | float | None = None
    last_hits_at_10: int | float | None = None
    is_roaming: bool | None = None
    observer_wards_at_10: int | None = None
    sentry_wards_at_10: int | None = None
    stacks_at_10: int | None = None
    final_gpm: int | None = None


@dataclass(frozen=True)
class RoleAssignment:
    match_id: int
    player_id: int
    purpose: RolePurpose
    position: int | None
    source: RoleSource
    confidence: float
    cutoff: datetime
    input_hash: str
    version: str
    usable_for_role_dependent: bool
    supporting_match_ids: tuple[int, ...]
    reason: str | None


@dataclass(frozen=True)
class _Resolved:
    position: int
    source: RoleSource
    confidence: float
    supporting_match_ids: tuple[int, ...] = ()


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_team(match_id: int, player_ids: Sequence[int]) -> tuple[int, ...]:
    if isinstance(match_id, bool) or not isinstance(match_id, int) or match_id <= 0:
        raise ValueError("match_id must be a positive integer")
    players = tuple(player_ids)
    if len(players) != 5 or len(set(players)) != 5:
        raise ValueError("position assignment requires exactly five unique players")
    if any(
        isinstance(player_id, bool)
        or not isinstance(player_id, int)
        or player_id == 0
        for player_id in players
    ):
        raise ValueError("player IDs must be non-zero integers")
    return players


def _eligible_roster(
    records: Iterable[AuditedRosterPosition],
    player_ids: tuple[int, ...],
    cutoff: datetime,
) -> tuple[dict[int, AuditedRosterPosition], tuple[AuditedRosterPosition, ...]]:
    by_player: dict[int, list[AuditedRosterPosition]] = {}
    player_set = set(player_ids)
    for record in records:
        if (
            record.player_id in player_set
            and record.audited_at <= cutoff
            and record.first_usable_at is not None
            and record.first_usable_at <= cutoff
        ):
            by_player.setdefault(record.player_id, []).append(record)

    latest: dict[int, AuditedRosterPosition] = {}
    for player_id, candidates in by_player.items():
        ordered = sorted(
            candidates,
            key=lambda row: (
                row.audited_at,
                row.first_usable_at,
                -row.position,
            ),
            reverse=True,
        )
        latest[player_id] = ordered[0]

    position_counts = {
        position: sum(row.position == position for row in latest.values())
        for position in POSITIONS
    }
    accepted = {
        player_id: row
        for player_id, row in latest.items()
        if position_counts[row.position] == 1
    }
    return accepted, tuple(sorted(latest.values(), key=lambda row: row.player_id))


def _eligible_history(
    records: Iterable[HistoricalPositionEvidence],
    player_ids: tuple[int, ...],
    match_id: int,
    target_started_at: datetime,
    cutoff: datetime,
) -> dict[int, tuple[HistoricalPositionEvidence, ...]]:
    player_set = set(player_ids)
    grouped: dict[int, list[HistoricalPositionEvidence]] = {}
    for record in records:
        if (
            record.player_id in player_set
            and record.match_id != match_id
            and record.completed_at < target_started_at
            and record.completed_at <= cutoff
            and record.first_usable_at is not None
            and record.first_usable_at <= cutoff
        ):
            grouped.setdefault(record.player_id, []).append(record)

    result: dict[int, tuple[HistoricalPositionEvidence, ...]] = {}
    for player_id, candidates in grouped.items():
        ordered = sorted(
            candidates,
            key=lambda row: (
                row.completed_at,
                row.first_usable_at,
                row.confidence,
                -row.position,
                row.match_id,
            ),
            reverse=True,
        )
        distinct: list[HistoricalPositionEvidence] = []
        seen_matches: set[int] = set()
        for row in ordered:
            if row.match_id in seen_matches:
                continue
            seen_matches.add(row.match_id)
            distinct.append(row)
            if len(distinct) == 20:
                break
        result[player_id] = tuple(distinct)
    return result


def _best_matching(
    player_ids: Sequence[int],
    available_positions: set[int],
    score: Callable[[int, int], float],
) -> dict[int, int]:
    players = tuple(player_ids)
    if not players or not available_positions:
        return {}
    options = [
        (None,)
        + tuple(
            position
            for position in sorted(available_positions)
            if score(player_id, position) > 0
        )
        for player_id in players
    ]
    best_key: tuple[float, int, tuple[int, ...]] | None = None
    best: tuple[int | None, ...] = (None,) * len(players)
    for choices in itertools.product(*options):
        known = tuple(choice for choice in choices if choice is not None)
        if len(known) != len(set(known)):
            continue
        total = sum(
            score(player_id, position)
            for player_id, position in zip(players, choices)
            if position is not None
        )
        tie_break = tuple(-(position if position is not None else 99) for position in choices)
        key = (round(total, 12), len(known), tie_break)
        if best_key is None or key > best_key:
            best_key = key
            best = choices
    return {
        player_id: position
        for player_id, position in zip(players, best)
        if position is not None
    }


def _history_assignments(
    unresolved: tuple[int, ...],
    available_positions: set[int],
    history: dict[int, tuple[HistoricalPositionEvidence, ...]],
) -> dict[int, _Resolved]:
    votes: dict[int, dict[int, float]] = {}
    for player_id in unresolved:
        rows = history.get(player_id, ())
        votes[player_id] = {
            position: sum(row.confidence for row in rows if row.position == position)
            for position in POSITIONS
        }
    matched = _best_matching(
        unresolved,
        available_positions,
        lambda player_id, position: votes[player_id][position],
    )
    resolved: dict[int, _Resolved] = {}
    for player_id, position in matched.items():
        rows = history[player_id]
        total = sum(row.confidence for row in rows)
        share = votes[player_id][position] / total if total > 0 else 0.0
        confidence = share * min(1.0, total / 5.0)
        resolved[player_id] = _Resolved(
            position=position,
            source=RoleSource.HISTORICAL_PATTERN,
            confidence=round(confidence, 6),
            supporting_match_ids=tuple(row.match_id for row in rows),
        )
    return resolved


def _percentile_ranks(
    evidence: Sequence[SingleMapRoleEvidence],
    attribute: str,
) -> dict[int, float]:
    rows = [
        (row.player_id, getattr(row, attribute))
        for row in evidence
        if getattr(row, attribute) is not None
    ]
    if not rows:
        return {}
    if len(rows) == 1:
        return {rows[0][0]: 0.5}
    ranks: dict[int, float] = {}
    for player_id, value in rows:
        below = sum(other < value for _, other in rows)
        equal = sum(other == value for _, other in rows)
        ranks[player_id] = (below + (equal - 1) / 2) / (len(rows) - 1)
    return ranks


def _middle_support_rank(rank: float) -> float:
    return max(0.0, 1.0 - abs(rank - 0.25) / 0.75)


def _single_map_scores(
    evidence: Sequence[SingleMapRoleEvidence],
) -> tuple[dict[int, dict[int, float]], dict[int, float]]:
    ranks = {
        field: _percentile_ranks(evidence, field)
        for field in (
            "gold_at_10",
            "last_hits_at_10",
            "observer_wards_at_10",
            "sentry_wards_at_10",
            "stacks_at_10",
        )
    }
    scores: dict[int, dict[int, float]] = {}
    coverage: dict[int, float] = {}
    for row in evidence:
        player_scores = {position: 0.0 for position in POSITIONS}
        if row.lane_role == 1:
            player_scores[1] += 5.0
            player_scores[4] += 1.0
            player_scores[5] += 1.0
        elif row.lane_role == 2:
            player_scores[2] += 8.0
        elif row.lane_role == 3:
            player_scores[3] += 5.0
            player_scores[4] += 1.0
            player_scores[5] += 1.0

        for field in ("gold_at_10", "last_hits_at_10"):
            rank = ranks[field].get(row.player_id)
            if rank is None:
                continue
            player_scores[1] += 3.0 * rank
            player_scores[2] += 2.0 * rank
            player_scores[3] += 2.0 * rank
            player_scores[4] += 1.5 * _middle_support_rank(rank)
            player_scores[5] += 2.0 * (1.0 - rank)

        if row.is_roaming is True:
            player_scores[4] += 3.0
            player_scores[5] += 0.5
        for field in ("observer_wards_at_10", "sentry_wards_at_10"):
            rank = ranks[field].get(row.player_id)
            if rank is not None:
                player_scores[4] += 0.75 * rank
                player_scores[5] += 1.5 * rank
        stack_rank = ranks["stacks_at_10"].get(row.player_id)
        if stack_rank is not None:
            player_scores[4] += 0.75 * stack_rank
            player_scores[5] += 1.5 * stack_rank

        present = sum(
            value is not None
            for value in (
                row.lane_role,
                row.gold_at_10,
                row.last_hits_at_10,
                row.is_roaming,
                row.observer_wards_at_10,
                row.sentry_wards_at_10,
                row.stacks_at_10,
            )
        )
        scores[row.player_id] = player_scores
        coverage[row.player_id] = present / 7.0
    return scores, coverage


def _single_map_assignments(
    unresolved: tuple[int, ...],
    available_positions: set[int],
    evidence: Sequence[SingleMapRoleEvidence],
) -> dict[int, _Resolved]:
    scores, coverage = _single_map_scores(evidence)
    matched = _best_matching(
        unresolved,
        available_positions,
        lambda player_id, position: scores.get(player_id, {}).get(position, 0.0),
    )
    resolved: dict[int, _Resolved] = {}
    for player_id, position in matched.items():
        player_scores = scores[player_id]
        assigned_score = player_scores[position]
        alternatives = [
            score for candidate, score in player_scores.items() if candidate != position
        ]
        margin = max(0.0, assigned_score - max(alternatives, default=0.0))
        separation = min(1.0, margin / (assigned_score + 1.0))
        confidence = coverage[player_id] * (0.55 + 0.45 * separation)
        resolved[player_id] = _Resolved(
            position=position,
            source=RoleSource.SINGLE_MAP,
            confidence=round(min(0.95, confidence), 6),
        )
    return resolved


def _datetime_json(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _input_hash(
    *,
    purpose: RolePurpose,
    match_id: int,
    player_ids: tuple[int, ...],
    target_started_at: datetime,
    cutoff: datetime,
    roster: Sequence[AuditedRosterPosition],
    history: dict[int, tuple[HistoricalPositionEvidence, ...]],
    target: Sequence[SingleMapRoleEvidence],
) -> str:
    payload = {
        "version": ASSIGNMENT_VERSION,
        "purpose": purpose.value,
        "match_id": match_id,
        "player_ids": sorted(player_ids),
        "target_started_at": _datetime_json(target_started_at),
        "cutoff": _datetime_json(cutoff),
        "roster": [
            (
                row.player_id,
                row.position,
                _datetime_json(row.audited_at),
                _datetime_json(row.first_usable_at),
            )
            for row in roster
        ],
        "history": [
            (
                row.player_id,
                row.match_id,
                row.position,
                row.confidence,
                _datetime_json(row.completed_at),
                _datetime_json(row.first_usable_at),
            )
            for player_id in player_ids
            for row in history.get(player_id, ())
        ],
        "target": [
            (
                row.player_id,
                _datetime_json(row.first_usable_at),
                row.lane_role,
                row.gold_at_10,
                row.last_hits_at_10,
                row.is_roaming,
                row.observer_wards_at_10,
                row.sentry_wards_at_10,
                row.stacks_at_10,
            )
            for row in sorted(target, key=lambda item: item.player_id)
        ],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _assign(
    *,
    purpose: RolePurpose,
    match_id: int,
    player_ids: Sequence[int],
    target_started_at: datetime,
    cutoff: datetime,
    audited_roster: Iterable[AuditedRosterPosition],
    history: Iterable[HistoricalPositionEvidence],
    target_evidence: Sequence[SingleMapRoleEvidence] = (),
) -> tuple[RoleAssignment, ...]:
    players = _validate_team(match_id, player_ids)
    target_started_at = _utc(target_started_at, "target_started_at")
    cutoff = _utc(cutoff, "cutoff")
    roster_rows = tuple(audited_roster)
    history_rows = tuple(history)
    eligible_roster, roster_for_hash = _eligible_roster(roster_rows, players, cutoff)
    eligible_history = _eligible_history(
        history_rows, players, match_id, target_started_at, cutoff
    )
    eligible_target = tuple(
        row
        for row in target_evidence
        if row.player_id in players
        and row.first_usable_at is not None
        and row.first_usable_at <= cutoff
    )

    resolved: dict[int, _Resolved] = {
        player_id: _Resolved(row.position, RoleSource.AUDITED_ROSTER, 1.0)
        for player_id, row in eligible_roster.items()
    }
    available = set(POSITIONS) - {row.position for row in resolved.values()}
    unresolved = tuple(player_id for player_id in players if player_id not in resolved)
    from_history = _history_assignments(unresolved, available, eligible_history)
    resolved.update(from_history)
    available -= {row.position for row in from_history.values()}

    if purpose is RolePurpose.OBSERVED_POSITION:
        unresolved = tuple(player_id for player_id in players if player_id not in resolved)
        from_target = _single_map_assignments(unresolved, available, eligible_target)
        resolved.update(from_target)

    input_hash = _input_hash(
        purpose=purpose,
        match_id=match_id,
        player_ids=players,
        target_started_at=target_started_at,
        cutoff=cutoff,
        roster=roster_for_hash,
        history=eligible_history,
        target=eligible_target if purpose is RolePurpose.OBSERVED_POSITION else (),
    )
    result = []
    for player_id in players:
        row = resolved.get(player_id)
        confidence = row.confidence if row is not None else 0.0
        result.append(
            RoleAssignment(
                match_id=match_id,
                player_id=player_id,
                purpose=purpose,
                position=row.position if row is not None else None,
                source=row.source if row is not None else RoleSource.UNKNOWN,
                confidence=confidence,
                cutoff=cutoff,
                input_hash=input_hash,
                version=ASSIGNMENT_VERSION,
                usable_for_role_dependent=(
                    row is not None and confidence >= ROLE_DEPENDENT_CONFIDENCE
                ),
                supporting_match_ids=(
                    row.supporting_match_ids if row is not None else ()
                ),
                reason=None if row is not None else "insufficient_as_of_evidence",
            )
        )
    return tuple(result)


def assign_observed_positions(
    *,
    match_id: int,
    target_started_at: datetime,
    cutoff: datetime,
    players: Sequence[SingleMapRoleEvidence],
    audited_roster: Iterable[AuditedRosterPosition] = (),
    history: Iterable[HistoricalPositionEvidence] = (),
) -> tuple[RoleAssignment, ...]:
    """Assign ex-post positions; only this API accepts target-map evidence."""
    target = tuple(players)
    player_ids = tuple(row.player_id for row in target)
    return _assign(
        purpose=RolePurpose.OBSERVED_POSITION,
        match_id=match_id,
        player_ids=player_ids,
        target_started_at=target_started_at,
        cutoff=cutoff,
        audited_roster=audited_roster,
        history=history,
        target_evidence=target,
    )


def assign_expected_positions(
    *,
    match_id: int,
    target_started_at: datetime,
    cutoff: datetime,
    player_ids: Sequence[int],
    audited_roster: Iterable[AuditedRosterPosition] = (),
    history: Iterable[HistoricalPositionEvidence] = (),
) -> tuple[RoleAssignment, ...]:
    """Assign pre-map positions from evidence available by the prediction cutoff."""
    return _assign(
        purpose=RolePurpose.EXPECTED_POSITION,
        match_id=match_id,
        player_ids=player_ids,
        target_started_at=target_started_at,
        cutoff=cutoff,
        audited_roster=audited_roster,
        history=history,
    )
