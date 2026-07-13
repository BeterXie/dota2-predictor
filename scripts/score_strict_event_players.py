"""Score source-exact player facts for strictly eligible Dota 2 event maps."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_intelligence.benchmarks import (  # noqa: E402
    BENCHMARK_VERSION,
    BenchmarkObservation,
    BenchmarkSnapshot,
    build_benchmark_snapshot,
)
from event_intelligence.player_scoring import (  # noqa: E402
    SCORE_VERSION,
    PlayerMapScore,
    PlayerScoreInput,
    ResidualAdjustments,
    score_player_map,
    score_version_for_role,
    transform_player_metrics,
)
from event_intelligence.raw_archive import canonical_json_bytes  # noqa: E402


UTC = timezone.utc
FORMAL_EVENT_STRENGTH = 1.0
LATE_FIGHT_START_SECONDS = 30 * 60


@dataclass(frozen=True)
class StrictPlayerFact:
    match_id: int
    player_slot: int
    account_id: int | None
    team_id: int | None
    is_radiant: bool
    position: int | None
    role_confidence: float
    role_assignment_source: str | None
    role_assignment_cutoff: datetime | None
    role_assignment_input_hash: str | None
    role_assignment_version: str | None
    facts: Mapping[str, Any]
    first_usable_at: datetime
    artifact_path: Path
    content_hash: str


@dataclass(frozen=True)
class StrictMap:
    match_id: int
    started_at: datetime
    completed_at: datetime
    duration_seconds: int
    patch: int | None
    radiant_win: bool
    players: tuple[StrictPlayerFact, ...]
    artifact: Mapping[str, Any]


@dataclass(frozen=True)
class ScoredRow:
    score: PlayerMapScore
    account_id: int | None


@dataclass(frozen=True)
class ScoreReport:
    score_version: str
    benchmark_version: str
    dry_run: bool
    eligible_maps: int
    scored_players: int
    inserted: int
    updated: int
    unchanged: int


def _utc(value: str, field: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _number(value: object) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        return float(value)
    return None


def _exact_sum(values: Iterable[object]) -> float | None:
    normalized = tuple(_number(value) for value in values)
    if not normalized or any(value is None for value in normalized):
        return None
    return math.fsum(value for value in normalized if value is not None)


def _player_id(row: StrictPlayerFact) -> int:
    return row.account_id if row.account_id is not None else -(row.player_slot + 1)


def _artifact_payload(path: Path, expected_hash: str) -> dict[str, Any]:
    try:
        content = gzip.decompress(path.read_bytes())
    except (OSError, EOFError) as error:
        raise RuntimeError(f"cannot read raw artifact {path}") from error
    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != expected_hash:
        raise RuntimeError(f"raw artifact hash mismatch: {path}")
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError(f"raw artifact must contain an object: {path}")
    return payload


def _resolve_assignment_version(
    connection: sqlite3.Connection,
    requested: str | None,
    *,
    match_id: int | None,
) -> str:
    match_filter = "AND roles.match_id=?" if match_id is not None else ""
    parameters: tuple[object, ...] = (match_id,) if match_id is not None else ()
    versions = tuple(
        str(row[0])
        for row in connection.execute(
            f"""SELECT DISTINCT roles.assignment_version
                FROM player_role_assignments AS roles
                JOIN formal_map_eligibility AS eligible
                  ON eligible.match_id=roles.match_id
                WHERE roles.purpose='observed_position' {match_filter}
                ORDER BY roles.assignment_version""",
            parameters,
        ).fetchall()
    )
    if requested is not None:
        if requested not in versions:
            raise ValueError(
                f"observed-position assignment version {requested!r} is unavailable"
            )
        return requested
    if len(versions) != 1:
        rendered = ", ".join(versions) if versions else "none"
        raise ValueError(
            "--assignment-version is required unless exactly one observed-position "
            f"version is available; found: {rendered}"
        )
    return versions[0]


def load_strict_maps(
    connection: sqlite3.Connection,
    *,
    database_path: Path,
    match_id: int | None = None,
    assignment_version: str | None = None,
) -> tuple[StrictMap, ...]:
    """Load only formal-map exact facts and their observed position labels."""
    assignment_version = _resolve_assignment_version(
        connection, assignment_version, match_id=match_id
    )
    parameters: list[object] = [assignment_version]
    match_filter = "AND f.match_id=?" if match_id is not None else ""
    if match_id is not None:
        parameters.append(match_id)
    rows = connection.execute(
        f"""WITH ranked_roles AS (
                SELECT assignment_id, match_id, player_slot, position, confidence,
                       assignment_source, input_cutoff, input_hash,
                       assignment_version,
                       ROW_NUMBER() OVER (
                           PARTITION BY match_id, player_slot
                           ORDER BY created_at DESC, assignment_id DESC
                       ) AS rank
                FROM player_role_assignments
                WHERE purpose='observed_position' AND assignment_version=?
            )
            SELECT f.match_id, f.player_slot, f.account_id, f.team_id, f.is_radiant,
                   f.facts_json, f.first_usable_at, f.source_content_hash,
                   a.storage_path, r.position, COALESCE(r.confidence, 0.0) AS confidence,
                   r.assignment_source, r.input_cutoff, r.input_hash,
                   r.assignment_version
            FROM formal_map_eligibility AS eligible
            JOIN match_ingest_status AS status ON status.match_id=eligible.match_id
            JOIN player_map_facts AS f
              ON f.match_id=eligible.match_id
             AND f.source_content_hash=status.latest_raw_content_hash
            JOIN raw_source_artifacts AS a ON a.artifact_id=f.source_artifact_id
            LEFT JOIN ranked_roles AS r
              ON r.match_id=f.match_id AND r.player_slot=f.player_slot AND r.rank=1
            WHERE eligible.player_readiness='ready'
              AND f.first_usable_at IS NOT NULL
              AND f.is_radiant IS NOT NULL
              AND f.fact_version='opendota-exact-v1:' || status.latest_raw_content_hash
              AND a.source='opendota'
              {match_filter}
            ORDER BY f.match_id, f.player_slot""",
        tuple(parameters),
    ).fetchall()

    grouped: dict[int, list[StrictPlayerFact]] = {}
    payloads: dict[tuple[Path, str], dict[str, Any]] = {}
    for row in rows:
        facts = json.loads(row["facts_json"])
        if not isinstance(facts, dict):
            raise ValueError("player_map_facts.facts_json must contain an object")
        artifact_path = Path(row["storage_path"])
        if not artifact_path.is_absolute():
            artifact_path = database_path.resolve().parent / artifact_path
        value = StrictPlayerFact(
            match_id=int(row["match_id"]),
            player_slot=int(row["player_slot"]),
            account_id=row["account_id"],
            team_id=row["team_id"],
            is_radiant=bool(row["is_radiant"]),
            position=row["position"],
            role_confidence=float(row["confidence"]),
            role_assignment_source=row["assignment_source"],
            role_assignment_cutoff=(
                _utc(row["input_cutoff"], "role input_cutoff")
                if row["input_cutoff"] is not None
                else None
            ),
            role_assignment_input_hash=row["input_hash"],
            role_assignment_version=row["assignment_version"],
            facts=facts,
            first_usable_at=_utc(row["first_usable_at"], "first_usable_at"),
            artifact_path=artifact_path,
            content_hash=str(row["source_content_hash"]),
        )
        grouped.setdefault(value.match_id, []).append(value)
        artifact_key = (artifact_path, value.content_hash)
        if artifact_key not in payloads:
            payloads[artifact_key] = _artifact_payload(artifact_path, value.content_hash)

    maps = []
    for current_match_id, players in sorted(grouped.items()):
        if len(players) != 10:
            continue
        identities = {(row.artifact_path, row.content_hash) for row in players}
        if len(identities) != 1:
            raise ValueError(f"match {current_match_id} has inconsistent fact artifacts")
        payload = payloads[next(iter(identities))]
        if payload.get("match_id") != current_match_id:
            raise ValueError(f"artifact identity mismatch for match {current_match_id}")
        start_time = payload.get("start_time")
        duration = payload.get("duration")
        radiant_win = payload.get("radiant_win")
        if (
            not isinstance(start_time, int)
            or isinstance(start_time, bool)
            or start_time <= 0
            or not isinstance(duration, int)
            or isinstance(duration, bool)
            or duration <= 0
            or not isinstance(radiant_win, bool)
        ):
            continue
        patch = payload.get("patch")
        if isinstance(patch, bool) or not isinstance(patch, int) or patch <= 0:
            patch = None
        started_at = datetime.fromtimestamp(start_time, UTC)
        maps.append(
            StrictMap(
                current_match_id,
                started_at,
                started_at + timedelta(seconds=duration),
                duration,
                patch,
                radiant_win,
                tuple(sorted(players, key=lambda row: row.player_slot)),
                payload,
            )
        )
    return tuple(maps)


def _team_key(row: StrictPlayerFact) -> tuple[str, object]:
    if row.team_id is not None:
        return ("team", row.team_id)
    return ("side", row.is_radiant)


def _effective_usable_at(row: StrictPlayerFact) -> datetime:
    if row.role_assignment_cutoff is None:
        return row.first_usable_at
    return max(row.first_usable_at, row.role_assignment_cutoff)


def _player_index(game: StrictMap, player: StrictPlayerFact) -> int | None:
    raw_players = game.artifact.get("players")
    if not isinstance(raw_players, list) or len(raw_players) != 10:
        return None
    matches = [
        index
        for index, row in enumerate(raw_players)
        if isinstance(row, dict) and row.get("player_slot") == player.player_slot
    ]
    return matches[0] if len(matches) == 1 else None


def _fight_record(record: object) -> tuple[bool, float] | None:
    if not isinstance(record, dict):
        return None
    values = tuple(_number(record.get(name)) for name in ("damage", "healing", "deaths", "buybacks"))
    killed = record.get("killed")
    if any(value is None or value < 0 for value in values) or not isinstance(killed, dict):
        return None
    kill_values = tuple(_number(value) for value in killed.values())
    if any(value is None or value < 0 for value in kill_values):
        return None
    exact = tuple(value for value in (*values, *kill_values) if value is not None)
    return any(value > 0 for value in exact), math.fsum(exact)


def _teamfight_metrics(
    game: StrictMap, player: StrictPlayerFact
) -> dict[str, float | None]:
    raw_fights = game.artifact.get("teamfights")
    player_index = _player_index(game, player)
    own_team = tuple(row for row in game.players if row.is_radiant == player.is_radiant)
    own_indexes = tuple(_player_index(game, row) for row in own_team)
    missing = {
        "teamfight_participations": None,
        "teamfight_opportunities": None,
        "teamfight_impact": None,
        "team_teamfight_impact": None,
        "late_fight_participations": None,
        "late_fight_opportunities": None,
        "late_fight_output": None,
        "team_late_fight_output": None,
    }
    if (
        not isinstance(raw_fights, list)
        or player_index is None
        or len(own_indexes) != 5
        or any(index is None for index in own_indexes)
    ):
        return missing

    parsed: list[tuple[float, tuple[tuple[bool, float], ...]]] = []
    for fight in raw_fights:
        if not isinstance(fight, dict):
            return missing
        start = _number(fight.get("start"))
        records = fight.get("players")
        if start is None or not isinstance(records, list) or len(records) != 10:
            return missing
        facts = tuple(_fight_record(record) for record in records)
        if any(row is None for row in facts):
            return missing
        parsed.append((start, tuple(row for row in facts if row is not None)))

    own_exact_indexes = tuple(int(index) for index in own_indexes if index is not None)

    def aggregate(
        rows: Sequence[tuple[float, tuple[tuple[bool, float], ...]]]
    ) -> tuple[float, float, float, float]:
        return (
            float(sum(row[1][player_index][0] for row in rows)),
            float(len(rows)),
            math.fsum(row[1][player_index][1] for row in rows),
            math.fsum(
                row[1][index][1] for row in rows for index in own_exact_indexes
            ),
        )

    all_values = aggregate(parsed)
    late_values = aggregate(
        tuple(row for row in parsed if row[0] >= LATE_FIGHT_START_SECONDS)
    )
    return {
        "teamfight_participations": all_values[0],
        "teamfight_opportunities": all_values[1],
        "teamfight_impact": all_values[2],
        "team_teamfight_impact": all_values[3],
        "late_fight_participations": late_values[0],
        "late_fight_opportunities": late_values[1],
        "late_fight_output": late_values[2],
        "team_late_fight_output": late_values[3],
    }


def _objective_side(row: Mapping[str, Any]) -> bool | None:
    if row.get("type") == "CHAT_MESSAGE_ROSHAN_KILL":
        return {2: True, 3: False}.get(row.get("team"))
    if row.get("type") != "building_kill":
        return None
    key = row.get("key")
    if not isinstance(key, str):
        return None
    if "badguys" in key:
        return True
    if "goodguys" in key:
        return False
    return None


def _attributed_objectives(
    game: StrictMap,
    player: StrictPlayerFact,
    *,
    high_ground_only: bool,
) -> tuple[float | None, float | None]:
    raw = game.artifact.get("objectives")
    if not isinstance(raw, list):
        return None, None
    candidates = []
    for row in raw:
        if not isinstance(row, dict) or _objective_side(row) != player.is_radiant:
            continue
        key = row.get("key")
        if high_ground_only and not (
            row.get("type") == "building_kill"
            and isinstance(key, str)
            and ("tower3" in key or "_rax_" in key)
        ):
            continue
        candidates.append(row)
    slots = []
    for row in candidates:
        slot = row.get("player_slot", row.get("slot"))
        if not isinstance(slot, int) or isinstance(slot, bool):
            return None, float(len(candidates))
        if (slot < 128) != player.is_radiant:
            return None, float(len(candidates))
        slots.append(slot)
    return float(slots.count(player.player_slot)), float(len(candidates))


def _roshan_opportunities(
    game: StrictMap, player: StrictPlayerFact
) -> float | None:
    raw = game.artifact.get("objectives")
    if not isinstance(raw, list):
        return None
    roshans = [
        row
        for row in raw
        if isinstance(row, dict) and row.get("type") == "CHAT_MESSAGE_ROSHAN_KILL"
    ]
    sides = tuple(_objective_side(row) for row in roshans)
    if any(side is None for side in sides):
        return None
    return float(sum(side == player.is_radiant for side in sides))


def _raw_metrics(game: StrictMap, player: StrictPlayerFact) -> dict[str, float | None]:
    facts = player.facts
    own_team = tuple(row for row in game.players if _team_key(row) == _team_key(player))
    opponents = tuple(row for row in game.players if row.is_radiant != player.is_radiant)

    def fact(name: str) -> float | None:
        return _number(facts.get(name))

    def team_sum(name: str) -> float | None:
        if len(own_team) != 5:
            return None
        return _exact_sum(row.facts.get(name) for row in own_team)

    def pair_sum(first: str, second: str) -> float | None:
        return _exact_sum((facts.get(first), facts.get(second)))

    opposing_role = tuple(row for row in opponents if row.position == player.position)
    lane_opponent = opposing_role[0] if len(opposing_role) == 1 else None
    opposing_carries = tuple(row for row in opponents if row.position == 1)
    opposing_carry = opposing_carries[0] if len(opposing_carries) == 1 else None

    def lane_diff(name: str) -> float | None:
        own = fact(name)
        other = _number(lane_opponent.facts.get(name)) if lane_opponent else None
        return own - other if own is not None and other is not None else None

    def opposing_carry_suppression(name: str) -> float | None:
        own = fact(name)
        other = _number(opposing_carry.facts.get(name)) if opposing_carry else None
        return own - other if own is not None and other is not None else None

    damage_taken = facts.get("damage_taken")
    if isinstance(damage_taken, dict):
        damage_taken_total = _exact_sum(damage_taken.values())
    else:
        damage_taken_total = _number(damage_taken)

    objective_participations, objective_opportunities = _attributed_objectives(
        game, player, high_ground_only=False
    )
    high_ground_participations, high_ground_opportunities = _attributed_objectives(
        game, player, high_ground_only=True
    )
    values: dict[str, float | None] = {
        "last_hits": fact("last_hits"),
        "net_worth": fact("net_worth"),
        "hero_damage": fact("hero_damage"),
        "tower_damage": fact("tower_damage"),
        "team_hero_damage": team_sum("hero_damage"),
        "team_tower_damage": team_sum("tower_damage"),
        "kills": fact("kills"),
        "deaths": fact("deaths"),
        "assists": fact("assists"),
        "kills_assists": pair_sum("kills", "assists"),
        "team_kills": team_sum("kills"),
        "gold_10_diff": lane_diff("gold_at_10"),
        "last_hits_10_diff": lane_diff("last_hits_at_10"),
        "opposing_carry_gold_suppression_at_10": opposing_carry_suppression(
            "gold_at_10"
        ),
        "opposing_carry_lh_suppression_at_10": opposing_carry_suppression(
            "last_hits_at_10"
        ),
        "early_kill_participations": pair_sum("kills_at_10", "assists_at_10"),
        "early_team_kills": team_sum("kills_at_10"),
        "rune_pickups": fact("rune_pickups"),
        "damage_taken": damage_taken_total,
        "control_seconds": fact("stuns"),
        "observer_wards": fact("observer_wards_placed"),
        "sentry_wards": fact("sentry_wards_placed"),
        "dewards": pair_sum("observer_kills", "sentry_kills"),
        "hero_healing": fact("hero_healing"),
        "stacks": fact("camps_stacked"),
        "roshan_participations": None,
        "roshan_opportunities": _roshan_opportunities(game, player),
        "objective_participations": objective_participations,
        "objective_opportunities": objective_opportunities,
        "high_ground_participations": high_ground_participations,
        "high_ground_opportunities": high_ground_opportunities,
    }
    values.update(_teamfight_metrics(game, player))
    return values


def build_scores(
    games: Sequence[StrictMap],
    *,
    min_samples: int = 5,
    target_match_id: int | None = None,
) -> tuple[ScoredRow, ...]:
    prepared: list[tuple[StrictMap, StrictPlayerFact, dict[str, float | None]]] = []
    for game in games:
        for player in game.players:
            if player.position in (1, 2, 3, 4, 5):
                prepared.append((game, player, _raw_metrics(game, player)))

    observations = tuple(
        BenchmarkObservation(
            match_id=game.match_id,
            player_id=_player_id(player),
            position=int(player.position),
            patch=game.patch,
            duration_seconds=game.duration_seconds,
            event_strength=FORMAL_EVENT_STRENGTH,
            completed_at=game.completed_at,
            first_usable_at=_effective_usable_at(player),
            role_assignment_source=str(player.role_assignment_source),
            role_assignment_cutoff=player.role_assignment_cutoff,
            role_assignment_input_hash=str(player.role_assignment_input_hash),
            role_assignment_version=str(player.role_assignment_version),
            metrics=tuple(
                sorted(
                    (name, value)
                    for name, value in transform_player_metrics(
                        int(player.position), raw, game.duration_seconds
                    ).items()
                    if value is not None
                )
            ),
        )
        for game, player, raw in prepared
    )
    results = []
    benchmark_cache: dict[
        tuple[int, datetime, datetime, int | None, int, int, float, int],
        BenchmarkSnapshot,
    ] = {}
    for game, player, raw in prepared:
        if target_match_id is not None and game.match_id != target_match_id:
            continue
        position = int(player.position)
        role_cutoff = player.role_assignment_cutoff
        role_source = player.role_assignment_source
        role_hash = player.role_assignment_input_hash
        role_version = player.role_assignment_version
        if (
            role_cutoff is None
            or role_source is None
            or role_hash is None
            or role_version is None
        ):
            continue
        scoring_cutoff = _effective_usable_at(player)
        benchmark_key = (
            game.match_id,
            game.started_at,
            scoring_cutoff,
            game.patch,
            position,
            game.duration_seconds,
            FORMAL_EVENT_STRENGTH,
            min_samples,
        )
        benchmark = benchmark_cache.get(benchmark_key)
        if benchmark is None:
            benchmark = build_benchmark_snapshot(
                observations,
                target_match_id=game.match_id,
                target_started_at=game.started_at,
                cutoff=scoring_cutoff,
                patch=game.patch,
                position=position,
                duration_seconds=game.duration_seconds,
                event_strength=FORMAL_EVENT_STRENGTH,
                min_samples=min_samples,
            )
            benchmark_cache[benchmark_key] = benchmark
        score = score_player_map(
            PlayerScoreInput(
                match_id=game.match_id,
                player_id=_player_id(player),
                player_slot=player.player_slot,
                position=position,
                role_confidence=player.role_confidence,
                patch=game.patch,
                duration_seconds=game.duration_seconds,
                event_strength=FORMAL_EVENT_STRENGTH,
                target_started_at=game.started_at,
                first_usable_at=player.first_usable_at,
                role_assignment_source=role_source,
                role_assignment_cutoff=role_cutoff,
                role_assignment_input_hash=role_hash,
                role_assignment_version=role_version,
                raw_metrics=tuple(sorted(raw.items())),
                residuals=ResidualAdjustments(),
                result_adjustment=5.0 if game.radiant_win == player.is_radiant else -5.0,
            ),
            benchmark,
        )
        results.append(ScoredRow(score, player.account_id))
    return tuple(results)


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def persist_scores(
    connection: sqlite3.Connection,
    rows: Sequence[ScoredRow],
    *,
    dry_run: bool,
) -> tuple[int, int, int]:
    inserted = updated = unchanged = 0
    now = datetime.now(UTC).isoformat()
    if not dry_run:
        connection.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            score = row.score
            existing = connection.execute(
                """SELECT input_hash FROM player_map_scores
                   WHERE match_id=? AND player_slot=? AND score_version=?""",
                (score.match_id, score.player_slot, score.version),
            ).fetchone()
            if existing is not None and existing["input_hash"] == score.input_hash:
                unchanged += 1
                continue
            inserted += existing is None
            updated += existing is not None
            if dry_run:
                continue
            component_facts = [
                {
                    "name": component.name,
                    "metrics": [
                        {
                            "metric_id": metric.metric_id,
                            "raw_metric": metric.raw_metric,
                            "transform": metric.transform.value,
                            "numerator": metric.numerator,
                            "denominator": metric.denominator,
                            "transformed_value": metric.transformed_value,
                            "benchmark_median": metric.benchmark_median,
                            "benchmark_mad": metric.benchmark_mad,
                            "robust_z": metric.robust_z,
                            "direction": metric.direction,
                            "missing_reason": metric.missing_reason,
                        }
                        for metric in component.metrics
                    ],
                }
                for component in score.components
            ]
            component_scores = [
                {
                    "name": component.name,
                    "coverage": component.coverage,
                    "score": component.score,
                }
                for component in score.components
            ]
            explanation = {
                "ranking_eligible": score.ranking_eligible,
                "residual_points": dict(score.residual_points),
                "residual_adjustment_applied": score.residual_adjustment_applied,
                "result_adjustment_applied": score.result_adjustment_applied,
                "explanation": score.explanation,
                "benchmark_version": BENCHMARK_VERSION,
            }
            connection.execute(
                """INSERT INTO player_map_scores
                   (match_id, player_slot, account_id, position, execution_score,
                    result_adjusted_score, component_facts_json, component_scores_json,
                    weights_json, coverage, role_confidence, benchmark_cutoff,
                    benchmark_hash, input_hash, score_version, explanation_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(match_id, player_slot, score_version) DO UPDATE SET
                     account_id=excluded.account_id, position=excluded.position,
                     execution_score=excluded.execution_score,
                     result_adjusted_score=excluded.result_adjusted_score,
                     component_facts_json=excluded.component_facts_json,
                     component_scores_json=excluded.component_scores_json,
                     weights_json=excluded.weights_json, coverage=excluded.coverage,
                     role_confidence=excluded.role_confidence,
                     benchmark_cutoff=excluded.benchmark_cutoff,
                     benchmark_hash=excluded.benchmark_hash,
                     input_hash=excluded.input_hash,
                     explanation_json=excluded.explanation_json,
                     created_at=excluded.created_at""",
                (
                    score.match_id,
                    score.player_slot,
                    row.account_id,
                    score.position,
                    score.execution_score,
                    score.result_adjusted_score,
                    _json(component_facts),
                    _json(component_scores),
                    _json(score.weights),
                    score.coverage,
                    score.role_confidence,
                    score.benchmark_cutoff.isoformat(),
                    score.benchmark_hash,
                    score.input_hash,
                    score.version,
                    _json(explanation),
                    now,
                ),
            )
        if not dry_run:
            connection.commit()
    except BaseException:
        if not dry_run:
            connection.rollback()
        raise
    return inserted, updated, unchanged


def run_scoring(
    database: Path,
    *,
    dry_run: bool = False,
    match_id: int | None = None,
    assignment_version: str | None = None,
    min_samples: int = 5,
) -> ScoreReport:
    database = database.resolve()
    if dry_run:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        resolved_assignment_version = _resolve_assignment_version(
            connection, assignment_version, match_id=match_id
        )
        games = load_strict_maps(
            connection,
            database_path=database,
            assignment_version=resolved_assignment_version,
        )
        if match_id is not None and all(game.match_id != match_id for game in games):
            raise ValueError(f"formal ready match not found: {match_id}")
        scores = build_scores(
            games,
            min_samples=min_samples,
            target_match_id=match_id,
        )
        inserted, updated, unchanged = persist_scores(
            connection, scores, dry_run=dry_run
        )
        return ScoreReport(
            score_version_for_role(resolved_assignment_version),
            BENCHMARK_VERSION,
            dry_run,
            len(games) if match_id is None else 1,
            len(scores),
            inserted,
            updated,
            unchanged,
        )
    finally:
        connection.close()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--match", type=_positive_int, help="one formal match ID")
    parser.add_argument("--assignment-version", help="pin observed-position version")
    parser.add_argument("--min-samples", type=_positive_int, default=5)
    parser.add_argument("--dry-run", action="store_true", help="compute without writes")
    parser.add_argument(
        "--score-version",
        choices=(SCORE_VERSION,),
        default=SCORE_VERSION,
        help="immutable scoring algorithm version",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_scoring(
        args.database,
        dry_run=args.dry_run,
        match_id=args.match,
        assignment_version=args.assignment_version,
        min_samples=args.min_samples,
    )
    print(_json(report.__dict__))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
