"""Versioned, role-aware player-map scoring from source-exact metrics."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

from .benchmarks import BenchmarkSnapshot, robust_z
from .raw_archive import canonical_json_bytes


SCORE_VERSION = "player-score-v2"
ROLE_DEPENDENT_CONFIDENCE = 0.7


class MetricTransform(str, Enum):
    RAW = "raw"
    PER_10 = "per_10_minutes"
    TEAM_SHARE = "team_share"
    OPPORTUNITY = "opportunity_rate"
    PER_ECONOMY = "per_1000_economy"


@dataclass(frozen=True)
class MetricSpec:
    raw_metric: str
    transform: MetricTransform
    denominator_metric: str | None = None
    direction: int = 1


@dataclass(frozen=True)
class ComponentConfig:
    name: str
    weight: float
    metrics: tuple[MetricSpec, ...]


@dataclass(frozen=True)
class RoleScoringConfig:
    position: int
    components: tuple[ComponentConfig, ...]


def _metric(
    name: str,
    transform: MetricTransform,
    denominator: str | None = None,
    direction: int = 1,
) -> MetricSpec:
    return MetricSpec(name, transform, denominator, direction)


def _component(
    name: str, weight: float, *metrics: MetricSpec
) -> ComponentConfig:
    return ComponentConfig(name, weight, metrics)


RAW = MetricTransform.RAW
PER_10 = MetricTransform.PER_10
TEAM_SHARE = MetricTransform.TEAM_SHARE
OPPORTUNITY = MetricTransform.OPPORTUNITY
PER_ECONOMY = MetricTransform.PER_ECONOMY


ROLE_SCORING_CONFIGS = (
    RoleScoringConfig(
        1,
        (
            _component(
                "farm_efficiency",
                0.25,
                _metric("last_hits", PER_10),
                _metric("net_worth", PER_10),
            ),
            _component(
                "output_per_economy",
                0.20,
                _metric("hero_damage", PER_ECONOMY, "net_worth"),
                _metric("tower_damage", PER_ECONOMY, "net_worth"),
            ),
            _component(
                "participation",
                0.15,
                _metric("kills_assists", TEAM_SHARE, "team_kills"),
            ),
            _component(
                "tower_roshan_conversion",
                0.20,
                _metric("tower_damage", TEAM_SHARE, "team_tower_damage"),
                _metric(
                    "roshan_participations", OPPORTUNITY, "roshan_opportunities"
                ),
            ),
            _component("survival", 0.10, _metric("deaths", PER_10, direction=-1)),
            _component(
                "late_fights",
                0.10,
                _metric(
                    "late_fight_participations",
                    OPPORTUNITY,
                    "late_fight_opportunities",
                ),
                _metric(
                    "late_fight_output", TEAM_SHARE, "team_late_fight_output"
                ),
            ),
        ),
    ),
    RoleScoringConfig(
        2,
        (
            _component(
                "lane_differential",
                0.20,
                _metric("gold_10_diff", RAW),
                _metric("last_hits_10_diff", RAW),
            ),
            _component(
                "rotation_participation",
                0.25,
                _metric(
                    "early_kill_participations", OPPORTUNITY, "early_team_kills"
                ),
                _metric("rune_pickups", PER_10),
            ),
            _component(
                "kill_damage_output",
                0.20,
                _metric("kills", PER_10),
                _metric("hero_damage", TEAM_SHARE, "team_hero_damage"),
            ),
            _component(
                "tempo_objectives",
                0.15,
                _metric(
                    "early_objective_participations",
                    OPPORTUNITY,
                    "early_objective_opportunities",
                ),
                _metric("tower_damage", TEAM_SHARE, "team_tower_damage"),
            ),
            _component(
                "resource_efficiency",
                0.10,
                _metric("hero_damage", PER_ECONOMY, "net_worth"),
                _metric("kills_assists", PER_ECONOMY, "net_worth"),
            ),
            _component("survival", 0.10, _metric("deaths", PER_10, direction=-1)),
        ),
    ),
    RoleScoringConfig(
        3,
        (
            _component(
                "suppress_opposing_carry",
                0.20,
                _metric("opposing_carry_gold_suppression_at_10", RAW),
                _metric("opposing_carry_lh_suppression_at_10", RAW),
            ),
            _component(
                "damage_taken_initiation_control",
                0.25,
                _metric("damage_taken", PER_10),
                _metric("control_seconds", PER_10),
                _metric("initiations", OPPORTUNITY, "initiation_opportunities"),
            ),
            _component(
                "teamfights",
                0.20,
                _metric(
                    "teamfight_participations",
                    OPPORTUNITY,
                    "teamfight_opportunities",
                ),
                _metric("teamfight_impact", TEAM_SHARE, "team_teamfight_impact"),
            ),
            _component(
                "tower_high_ground_conversion",
                0.15,
                _metric("tower_damage", TEAM_SHARE, "team_tower_damage"),
                _metric(
                    "high_ground_participations",
                    OPPORTUNITY,
                    "high_ground_opportunities",
                ),
            ),
            _component(
                "low_resource_efficiency",
                0.10,
                _metric("hero_damage", PER_ECONOMY, "net_worth"),
                _metric("control_seconds", PER_ECONOMY, "net_worth"),
            ),
            _component("survival", 0.10, _metric("deaths", PER_10, direction=-1)),
        ),
    ),
    RoleScoringConfig(
        4,
        (
            _component(
                "early_rotation_participation",
                0.25,
                _metric(
                    "early_kill_participations", OPPORTUNITY, "early_team_kills"
                ),
                _metric("rotations_at_10", RAW),
            ),
            _component(
                "vision_dewarding",
                0.20,
                _metric("observer_wards", PER_10),
                _metric("sentry_wards", PER_10),
                _metric("dewards", PER_10),
            ),
            _component(
                "control_initiation",
                0.25,
                _metric("control_seconds", PER_10),
                _metric("initiations", OPPORTUNITY, "initiation_opportunities"),
            ),
            _component(
                "teamfights",
                0.15,
                _metric(
                    "teamfight_participations",
                    OPPORTUNITY,
                    "teamfight_opportunities",
                ),
                _metric("assists", PER_10),
            ),
            _component(
                "low_resource_efficiency",
                0.10,
                _metric("hero_damage", PER_ECONOMY, "net_worth"),
                _metric("control_seconds", PER_ECONOMY, "net_worth"),
            ),
            _component(
                "objective_conversion",
                0.05,
                _metric(
                    "objective_participations", OPPORTUNITY, "objective_opportunities"
                ),
            ),
        ),
    ),
    RoleScoringConfig(
        5,
        (
            _component(
                "vision_dewarding",
                0.30,
                _metric("observer_wards", PER_10),
                _metric("sentry_wards", PER_10),
                _metric("dewards", PER_10),
            ),
            _component(
                "control_save_healing",
                0.25,
                _metric("control_seconds", PER_10),
                _metric("saves", OPPORTUNITY, "save_opportunities"),
                _metric("hero_healing", PER_10),
            ),
            _component(
                "teamfight_participation",
                0.20,
                _metric(
                    "teamfight_participations",
                    OPPORTUNITY,
                    "teamfight_opportunities",
                ),
                _metric("assists", TEAM_SHARE, "team_kills"),
            ),
            _component(
                "low_resource_efficiency",
                0.10,
                _metric("control_seconds", PER_ECONOMY, "net_worth"),
                _metric("assists", PER_ECONOMY, "net_worth"),
            ),
            _component(
                "lane_support_pulls_stacks",
                0.10,
                _metric("pulls", OPPORTUNITY, "pull_opportunities"),
                _metric("stacks", PER_10),
                _metric("lane_support_events", PER_10),
            ),
            _component(
                "objectives",
                0.05,
                _metric(
                    "objective_participations", OPPORTUNITY, "objective_opportunities"
                ),
            ),
        ),
    ),
)


@dataclass(frozen=True)
class ResidualAdjustments:
    opponent_strength: float | None = None
    hero_matchup: float | None = None
    draft_expectation: float | None = None


@dataclass(frozen=True)
class PlayerScoreInput:
    match_id: int
    player_id: int
    player_slot: int
    position: int
    role_confidence: float
    patch: int | None
    duration_seconds: int
    event_strength: float
    target_started_at: datetime
    first_usable_at: datetime | None
    role_assignment_source: str
    role_assignment_cutoff: datetime
    role_assignment_input_hash: str
    role_assignment_version: str
    raw_metrics: tuple[tuple[str, float | None], ...]
    residuals: ResidualAdjustments = ResidualAdjustments()
    result_adjustment: float = 0.0


@dataclass(frozen=True)
class MetricResult:
    metric_id: str
    raw_metric: str
    transform: MetricTransform
    numerator: float | None
    denominator: float | None
    transformed_value: float | None
    benchmark_median: float | None
    benchmark_mad: float | None
    robust_z: float | None
    direction: int
    missing_reason: str | None


@dataclass(frozen=True)
class ComponentResult:
    name: str
    weight: float
    coverage: float
    score: float | None
    metrics: tuple[MetricResult, ...]


@dataclass(frozen=True)
class PlayerMapScore:
    match_id: int
    player_id: int
    player_slot: int
    position: int
    execution_score: float
    result_adjusted_score: float
    components: tuple[ComponentResult, ...]
    weights: tuple[tuple[str, float], ...]
    coverage: float
    role_confidence: float
    ranking_eligible: bool
    benchmark_cutoff: datetime
    benchmark_hash: str
    input_hash: str
    version: str
    residual_points: tuple[tuple[str, float | None], ...]
    residual_adjustment_applied: float
    result_adjustment_applied: float
    explanation: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return _jsonable(self)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    return value


def config_for_position(position: int) -> RoleScoringConfig:
    config = next((row for row in ROLE_SCORING_CONFIGS if row.position == position), None)
    if config is None:
        raise ValueError("position must be between 1 and 5")
    return config


def _raw_mapping(
    raw_metrics: Mapping[str, float | None] | Iterable[tuple[str, float | None]],
) -> dict[str, float | None]:
    if isinstance(raw_metrics, Mapping):
        return dict(raw_metrics)
    rows = tuple(raw_metrics)
    result = dict(rows)
    if len(result) != len(rows):
        raise ValueError("raw_metrics contains duplicate names")
    return result


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _transform(
    spec: MetricSpec,
    raw: Mapping[str, float | None],
    duration_seconds: int,
) -> tuple[float | None, float | None, str | None]:
    numerator = raw.get(spec.raw_metric)
    if numerator is None:
        return None, None, "source_missing"
    if not _finite_number(numerator):
        return None, None, "source_invalid"
    if spec.transform is RAW:
        return float(numerator), None, None
    if spec.transform is PER_10:
        if duration_seconds <= 0:
            return None, None, "duration_missing"
        return float(numerator) / duration_seconds * 600.0, float(duration_seconds), None
    denominator = raw.get(spec.denominator_metric or "")
    if not _finite_number(denominator) or denominator <= 0:
        return (
            None,
            float(denominator) if _finite_number(denominator) else None,
            "denominator_missing",
        )
    value = float(numerator) / float(denominator)
    if spec.transform is PER_ECONOMY:
        value *= 1_000.0
    return value, float(denominator), None


def transform_player_metrics(
    position: int,
    raw_metrics: Mapping[str, float | None] | Iterable[tuple[str, float | None]],
    duration_seconds: int,
) -> dict[str, float | None]:
    raw = _raw_mapping(raw_metrics)
    return {
        f"{component.name}.{spec.raw_metric}": _transform(
            spec, raw, duration_seconds
        )[0]
        for component in config_for_position(position).components
        for spec in component.metrics
    }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _input_hash(value: PlayerScoreInput, benchmark: BenchmarkSnapshot) -> str:
    payload = {
        "version": SCORE_VERSION,
        "input": _jsonable(value),
        "benchmark_hash": benchmark.benchmark_hash,
        "weights": [
            (component.name, component.weight)
            for component in config_for_position(value.position).components
        ],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def score_version_for_role(assignment_version: str) -> str:
    if not assignment_version.strip():
        raise ValueError("role assignment version cannot be empty")
    return f"{SCORE_VERSION}+observed-role={assignment_version}"


def score_player_map(
    value: PlayerScoreInput,
    benchmark: BenchmarkSnapshot,
) -> PlayerMapScore:
    """Score one player without substituting any missing source metric."""
    config = config_for_position(value.position)
    if not 0.0 <= value.role_confidence <= 1.0:
        raise ValueError("role_confidence must be between 0 and 1")
    if benchmark.position != value.position or benchmark.target_match_id != value.match_id:
        raise ValueError("benchmark target does not match player score input")
    if value.first_usable_at is not None and value.first_usable_at > benchmark.cutoff:
        raise ValueError("player facts were not usable at the scoring cutoff")
    if value.role_assignment_cutoff > benchmark.cutoff:
        raise ValueError("role assignment was not usable at the scoring cutoff")
    if not value.role_assignment_source.strip():
        raise ValueError("role assignment source cannot be empty")
    if len(value.role_assignment_input_hash) != 64:
        raise ValueError("role assignment input hash must be a SHA-256 hex digest")
    try:
        int(value.role_assignment_input_hash, 16)
    except ValueError as error:
        raise ValueError("role assignment input hash must be a SHA-256 hex digest") from error
    residual_values = tuple(
        point
        for point in (
            value.residuals.opponent_strength,
            value.residuals.hero_matchup,
            value.residuals.draft_expectation,
        )
        if point is not None
    )
    if any(not _finite_number(point) for point in residual_values):
        raise ValueError("residual adjustments must be finite numbers or None")
    if not _finite_number(value.result_adjustment):
        raise ValueError("result_adjustment must be a finite number")
    raw = _raw_mapping(value.raw_metrics)
    if any(
        isinstance(point, (int, float))
        and not isinstance(point, bool)
        and not math.isfinite(point)
        for point in raw.values()
        if point is not None
    ):
        raise ValueError("raw_metrics cannot contain non-finite numbers")
    components: list[ComponentResult] = []
    for component in config.components:
        metric_results = []
        normalized = []
        for spec in component.metrics:
            metric_id = f"{component.name}.{spec.raw_metric}"
            transformed, denominator, missing = _transform(
                spec, raw, value.duration_seconds
            )
            statistic = benchmark.get(metric_id) if transformed is not None else None
            if transformed is not None and statistic is None:
                missing = "benchmark_missing"
            z_score = (
                robust_z(transformed, median=statistic.median, mad=statistic.mad)
                * spec.direction
                if transformed is not None and statistic is not None
                else None
            )
            if z_score is not None:
                normalized.append(z_score)
            numerator = raw.get(spec.raw_metric)
            metric_results.append(
                MetricResult(
                    metric_id=metric_id,
                    raw_metric=spec.raw_metric,
                    transform=spec.transform,
                    numerator=float(numerator) if _finite_number(numerator) else None,
                    denominator=denominator,
                    transformed_value=transformed,
                    benchmark_median=statistic.median if statistic is not None else None,
                    benchmark_mad=statistic.mad if statistic is not None else None,
                    robust_z=z_score,
                    direction=spec.direction,
                    missing_reason=missing,
                )
            )
        coverage = len(normalized) / len(component.metrics)
        component_score = (
            _clamp(50.0 + 10.0 * math.fsum(normalized) / len(normalized), 0.0, 100.0)
            if normalized
            else None
        )
        components.append(
            ComponentResult(
                component.name,
                component.weight,
                round(coverage, 6),
                round(component_score, 6) if component_score is not None else None,
                tuple(metric_results),
            )
        )

    coverage = math.fsum(row.weight * row.coverage for row in components)
    observed_weight = math.fsum(
        row.weight * row.coverage for row in components if row.score is not None
    )
    covered_score = (
        math.fsum(
            row.weight * row.coverage * row.score
            for row in components
            if row.score is not None
        )
        / observed_weight
        if observed_weight > 0
        else 50.0
    )
    reliability = _clamp(coverage * value.role_confidence, 0.0, 1.0)
    residual_points = (
        ("opponent_strength", value.residuals.opponent_strength),
        ("hero_matchup", value.residuals.hero_matchup),
        ("draft_expectation", value.residuals.draft_expectation),
    )
    residual_total = math.fsum(
        point for _, point in residual_points if point is not None
    )
    residual_applied = _clamp(residual_total, -5.0, 5.0) * reliability
    execution = _clamp(
        50.0 + (covered_score - 50.0) * reliability + residual_applied,
        0.0,
        100.0,
    )
    result_adjustment = _clamp(value.result_adjustment, -5.0, 5.0) * reliability
    result_adjusted = _clamp(execution + result_adjustment, 0.0, 100.0)
    missing_ids = tuple(
        metric.metric_id
        for component in components
        for metric in component.metrics
        if metric.missing_reason is not None
    )
    explanation = (
        f"weighted_coverage={coverage:.6f}",
        f"role_confidence={value.role_confidence:.6f}",
        f"reliability={reliability:.6f}",
        f"residual_adjustment={residual_applied:.6f}",
        "missing=" + ",".join(missing_ids),
    )
    return PlayerMapScore(
        match_id=value.match_id,
        player_id=value.player_id,
        player_slot=value.player_slot,
        position=value.position,
        execution_score=round(execution, 6),
        result_adjusted_score=round(result_adjusted, 6),
        components=tuple(components),
        weights=tuple((row.name, row.weight) for row in config.components),
        coverage=round(coverage, 6),
        role_confidence=value.role_confidence,
        ranking_eligible=(
            value.role_confidence >= ROLE_DEPENDENT_CONFIDENCE and coverage > 0
        ),
        benchmark_cutoff=benchmark.cutoff,
        benchmark_hash=benchmark.benchmark_hash,
        input_hash=_input_hash(value, benchmark),
        version=score_version_for_role(value.role_assignment_version),
        residual_points=residual_points,
        residual_adjustment_applied=round(residual_applied, 6),
        result_adjustment_applied=round(result_adjustment, 6),
        explanation=explanation,
    )
