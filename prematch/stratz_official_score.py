"""Pure normalizer and scorer for the frozen STRATZ official R.O.S.H. profile."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import prematch.stratz_official_profile as official_profile
from prematch.stratz_official_profile import (
    RoshParityProfile,
    RoshRequestPlan,
    canonical_bytes,
    validate_draft,
)


POSITION_RELIABILITY_COUNT = 1000
SYNERGY_RELIABILITY_COUNT = 100
TIME_RANK_FALLBACK_COUNT = 1000
ALL_RANK_FALLBACK = "ALL_RANK_FALLBACK"
DIVINE_IMMORTAL = "DIVINE_IMMORTAL"


class ScoreError(ValueError):
    """Raised when official inputs cannot be scored without guessing."""


@dataclass(frozen=True, order=True)
class DraftSlot:
    team_side: str
    position_id: int
    hero_id: int


@dataclass(frozen=True, order=True)
class PositionAggregate:
    hero_id: int
    position_id: int
    win_count: int
    match_count: int


@dataclass(frozen=True, order=True)
class SynergySample:
    week_index: int
    relation: str
    hero_id: int
    other_hero_id: int
    synergy: float
    match_count: int


@dataclass(frozen=True, order=True)
class TimeAggregate:
    hero_id: int
    position_id: int
    minute: int
    window_win_count: int
    window_match_count: int
    bucket_match_count: int


@dataclass(frozen=True)
class NormalizedRoshInputs:
    draft: tuple[DraftSlot, ...]
    position_stats: tuple[PositionAggregate, ...]
    synergy_samples: tuple[SynergySample, ...]
    all_rank_time_stats: tuple[TimeAggregate, ...]
    rank_time_stats: tuple[TimeAggregate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "draft", tuple(sorted(self.draft, key=_draft_key)))
        object.__setattr__(self, "position_stats", tuple(sorted(self.position_stats)))
        object.__setattr__(self, "synergy_samples", tuple(sorted(self.synergy_samples)))
        object.__setattr__(self, "all_rank_time_stats", tuple(sorted(self.all_rank_time_stats)))
        object.__setattr__(self, "rank_time_stats", tuple(sorted(self.rank_time_stats)))


@dataclass(frozen=True)
class HeroScore:
    team_side: str
    hero_id: int
    position_id: int
    position_base_diff: float
    same_team_synergy: float
    opponent_matchup_synergy: float
    raw_score: float
    display_score: float

    def projection(self) -> dict[str, Any]:
        return {
            "team_side": self.team_side,
            "hero_id": self.hero_id,
            "position_id": self.position_id,
            "position_base_diff": self.position_base_diff,
            "same_team_synergy": self.same_team_synergy,
            "opponent_matchup_synergy": self.opponent_matchup_synergy,
            "raw_score": self.raw_score,
            "display_score": self.display_score,
        }


@dataclass(frozen=True)
class MinuteSlot:
    team_side: str
    hero_id: int
    position_id: int
    source: str
    match_count: int
    win_rate_diff: float

    def projection(self) -> dict[str, Any]:
        return {
            "team_side": self.team_side,
            "hero_id": self.hero_id,
            "position_id": self.position_id,
            "source": self.source,
            "match_count": self.match_count,
            "win_rate_diff": self.win_rate_diff,
        }


@dataclass(frozen=True)
class MinutePoint:
    minute: int
    radiant_time_delta: float
    dire_time_delta: float
    synergy_delta: float
    raw_score: float
    display_score: float
    rank_source_counts: Mapping[str, int]
    slots: tuple[MinuteSlot, ...]

    def __post_init__(self) -> None:
        counts = {
            DIVINE_IMMORTAL: int(self.rank_source_counts.get(DIVINE_IMMORTAL, 0)),
            ALL_RANK_FALLBACK: int(self.rank_source_counts.get(ALL_RANK_FALLBACK, 0)),
        }
        object.__setattr__(self, "rank_source_counts", MappingProxyType(counts))
        object.__setattr__(self, "slots", tuple(sorted(self.slots, key=_minute_slot_key)))

    def projection(self) -> dict[str, Any]:
        return {
            "minute": self.minute,
            "radiant_time_delta": self.radiant_time_delta,
            "dire_time_delta": self.dire_time_delta,
            "synergy_delta": self.synergy_delta,
            "raw_score": self.raw_score,
            "display_score": self.display_score,
            "rank_source_counts": dict(self.rank_source_counts),
            "slots": [slot.projection() for slot in self.slots],
        }


@dataclass(frozen=True)
class OfficialRoshResult:
    formula_version: str
    radiant_team_raw_score: float
    radiant_team_score: float
    dire_team_raw_score: float
    dire_team_score: float
    relative_advantage_raw: float
    relative_advantage: float
    hero_scores: tuple[HeroScore, ...]
    minute_points: tuple[MinutePoint, ...]
    result_hash: str


def _draft_key(slot: DraftSlot) -> tuple[int, int]:
    return (0 if slot.team_side == "RADIANT" else 1, slot.position_id)


def _minute_slot_key(slot: MinuteSlot) -> tuple[int, int]:
    return (0 if slot.team_side == "RADIANT" else 1, slot.position_id)


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ScoreError(f"{field} must be an integer >= {minimum}")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoreError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ScoreError(f"{field} must be a finite number")
    return result


def _count_pair(row: Mapping[str, Any], context: str) -> tuple[int, int]:
    match_count = _integer(row.get("matchCount"), f"{context}.matchCount")
    win_count = _integer(row.get("winCount"), f"{context}.winCount")
    if win_count > match_count:
        raise ScoreError(f"{context}.winCount exceeds matchCount")
    return win_count, match_count


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScoreError(f"{context} must be an object")
    return value


def _array(value: Any, context: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ScoreError(f"{context} must be an array")
    return value


def profile_round(value: float) -> float:
    """Return Number(value.toFixed(1)) using the exact binary float value."""

    number = _number(value, "rounding value")
    if abs(number) >= 1e21:
        return number
    magnitude = Decimal.from_float(abs(number)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    rounded = float(magnitude)
    return math.copysign(rounded, number)


def position_base_diff(match_count: int, win_count: int) -> float:
    matches = _integer(match_count, "position match_count")
    wins = _integer(win_count, "position win_count")
    if matches == 0:
        raise ScoreError("position match_count must be positive")
    if wins > matches:
        raise ScoreError("position win_count exceeds match_count")
    result = (wins / matches * 100.0 - 50.0) * min(matches / POSITION_RELIABILITY_COUNT, 1.0)
    return _finite(result, "position_base_diff")


def _pair_round(value: float) -> float:
    """The captured bundle normalizes each aggregated pair to two decimals."""

    scaled = _number(value, "pair synergy") * 100.0
    lower = math.floor(scaled)
    rounded = lower if scaled - lower < 0.5 else lower + 1
    result = float(rounded) / 100.0
    if rounded == 0 and math.copysign(1.0, scaled) < 0:
        result = -0.0
    return _finite(result, "pair synergy")


def _aggregate_pair_synergy(samples: Sequence[tuple[float, int]]) -> tuple[float, int]:
    """Return the bundle pair value and the full newest-first sample count."""

    pair_synergy = 0.0
    cumulative_count = 0
    for synergy, match_count in samples:
        if cumulative_count >= SYNERGY_RELIABILITY_COUNT:
            break
        value = _number(synergy, "synergy")
        count = _integer(match_count, "synergy match_count")
        combined_count = cumulative_count + count
        if combined_count:
            pair_synergy = _pair_round(
                pair_synergy * (cumulative_count / combined_count)
                + value * (count / combined_count)
            )
        cumulative_count = combined_count
    if cumulative_count < SYNERGY_RELIABILITY_COUNT:
        pair_synergy = _pair_round(
            pair_synergy * cumulative_count / SYNERGY_RELIABILITY_COUNT
        )
    return pair_synergy, cumulative_count


def aggregate_pair_synergy(samples: Sequence[tuple[float, int]]) -> float:
    """Aggregate newest-to-oldest whole-week samples using bundle semantics."""

    return _aggregate_pair_synergy(samples)[0]


def normalize_official_responses(
    plan: RoshRequestPlan,
    responses: Sequence[Mapping[str, Any]],
) -> NormalizedRoshInputs:
    """Validate and project one complete official GraphQL batch."""

    if not isinstance(plan, RoshRequestPlan):
        raise ScoreError("plan must be a RoshRequestPlan")
    validator = getattr(official_profile, "validate_canonical_request_plan", None)
    if not callable(validator):
        raise ScoreError("canonical request plan validator is unavailable")
    try:
        validator(plan)
    except ValueError as exc:
        raise ScoreError("request plan failed canonical validation") from exc
    expected = (
        ("GetMatchPicksBans", "HeroesMetaPositions", "GetMatchCountPreviousWeekDay", "Synergy", "GetHeroStatsByTime", "GetHeroStatsByTime")
        if plan.analysis_input.mode == "historical_match"
        else ("HeroesMetaPositions", "GetMatchCountPreviousWeekDay", "Synergy", "GetHeroStatsByTime", "GetHeroStatsByTime")
    )
    operation_names = tuple(operation.operation_name for operation in plan.operations)
    if operation_names != expected or tuple(operation.index for operation in plan.operations) != tuple(range(len(expected))):
        raise ScoreError("request plan operation identity is incomplete")
    batch = _array(responses, "responses")
    if len(batch) != len(plan.operations):
        raise ScoreError("response batch does not match request plan")

    paired: list[tuple[Any, Mapping[str, Any]]] = []
    for operation, raw in zip(plan.operations, batch, strict=True):
        response = _mapping(raw, f"{operation.operation_name} response")
        if "errors" in response:
            errors = response["errors"]
            if errors is None or not isinstance(errors, Sequence) or isinstance(errors, (str, bytes)) or len(errors):
                raise ScoreError(f"{operation.operation_name} returned GraphQL errors")
        data = _mapping(response.get("data"), f"{operation.operation_name}.data")
        paired.append((operation, data))

    offset = 0
    if plan.analysis_input.mode == "historical_match":
        draft = _historical_draft(plan, paired[0][1])
        offset = 1
    else:
        draft = _explicit_draft(plan)

    position_stats = _normalize_positions(paired[offset][1], draft)
    _validate_match_counts(paired[offset + 1][1])
    synergy_samples = _normalize_synergy(paired[offset + 2][1], draft)

    all_rank: tuple[TimeAggregate, ...] | None = None
    rank: tuple[TimeAggregate, ...] | None = None
    for operation, data in paired[offset + 3 : offset + 5]:
        bracket = operation.variables.get("bracketBasicIds")
        if bracket is None:
            if all_rank is not None:
                raise ScoreError("duplicate all-rank time operation")
            all_rank = _normalize_time(data, draft)
        elif bracket == DIVINE_IMMORTAL:
            if rank is not None:
                raise ScoreError("duplicate rank time operation")
            rank = _normalize_time(data, draft)
        else:
            raise ScoreError("unexpected time operation rank identity")
    if all_rank is None or rank is None:
        raise ScoreError("both time operations are required")

    return NormalizedRoshInputs(draft, position_stats, synergy_samples, all_rank, rank)


def _historical_draft(plan: RoshRequestPlan, data: Mapping[str, Any]) -> tuple[DraftSlot, ...]:
    match = _mapping(data.get("match"), "GetMatchPicksBans.match")
    match_id = _integer(match.get("id"), "match.id", minimum=1)
    if match_id != plan.analysis_input.match_id:
        raise ScoreError("source match identity mismatch")
    end_date_time = _integer(match.get("endDateTime"), "match.endDateTime", minimum=1)
    if end_date_time != plan.analysis_input.date_time:
        raise ScoreError("source match date_time mismatch")

    picked_sides: dict[int, str] = {}
    for index, raw in enumerate(_array(match.get("pickBans"), "match.pickBans")):
        row = _mapping(raw, f"match.pickBans[{index}]")
        is_pick = row.get("isPick")
        is_radiant = row.get("isRadiant")
        if not isinstance(is_pick, bool) or not isinstance(is_radiant, bool):
            raise ScoreError("pick/ban flags must be booleans")
        if not is_pick:
            continue
        hero_id = _integer(row.get("heroId"), "pick heroId", minimum=1)
        if hero_id in picked_sides:
            raise ScoreError("draft contains a duplicate picked hero")
        picked_sides[hero_id] = "RADIANT" if is_radiant else "DIRE"

    slots: list[DraftSlot] = []
    seen_players: set[int] = set()
    for index, raw in enumerate(_array(match.get("players"), "match.players")):
        row = _mapping(raw, f"match.players[{index}]")
        hero_id = _integer(row.get("heroId"), "player heroId", minimum=1)
        if hero_id in seen_players or hero_id not in picked_sides:
            raise ScoreError("players do not identify one complete draft")
        seen_players.add(hero_id)
        position = row.get("position")
        if not isinstance(position, str) or not position.startswith("POSITION_"):
            raise ScoreError("player position is invalid")
        try:
            position_id = int(position.removeprefix("POSITION_"))
        except ValueError as exc:
            raise ScoreError("player position is invalid") from exc
        slots.append(DraftSlot(picked_sides[hero_id], position_id, hero_id))
    if seen_players != set(picked_sides):
        raise ScoreError("players and picked heroes do not match")
    return _validate_slots(slots)


def _explicit_draft(plan: RoshRequestPlan) -> tuple[DraftSlot, ...]:
    slots = [
        DraftSlot(side, int(row.get("position_id", row.get("positionId"))), int(row.get("hero_id", row.get("heroId"))))
        for side, rows in (("RADIANT", plan.analysis_input.radiant), ("DIRE", plan.analysis_input.dire))
        for row in rows
    ]
    return _validate_slots(slots)


def _validate_slots(slots: Sequence[DraftSlot]) -> tuple[DraftSlot, ...]:
    if len(slots) != 10:
        raise ScoreError("draft must contain exactly ten slots")
    if any(slot.team_side not in {"RADIANT", "DIRE"} for slot in slots):
        raise ScoreError("draft side must be RADIANT or DIRE")
    radiant = [{"hero_id": slot.hero_id, "position_id": slot.position_id} for slot in slots if slot.team_side == "RADIANT"]
    dire = [{"hero_id": slot.hero_id, "position_id": slot.position_id} for slot in slots if slot.team_side == "DIRE"]
    try:
        validate_draft(radiant, dire)
    except ValueError as exc:
        raise ScoreError("draft is incomplete or duplicated") from exc
    return tuple(sorted(slots, key=_draft_key))


def _normalize_positions(data: Mapping[str, Any], draft: Sequence[DraftSlot]) -> tuple[PositionAggregate, ...]:
    hero_stats = _mapping(data.get("heroStats"), "HeroesMetaPositions.heroStats")
    target = {(slot.hero_id, slot.position_id) for slot in draft}
    grouped: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    for position_id in range(1, 6):
        field = f"heroesPos_{position_id}"
        seen: set[tuple[int, int]] = set()
        for index, raw in enumerate(_array(hero_stats.get(field), field)):
            row = _mapping(raw, f"{field}[{index}]")
            hero_id = _integer(row.get("heroId"), f"{field}.heroId", minimum=1)
            timestamp = _integer(row.get("timestamp"), f"{field}.timestamp", minimum=1)
            win_count, match_count = _count_pair(row, field)
            identity = (hero_id, timestamp)
            if identity in seen:
                raise ScoreError(f"{field} contains a duplicate hero/day")
            seen.add(identity)
            if (hero_id, position_id) in target:
                grouped.setdefault((hero_id, position_id), []).append((timestamp, win_count, match_count))
    _validate_overall_position_rows(hero_stats)

    result: list[PositionAggregate] = []
    for hero_id, position_id in sorted(target):
        rows = sorted(grouped.get((hero_id, position_id), ()), reverse=True)
        wins = 0
        matches = 0
        for _, win_count, match_count in rows:
            if matches >= POSITION_RELIABILITY_COUNT:
                break
            wins += win_count
            matches += match_count
        position_base_diff(matches, wins)
        result.append(PositionAggregate(hero_id, position_id, wins, matches))
    return tuple(result)


def _validate_overall_position_rows(hero_stats: Mapping[str, Any]) -> None:
    seen: set[tuple[int, int]] = set()
    for index, raw in enumerate(_array(hero_stats.get("heroes"), "heroes")):
        row = _mapping(raw, f"heroes[{index}]")
        hero_id = _integer(row.get("heroId"), "heroes.heroId", minimum=1)
        timestamp = _integer(row.get("timestamp"), "heroes.timestamp", minimum=1)
        _count_pair(row, "heroes")
        identity = (hero_id, timestamp)
        if identity in seen:
            raise ScoreError("heroes contains a duplicate hero/day")
        seen.add(identity)


def _validate_match_counts(data: Mapping[str, Any]) -> None:
    matches = _mapping(_mapping(_mapping(data.get("stratz"), "stratz").get("page"), "stratz.page").get("matches"), "stratz.page.matches")
    for field, timestamp_field in (("matchesStatsDay", "day"), ("matchesStatsWeek", "week")):
        seen: set[int] = set()
        for index, raw in enumerate(_array(matches.get(field), field)):
            row = _mapping(raw, f"{field}[{index}]")
            timestamp = _integer(row.get(timestamp_field), f"{field}.{timestamp_field}", minimum=1)
            _integer(row.get("matchCount"), f"{field}.matchCount")
            if timestamp in seen:
                raise ScoreError(f"{field} contains a duplicate timestamp")
            seen.add(timestamp)


def _normalize_synergy(data: Mapping[str, Any], draft: Sequence[DraftSlot]) -> tuple[SynergySample, ...]:
    hero_stats = _mapping(data.get("heroStats"), "Synergy.heroStats")
    target_ids = {slot.hero_id for slot in draft}
    result: list[SynergySample] = []
    for week_index in range(1, 5):
        field = f"matchUp_Prev_Week_{week_index}"
        seen_heroes: set[int] = set()
        for row_index, raw in enumerate(_array(hero_stats.get(field), field)):
            row = _mapping(raw, f"{field}[{row_index}]")
            hero_id = _integer(row.get("heroId"), f"{field}.heroId")
            if hero_id in seen_heroes:
                raise ScoreError(f"{field} contains a duplicate hero")
            seen_heroes.add(hero_id)
            for relation in ("with", "vs"):
                seen_pairs: set[int] = set()
                for pair_index, raw_pair in enumerate(_array(row.get(relation), f"{field}.{relation}")):
                    pair = _mapping(raw_pair, f"{field}.{relation}[{pair_index}]")
                    other_id = _integer(pair.get("heroId2"), "synergy.heroId2", minimum=1)
                    if other_id in seen_pairs:
                        raise ScoreError(f"{field}.{relation} contains a duplicate pair")
                    seen_pairs.add(other_id)
                    synergy = _number(pair.get("synergy"), "synergy.synergy")
                    match_count = _integer(pair.get("matchCount"), "synergy.matchCount")
                    if hero_id in target_ids and other_id in target_ids:
                        result.append(SynergySample(week_index, relation, hero_id, other_id, synergy, match_count))
            if hero_id == 0 and (row.get("with") or row.get("vs")):
                raise ScoreError("sentinel synergy row must be empty")
    return tuple(result)


def _normalize_time(data: Mapping[str, Any], draft: Sequence[DraftSlot]) -> tuple[TimeAggregate, ...]:
    hero_stats = _mapping(data.get("heroStats"), "GetHeroStatsByTime.heroStats")
    target = {(slot.hero_id, slot.position_id) for slot in draft}
    result: list[TimeAggregate] = []
    for position_id in range(1, 6):
        field = f"heroStatsByTime_{position_id}"
        by_hero: dict[int, list[tuple[int, int, int]]] = {}
        seen: set[tuple[int, int]] = set()
        for index, raw in enumerate(_array(hero_stats.get(field), field)):
            row = _mapping(raw, f"{field}[{index}]")
            hero_id = _integer(row.get("heroId"), f"{field}.heroId", minimum=1)
            minute = _integer(row.get("time"), f"{field}.time")
            win_count, match_count = _count_pair(row, field)
            identity = (hero_id, minute)
            if identity in seen:
                raise ScoreError(f"{field} contains a duplicate hero/minute")
            seen.add(identity)
            by_hero.setdefault(hero_id, []).append((minute, win_count, match_count))

        for hero_id, rows in by_hero.items():
            rows.sort()
            buckets: list[tuple[int, int, int]] = []
            for index, (minute, wins, matches) in enumerate(rows):
                if index + 1 < len(rows):
                    _, next_wins, next_matches = rows[index + 1]
                    bucket_wins = wins - next_wins
                    bucket_matches = matches - next_matches
                else:
                    bucket_wins = wins
                    bucket_matches = matches
                if bucket_matches < 0 or bucket_wins < 0 or bucket_wins > bucket_matches:
                    raise ScoreError(f"{field} cumulative counts are invalid")
                buckets.append((minute, bucket_wins, bucket_matches))
            if (hero_id, position_id) not in target:
                continue
            for index, (minute, _, bucket_matches) in enumerate(buckets):
                window = buckets[max(0, index - 1) : index + 2]
                window_wins = sum(row[1] for row in window)
                window_matches = sum(row[2] for row in window)
                if window_matches == 0:
                    continue
                if window_wins > window_matches:
                    raise ScoreError(f"{field} window winCount exceeds matchCount")
                result.append(TimeAggregate(hero_id, position_id, minute, window_wins, window_matches, bucket_matches))
    return tuple(result)


def score_official_rosh(inputs: NormalizedRoshInputs, profile: RoshParityProfile) -> OfficialRoshResult:
    """Score normalized official inputs without I/O, time, or environment state."""

    if not isinstance(inputs, NormalizedRoshInputs):
        raise ScoreError("inputs must be NormalizedRoshInputs")
    validator = getattr(official_profile, "validate_active_profile", None)
    if not callable(validator):
        raise ScoreError("active profile validator is unavailable")
    try:
        validator(profile)
    except ValueError as exc:
        raise ScoreError("profile is not active for scoring") from exc
    draft = _validate_slots(inputs.draft)

    position_by_slot = {(row.hero_id, row.position_id): row for row in inputs.position_stats}
    if len(position_by_slot) != len(inputs.position_stats):
        raise ScoreError("duplicate normalized position identity")
    synergy_by_pair: dict[tuple[str, int, int], list[tuple[int, float, int]]] = {}
    for sample in inputs.synergy_samples:
        if sample.week_index not in range(1, 5) or sample.relation not in {"with", "vs"}:
            raise ScoreError("invalid normalized synergy identity")
        key = (sample.relation, sample.hero_id, sample.other_hero_id)
        synergy_by_pair.setdefault(key, []).append((sample.week_index, sample.synergy, sample.match_count))
    pair_values: dict[tuple[str, int, int], float] = {}
    for key, samples in synergy_by_pair.items():
        if len({sample[0] for sample in samples}) != len(samples):
            raise ScoreError("duplicate normalized synergy week")
        pair_values[key] = aggregate_pair_synergy([(value, count) for _, value, count in sorted(samples)])

    team_slots = {
        side: tuple(slot for slot in draft if slot.team_side == side)
        for side in ("RADIANT", "DIRE")
    }
    hero_scores: list[HeroScore] = []
    team_synergy: dict[str, float] = {"RADIANT": 0.0, "DIRE": 0.0}
    for side in ("RADIANT", "DIRE"):
        opponent = "DIRE" if side == "RADIANT" else "RADIANT"
        for slot in team_slots[side]:
            position = position_by_slot.get((slot.hero_id, slot.position_id))
            if position is None:
                raise ScoreError("position input is missing for a draft slot")
            base = position_base_diff(position.match_count, position.win_count)
            same_team = sum(
                pair_values.get(("with", slot.hero_id, teammate.hero_id), 0.0)
                for teammate in team_slots[side]
                if teammate.hero_id != slot.hero_id
            )
            matchup = sum(
                pair_values.get(("vs", slot.hero_id, enemy.hero_id), 0.0)
                for enemy in team_slots[opponent]
            )
            raw_score = _finite(base + same_team + matchup, "hero raw_score")
            team_synergy[side] += same_team + matchup
            hero_scores.append(
                HeroScore(side, slot.hero_id, slot.position_id, base, same_team, matchup, raw_score, profile_round(raw_score))
            )
    hero_scores.sort(key=lambda row: (0 if row.team_side == "RADIANT" else 1, row.position_id))

    radiant_raw = _finite(sum(row.raw_score for row in hero_scores if row.team_side == "RADIANT"), "radiant team score")
    dire_raw = _finite(sum(row.raw_score for row in hero_scores if row.team_side == "DIRE"), "dire team score")
    relative_raw = _finite(radiant_raw - dire_raw, "relative advantage")
    synergy_delta = _finite(team_synergy["RADIANT"] - team_synergy["DIRE"], "synergy delta")
    minute_points = _score_minutes(inputs, draft, synergy_delta)

    partial = OfficialRoshResult(
        profile.formula_version,
        radiant_raw,
        profile_round(radiant_raw),
        dire_raw,
        profile_round(dire_raw),
        relative_raw,
        profile_round(relative_raw),
        tuple(hero_scores),
        minute_points,
        "",
    )
    projection = result_projection(partial)
    _validate_finite_json(projection)
    result_hash = hashlib.sha256(canonical_bytes(projection)).hexdigest()
    return OfficialRoshResult(
        partial.formula_version,
        partial.radiant_team_raw_score,
        partial.radiant_team_score,
        partial.dire_team_raw_score,
        partial.dire_team_score,
        partial.relative_advantage_raw,
        partial.relative_advantage,
        partial.hero_scores,
        partial.minute_points,
        result_hash,
    )


def _score_minutes(
    inputs: NormalizedRoshInputs,
    draft: Sequence[DraftSlot],
    synergy_delta: float,
) -> tuple[MinutePoint, ...]:
    all_rank = _time_index(inputs.all_rank_time_stats, "all-rank")
    rank = _time_index(inputs.rank_time_stats, "rank")
    minutes = sorted({key[2] for key in all_rank} | {key[2] for key in rank})
    points: list[MinutePoint] = []
    for minute in minutes:
        slots: list[MinuteSlot] = []
        complete = True
        for draft_slot in draft:
            key = (draft_slot.hero_id, draft_slot.position_id, minute)
            rank_value = rank.get(key)
            if rank_value is not None and rank_value.bucket_match_count >= TIME_RANK_FALLBACK_COUNT:
                selected = rank_value
                source = DIVINE_IMMORTAL
            else:
                selected = all_rank.get(key)
                source = ALL_RANK_FALLBACK
            if selected is None or selected.window_match_count == 0:
                complete = False
                break
            diff = _finite(selected.window_win_count / selected.window_match_count * 100.0 - 50.0, "time win_rate_diff")
            slots.append(MinuteSlot(draft_slot.team_side, draft_slot.hero_id, draft_slot.position_id, source, selected.bucket_match_count, diff))
        if not complete:
            continue
        radiant_delta = _finite(sum(slot.win_rate_diff for slot in slots if slot.team_side == "RADIANT"), "radiant time delta")
        dire_delta = _finite(sum(slot.win_rate_diff for slot in slots if slot.team_side == "DIRE"), "dire time delta")
        raw_score = _finite((radiant_delta - dire_delta) / 10.0 + synergy_delta, "minute raw_score")
        counts = {
            DIVINE_IMMORTAL: sum(slot.source == DIVINE_IMMORTAL for slot in slots),
            ALL_RANK_FALLBACK: sum(slot.source == ALL_RANK_FALLBACK for slot in slots),
        }
        points.append(
            MinutePoint(minute, radiant_delta, dire_delta, synergy_delta, raw_score, profile_round(raw_score), counts, tuple(slots))
        )
    return tuple(points)


def _time_index(rows: Sequence[TimeAggregate], name: str) -> dict[tuple[int, int, int], TimeAggregate]:
    result: dict[tuple[int, int, int], TimeAggregate] = {}
    for row in rows:
        _integer(row.hero_id, f"{name} hero_id", minimum=1)
        if row.position_id not in range(1, 6):
            raise ScoreError(f"{name} position_id is invalid")
        _integer(row.minute, f"{name} minute")
        wins = _integer(row.window_win_count, f"{name} window_win_count")
        matches = _integer(row.window_match_count, f"{name} window_match_count")
        _integer(row.bucket_match_count, f"{name} bucket_match_count")
        if wins > matches:
            raise ScoreError(f"{name} win_count exceeds match_count")
        key = (row.hero_id, row.position_id, row.minute)
        if key in result:
            raise ScoreError(f"duplicate {name} time identity")
        result[key] = row
    return result


def result_projection(result: OfficialRoshResult) -> dict[str, Any]:
    return {
        "schema": "stratz-official-rosh-result/v1",
        "formula_version": result.formula_version,
        "radiant_team": {
            "raw_score": result.radiant_team_raw_score,
            "display_score": result.radiant_team_score,
        },
        "dire_team": {
            "raw_score": result.dire_team_raw_score,
            "display_score": result.dire_team_score,
        },
        "relative_advantage": {
            "raw_score": result.relative_advantage_raw,
            "display_score": result.relative_advantage,
        },
        "hero_scores": [row.projection() for row in result.hero_scores],
        "minute_points": [row.projection() for row in result.minute_points],
    }


def _finite(value: float, field: str) -> float:
    if not math.isfinite(value):
        raise ScoreError(f"{field} is not finite")
    return value


def _validate_finite_json(value: Any) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _validate_finite_json(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_finite_json(item)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ScoreError("result contains a non-finite number")
