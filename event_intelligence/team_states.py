"""Reproducible, team-perspective map-state facts and labels."""

from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .raw_archive import canonical_json_bytes


LABEL_VERSION = "team-state-v1"
ANALYSIS_START_MINUTE = 10
END_BUFFER_MINUTES = 2
SUSTAINED_MINUTES = 3
PROFILE_THRESHOLDS = (3_000, 5_000, 10_000)

Number = int | float


class Side(str, Enum):
    RADIANT = "radiant"
    DIRE = "dire"


class TeamStateLabel(str, Enum):
    COMEBACK = "comeback"
    THROW = "throw"
    STOMP = "stomp"
    STOMP_LOSS = "stomp_loss"
    ADVANTAGE = "advantage"
    DISADVANTAGE = "disadvantage"
    EVEN = "even"
    UNSCORABLE = "state_unscorable"


@dataclass(frozen=True)
class TeamObjective:
    time_seconds: Number
    side: Side
    kind: str


@dataclass(frozen=True)
class CurveCrossing:
    minute: int
    from_band: str
    to_band: str


@dataclass(frozen=True)
class ThresholdFacts:
    threshold: int
    first_lead_at: int | None
    first_deficit_at: int | None

    @property
    def had_lead(self) -> bool:
        return self.first_lead_at is not None

    @property
    def had_deficit(self) -> bool:
        return self.first_deficit_at is not None


@dataclass(frozen=True)
class ObjectiveConversionFacts:
    source_complete: bool
    roshan_opportunity: bool | None
    first_roshan_at: int | None
    tower_after_roshan: bool | None
    tower_after_roshan_seconds: int | None
    high_ground_after_roshan: bool | None
    high_ground_after_roshan_seconds: int | None
    win_after_roshan: bool | None


@dataclass(frozen=True)
class TeamMapState:
    match_id: int
    team_id: int | None
    opponent_id: int | None
    side: Side
    won: bool | None
    label: TeamStateLabel
    unscorable_reason: str | None
    duration_seconds: int | None
    analysis_start_minute: int | None
    analysis_end_minute: int | None
    smoothed_curve: tuple[tuple[int, Number], ...]
    max_lead: Number | None
    max_deficit: Number | None
    ahead_fraction: float | None
    behind_fraction: float | None
    even_fraction: float | None
    signed_auc: float | None
    absolute_auc: float | None
    crossings: tuple[CurveCrossing, ...]
    first_significant_lead_at: int | None
    first_significant_deficit_at: int | None
    closeout_seconds: int | None
    thresholds: tuple[ThresholdFacts, ...]
    objective_conversion: ObjectiveConversionFacts
    curve_coverage: float
    source_versions: tuple[tuple[str, str], ...]
    input_hash: str
    label_version: str = LABEL_VERSION

    def threshold(self, value: int) -> ThresholdFacts:
        for facts in self.thresholds:
            if facts.threshold == value:
                return facts
        raise KeyError(value)


@dataclass(frozen=True)
class _CurveFacts:
    points: tuple[tuple[int, Number], ...]
    max_lead: Number
    max_deficit: Number
    ahead_fraction: float
    behind_fraction: float
    even_fraction: float
    signed_auc: float
    absolute_auc: float
    crossings: tuple[CurveCrossing, ...]
    first_lead_minute: int | None
    first_deficit_minute: int | None
    first_stomp_minute: int | None
    stomp_fraction: float
    thresholds: tuple[ThresholdFacts, ...]


def significant_threshold(minute: int) -> int:
    return max(3_000, 250 * minute)


def stomp_threshold(minute: int) -> int:
    return max(6_000, 400 * minute)


def _finite_number(value: Any) -> Number | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _duration(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _analysis_minutes(duration_seconds: int) -> tuple[int, ...]:
    end_minute = duration_seconds // 60 - END_BUFFER_MINUTES
    if end_minute < ANALYSIS_START_MINUTE:
        return ()
    return tuple(range(ANALYSIS_START_MINUTE, end_minute + 1))


def _curve_hash_values(values: Sequence[Any] | None) -> list[Any] | None:
    if values is None:
        return None
    result: list[Any] = []
    for value in values:
        number = _finite_number(value)
        if number is not None:
            result.append(number)
        elif value is None:
            result.append(None)
        else:
            result.append({"invalid_type": type(value).__name__})
    return result


def _normalize_versions(values: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(key), str(value)) for key, value in values.items()))


def _normalize_objectives(
    objectives: Iterable[TeamObjective] | None,
) -> tuple[TeamObjective, ...] | None:
    if objectives is None:
        return None
    normalized = []
    for objective in objectives:
        if not isinstance(objective, TeamObjective):
            raise TypeError("objectives must contain TeamObjective records")
        time_seconds = _finite_number(objective.time_seconds)
        if time_seconds is None:
            raise ValueError("objective time must be a finite number")
        side = Side(objective.side)
        kind = str(objective.kind).strip().lower()
        if kind not in {"roshan", "tower", "high_ground"}:
            raise ValueError(f"unsupported objective kind: {objective.kind!r}")
        normalized.append(TeamObjective(time_seconds, side, kind))
    return tuple(sorted(normalized, key=lambda row: (row.time_seconds, row.side.value, row.kind)))


def _input_hash(
    *,
    match_id: int,
    duration_seconds: Any,
    radiant_win: Any,
    radiant_team_id: int | None,
    dire_team_id: int | None,
    radiant_gold_adv: Sequence[Any] | None,
    objectives: tuple[TeamObjective, ...] | None,
    source_versions: tuple[tuple[str, str], ...],
) -> str:
    payload = {
        "version": LABEL_VERSION,
        "match_id": match_id,
        "duration_seconds": duration_seconds,
        "radiant_win": radiant_win if isinstance(radiant_win, bool) else None,
        "radiant_team_id": radiant_team_id,
        "dire_team_id": dire_team_id,
        "radiant_gold_adv": _curve_hash_values(radiant_gold_adv),
        "objectives": None
        if objectives is None
        else [
            (row.time_seconds, row.side.value, row.kind)
            for row in objectives
        ],
        "source_versions": source_versions,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _smooth_curve(
    values: Sequence[Any] | None,
    minutes: tuple[int, ...],
) -> tuple[tuple[tuple[int, Number], ...], float]:
    if not minutes:
        return (), 0.0
    required = tuple(range(minutes[0] - 1, minutes[-1] + 2))
    valid: dict[int, Number] = {}
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        for minute in required:
            if minute < len(values):
                number = _finite_number(values[minute])
                if number is not None:
                    valid[minute] = number
    coverage = len(valid) / len(required)
    if coverage < 1.0:
        return (), coverage
    return (
        tuple(
            (
                minute,
                statistics.median(
                    (valid[minute - 1], valid[minute], valid[minute + 1])
                ),
            )
            for minute in minutes
        ),
        coverage,
    )


def _sustained_mask(flags: Sequence[bool]) -> tuple[bool, ...]:
    result = [False] * len(flags)
    start = 0
    while start < len(flags):
        if not flags[start]:
            start += 1
            continue
        end = start + 1
        while end < len(flags) and flags[end]:
            end += 1
        if end - start >= SUSTAINED_MINUTES:
            result[start:end] = [True] * (end - start)
        start = end
    return tuple(result)


def _first_minute(points: Sequence[tuple[int, Number]], mask: Sequence[bool]) -> int | None:
    for (minute, _), selected in zip(points, mask):
        if selected:
            return minute
    return None


def _crossings(
    points: Sequence[tuple[int, Number]],
    ahead: Sequence[bool],
    behind: Sequence[bool],
) -> tuple[CurveCrossing, ...]:
    bands = tuple(
        "ahead" if is_ahead else "behind" if is_behind else "even"
        for is_ahead, is_behind in zip(ahead, behind)
    )
    return tuple(
        CurveCrossing(points[index][0], bands[index - 1], bands[index])
        for index in range(1, len(points))
        if bands[index] != bands[index - 1]
    )


def _threshold_facts(
    points: Sequence[tuple[int, Number]], threshold: int
) -> ThresholdFacts:
    lead = _sustained_mask(tuple(value >= threshold for _, value in points))
    deficit = _sustained_mask(tuple(value <= -threshold for _, value in points))
    lead_minute = _first_minute(points, lead)
    deficit_minute = _first_minute(points, deficit)
    return ThresholdFacts(
        threshold,
        None if lead_minute is None else lead_minute * 60,
        None if deficit_minute is None else deficit_minute * 60,
    )


def _auc(points: Sequence[tuple[int, Number]], *, absolute: bool) -> float:
    values = tuple(abs(value) if absolute else value for _, value in points)
    return float(sum((left + right) / 2 for left, right in zip(values, values[1:])))


def _curve_facts(points: tuple[tuple[int, Number], ...]) -> _CurveFacts:
    values = tuple(value for _, value in points)
    ahead = _sustained_mask(
        tuple(value >= significant_threshold(minute) for minute, value in points)
    )
    behind = _sustained_mask(
        tuple(value <= -significant_threshold(minute) for minute, value in points)
    )
    stomp = _sustained_mask(
        tuple(value >= stomp_threshold(minute) for minute, value in points)
    )
    count = len(points)
    first_lead = _first_minute(points, ahead)
    first_deficit = _first_minute(points, behind)
    return _CurveFacts(
        points=points,
        max_lead=max(0, max(values)),
        max_deficit=min(0, min(values)),
        ahead_fraction=sum(ahead) / count,
        behind_fraction=sum(behind) / count,
        even_fraction=(count - sum(ahead) - sum(behind)) / count,
        signed_auc=_auc(points, absolute=False),
        absolute_auc=_auc(points, absolute=True),
        crossings=_crossings(points, ahead, behind),
        first_lead_minute=first_lead,
        first_deficit_minute=first_deficit,
        first_stomp_minute=_first_minute(points, stomp),
        stomp_fraction=sum(stomp) / count,
        thresholds=tuple(_threshold_facts(points, value) for value in PROFILE_THRESHOLDS),
    )


def _objective_conversion(
    objectives: tuple[TeamObjective, ...] | None,
    side: Side,
    won: bool | None,
) -> ObjectiveConversionFacts:
    if objectives is None:
        return ObjectiveConversionFacts(False, None, None, None, None, None, None, None)
    own = tuple(row for row in objectives if row.side is side)
    roshans = tuple(row for row in own if row.kind == "roshan")
    if not roshans:
        return ObjectiveConversionFacts(True, False, None, None, None, None, None, None)
    first = roshans[0].time_seconds
    towers = tuple(
        row for row in own if row.kind in {"tower", "high_ground"} and row.time_seconds > first
    )
    high_ground = tuple(
        row for row in own if row.kind == "high_ground" and row.time_seconds > first
    )
    first_tower = towers[0].time_seconds if towers else None
    first_high_ground = high_ground[0].time_seconds if high_ground else None
    return ObjectiveConversionFacts(
        source_complete=True,
        roshan_opportunity=True,
        first_roshan_at=int(first),
        tower_after_roshan=bool(towers),
        tower_after_roshan_seconds=(
            None if first_tower is None else int(first_tower - first)
        ),
        high_ground_after_roshan=bool(high_ground),
        high_ground_after_roshan_seconds=(
            None if first_high_ground is None else int(first_high_ground - first)
        ),
        win_after_roshan=won,
    )


def _stomp(facts: _CurveFacts) -> bool:
    return (
        facts.first_stomp_minute is not None
        and facts.first_stomp_minute < 20
        and facts.stomp_fraction >= 0.60
        and facts.first_deficit_minute is None
    )


def _labels(
    radiant: _CurveFacts,
    dire: _CurveFacts,
    radiant_win: bool,
) -> tuple[TeamStateLabel, TeamStateLabel]:
    winner, loser = (radiant, dire) if radiant_win else (dire, radiant)
    if winner.first_deficit_minute is not None:
        winner_label = TeamStateLabel.COMEBACK
        loser_label = TeamStateLabel.THROW
    elif _stomp(winner):
        winner_label = TeamStateLabel.STOMP
        loser_label = TeamStateLabel.STOMP_LOSS
    else:
        winner_label = (
            TeamStateLabel.ADVANTAGE
            if winner.ahead_fraction >= 0.25 and winner.first_deficit_minute is None
            else TeamStateLabel.EVEN
        )
        loser_label = (
            TeamStateLabel.DISADVANTAGE
            if loser.behind_fraction >= 0.25 and loser.first_lead_minute is None
            else TeamStateLabel.EVEN
        )
    return (winner_label, loser_label) if radiant_win else (loser_label, winner_label)


def _state(
    *,
    match_id: int,
    team_id: int | None,
    opponent_id: int | None,
    side: Side,
    won: bool | None,
    label: TeamStateLabel,
    reason: str | None,
    duration_seconds: int | None,
    facts: _CurveFacts | None,
    curve_coverage: float,
    objectives: tuple[TeamObjective, ...] | None,
    source_versions: tuple[tuple[str, str], ...],
    input_hash: str,
) -> TeamMapState:
    first_lead_at = (
        None if facts is None or facts.first_lead_minute is None else facts.first_lead_minute * 60
    )
    first_deficit_at = (
        None
        if facts is None or facts.first_deficit_minute is None
        else facts.first_deficit_minute * 60
    )
    return TeamMapState(
        match_id=match_id,
        team_id=team_id,
        opponent_id=opponent_id,
        side=side,
        won=won,
        label=label,
        unscorable_reason=reason,
        duration_seconds=duration_seconds,
        analysis_start_minute=(None if facts is None else facts.points[0][0]),
        analysis_end_minute=(None if facts is None else facts.points[-1][0]),
        smoothed_curve=(() if facts is None else facts.points),
        max_lead=(None if facts is None else facts.max_lead),
        max_deficit=(None if facts is None else facts.max_deficit),
        ahead_fraction=(None if facts is None else facts.ahead_fraction),
        behind_fraction=(None if facts is None else facts.behind_fraction),
        even_fraction=(None if facts is None else facts.even_fraction),
        signed_auc=(None if facts is None else facts.signed_auc),
        absolute_auc=(None if facts is None else facts.absolute_auc),
        crossings=(() if facts is None else facts.crossings),
        first_significant_lead_at=first_lead_at,
        first_significant_deficit_at=first_deficit_at,
        closeout_seconds=(
            None
            if duration_seconds is None or first_lead_at is None
            else max(0, duration_seconds - first_lead_at)
        ),
        thresholds=(
            tuple(ThresholdFacts(value, None, None) for value in PROFILE_THRESHOLDS)
            if facts is None
            else facts.thresholds
        ),
        objective_conversion=_objective_conversion(objectives, side, won),
        curve_coverage=curve_coverage,
        source_versions=source_versions,
        input_hash=input_hash,
    )


def build_team_map_states(
    *,
    match_id: int,
    duration_seconds: int | None,
    radiant_win: bool | None,
    radiant_team_id: int | None,
    dire_team_id: int | None,
    radiant_gold_adv: Sequence[Any] | None,
    objectives: Iterable[TeamObjective] | None = None,
    source_versions: Mapping[str, Any] | None = None,
) -> tuple[TeamMapState, TeamMapState]:
    """Build one audited state row per team from a Radiant gold curve."""
    if isinstance(match_id, bool) or not isinstance(match_id, int) or match_id <= 0:
        raise ValueError("match_id must be a positive integer")
    normalized_objectives = _normalize_objectives(objectives)
    versions = _normalize_versions(source_versions or {})
    input_hash = _input_hash(
        match_id=match_id,
        duration_seconds=duration_seconds,
        radiant_win=radiant_win,
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
        radiant_gold_adv=radiant_gold_adv,
        objectives=normalized_objectives,
        source_versions=versions,
    )
    duration = _duration(duration_seconds)
    minutes = () if duration is None else _analysis_minutes(duration)
    radiant_points, coverage = _smooth_curve(radiant_gold_adv, minutes)
    curve_reason = None
    if duration is None:
        curve_reason = "invalid_duration"
    elif len(minutes) < SUSTAINED_MINUTES:
        curve_reason = "analysis_window_too_short"
    elif coverage < 1.0:
        curve_reason = "gold_timeline_incomplete"

    radiant_facts = None if curve_reason is not None else _curve_facts(radiant_points)
    dire_facts = (
        None
        if radiant_facts is None
        else _curve_facts(tuple((minute, -value) for minute, value in radiant_points))
    )
    winner_known = isinstance(radiant_win, bool)
    reason = curve_reason if curve_reason is not None else None if winner_known else "result_missing"
    if radiant_facts is None or dire_facts is None or not winner_known:
        labels = (TeamStateLabel.UNSCORABLE, TeamStateLabel.UNSCORABLE)
    else:
        labels = _labels(radiant_facts, dire_facts, radiant_win)

    radiant_won = radiant_win if winner_known else None
    dire_won = not radiant_win if winner_known else None
    return (
        _state(
            match_id=match_id,
            team_id=radiant_team_id,
            opponent_id=dire_team_id,
            side=Side.RADIANT,
            won=radiant_won,
            label=labels[0],
            reason=reason,
            duration_seconds=duration,
            facts=radiant_facts,
            curve_coverage=coverage,
            objectives=normalized_objectives,
            source_versions=versions,
            input_hash=input_hash,
        ),
        _state(
            match_id=match_id,
            team_id=dire_team_id,
            opponent_id=radiant_team_id,
            side=Side.DIRE,
            won=dire_won,
            label=labels[1],
            reason=reason,
            duration_seconds=duration,
            facts=dire_facts,
            curve_coverage=coverage,
            objectives=normalized_objectives,
            source_versions=versions,
            input_hash=input_hash,
        ),
    )
