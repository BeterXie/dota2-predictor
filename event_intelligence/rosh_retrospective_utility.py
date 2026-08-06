"""Retrospective-only utility analysis for legacy pure R.O.S.H. scores."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from statistics import mean, median, stdev
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logit
from scipy.stats import pointbiserialr, spearmanr
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold

from database.session import PostgresSession
from event_intelligence.legacy_rosh_reconstruction import (
    LEGACY_ROSH_FORMULA_VERSION,
    LegacyRoshAuditRow,
    audit_legacy_rosh_reconstruction,
)


ROSH_RETROSPECTIVE_UTILITY_VERSION = "rosh-retrospective-utility-v1"
ROSH_SCORE_DIRECTION = "positive_favors_radiant_negative_favors_dire"
ROSH_NEUTRAL_POINT = 0.0
BOOTSTRAP_SEED = "rosh-retrospective-series-bootstrap-v1"
SANITY_SEED = "rosh-retrospective-sanity-v1"
DEFAULT_BOOTSTRAP_SAMPLES = 2_000
DEFAULT_SANITY_PERMUTATIONS = 200
ALLOWED_CONCLUSIONS = frozenset(
    {
        "no retrospective association detected",
        "standalone retrospective association only",
        "incremental retrospective information beyond Team Rating",
        "unstable / inconclusive retrospective evidence",
    }
)
_UTC = timezone.utc
_PROBABILITY_EPSILON = 1e-12


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _seed(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:16], 16)


def _utc(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{field} must be an RFC 3339 timestamp") from error
    else:
        raise ValueError(f"{field} must be an RFC 3339 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(_UTC)


def _outcome(value: object) -> int:
    if value in (True, 1):
        return 1
    if value in (False, 0):
        return 0
    raise ValueError("radiant outcome must be binary")


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


@dataclass(frozen=True)
class LegacyPureScore:
    match_id: int
    score_key: str
    formula_version: str
    source_week: int
    source_as_of: str
    prediction_cutoff: datetime
    pure_lineup_score: float
    event_id: str
    patch: int | None


@dataclass(frozen=True)
class RetrospectiveRow:
    match_id: int
    score_key: str
    formula_version: str
    prediction_cutoff: datetime
    pure_lineup_score: float
    radiant_win: int
    series_id: int | None
    series_key: str
    event_id: str
    patch: int | None
    month: str
    team_probability: float | None


@dataclass(frozen=True)
class CanonicalSelection:
    rows_before: int
    duplicate_groups: int
    duplicate_rows: int
    conflicting_score_groups: int
    rows_after: int
    removed_rows: int
    rule: str


@dataclass(frozen=True)
class CohortLoadResult:
    candidates: tuple[RetrospectiveRow, ...]
    paired: tuple[RetrospectiveRow, ...]
    canonical_selection: CanonicalSelection
    evidence_hash_valid: int
    formal_valid_results: int
    missing_team_rating: int
    formula_versions: tuple[tuple[str, int], ...]
    source_unchanged: bool


def _legacy_score(row: LegacyRoshAuditRow) -> LegacyPureScore:
    return LegacyPureScore(
        match_id=row.match_id,
        score_key=row.score_key,
        formula_version=row.formula_version,
        source_week=row.source_week,
        source_as_of=row.source_as_of,
        prediction_cutoff=_utc(row.prediction_cutoff, "prediction_cutoff"),
        pure_lineup_score=_finite(row.stored_score, "pure_lineup_score"),
        event_id=row.event_id,
        patch=row.patch,
    )


def canonicalize_legacy_scores(
    rows: Sequence[LegacyPureScore],
) -> tuple[tuple[LegacyPureScore, ...], CanonicalSelection]:
    """Choose one result-independent row for each match/formula identity."""

    grouped: dict[tuple[int, str], list[LegacyPureScore]] = {}
    for row in rows:
        grouped.setdefault((row.match_id, row.formula_version), []).append(row)
    selected: list[LegacyPureScore] = []
    duplicate_groups = 0
    duplicate_rows = 0
    conflicting_groups = 0
    for key in sorted(grouped):
        candidates = grouped[key]
        if len(candidates) > 1:
            duplicate_groups += 1
            duplicate_rows += len(candidates)
            if len({row.pure_lineup_score for row in candidates}) > 1:
                conflicting_groups += 1
        selected.append(
            min(
                candidates,
                key=lambda row: (
                    _utc(row.source_as_of, "source_as_of"),
                    row.source_week,
                    row.score_key,
                ),
            )
        )
    ordered = tuple(sorted(selected, key=lambda row: (row.prediction_cutoff, row.match_id)))
    summary = CanonicalSelection(
        rows_before=len(rows),
        duplicate_groups=duplicate_groups,
        duplicate_rows=duplicate_rows,
        conflicting_score_groups=conflicting_groups,
        rows_after=len(ordered),
        removed_rows=len(rows) - len(ordered),
        rule="earliest_source_as_of_then_source_week_then_score_key",
    )
    return ordered, summary


def _metadata_rows(
    connection: PostgresSession,
    match_ids: Sequence[int],
) -> tuple[Mapping[str, Any], ...]:
    if not match_ids:
        return ()
    rows = connection.execute(
        """SELECT status.match_id, status.event_id, status.start_time,
                  status.series_id, status.has_valid_result,
                  status.is_exhibition, status.is_forfeit,
                  status.is_void_remake, game.patch, game.radiant_win,
                  prediction.run_id AS team_run_id,
                  prediction.raw_probability AS team_probability,
                  prediction.eventual_radiant_win AS team_outcome,
                  run.rating_version AS team_rating_version,
                  run.availability_mode AS team_availability_mode
             FROM match_ingest_status AS status
             JOIN matches AS game ON game.match_id=status.match_id
             LEFT JOIN team_rating_predictions AS prediction
               ON prediction.match_id=status.match_id
              AND prediction.status='settled'
              AND prediction.raw_probability IS NOT NULL
             LEFT JOIN team_rating_runs AS run ON run.run_id=prediction.run_id
            WHERE status.match_id=ANY(?)
            ORDER BY status.match_id, prediction.run_id""",
        (list(match_ids),),
    ).fetchall()
    return tuple(dict(row) for row in rows)


def load_retrospective_cohort(connection: PostgresSession) -> CohortLoadResult:
    """Load the legacy pure-score cohort without mutating source state."""

    if not isinstance(connection, PostgresSession):
        raise ValueError("connection must be a PostgresSession")
    audit = audit_legacy_rosh_reconstruction(connection)
    valid = tuple(
        _legacy_score(row)
        for row in audit.records
        if row.evidence_hash_valid and row.formula_available
    )
    canonical, selection = canonicalize_legacy_scores(valid)
    by_match = {row.match_id: row for row in canonical}
    if len(by_match) != len(canonical):
        raise ValueError("multiple canonical legacy formulas remain for one match")
    metadata = _metadata_rows(connection, tuple(by_match))
    counts = Counter(int(row["match_id"]) for row in metadata)
    duplicate_team_matches = sorted(match_id for match_id, count in counts.items() if count > 1)
    if duplicate_team_matches:
        raise ValueError("multiple settled Team Rating predictions for one match")

    candidates: list[RetrospectiveRow] = []
    for row in metadata:
        match_id = int(row["match_id"])
        legacy = by_match[match_id]
        if (
            row["has_valid_result"] != 1
            or row["is_exhibition"] == 1
            or row["is_forfeit"] == 1
            or row["is_void_remake"] == 1
            or row["radiant_win"] is None
        ):
            continue
        outcome = _outcome(row["radiant_win"])
        series_id = row["series_id"]
        normalized_series = series_id if type(series_id) is int and series_id > 0 else None
        team_probability = row["team_probability"]
        probability = (
            None
            if team_probability is None
            else _finite(team_probability, "team_probability")
        )
        if probability is not None and not 0.0 <= probability <= 1.0:
            raise ValueError("team_probability must be between zero and one")
        if probability is not None:
            if _outcome(row["team_outcome"]) != outcome:
                raise ValueError("Team Rating outcome disagrees with formal result")
            if row["team_rating_version"] != "team-rating-elo-v1":
                raise ValueError("unexpected Team Rating version")
            if row["team_availability_mode"] != "reconstructed_walk_forward":
                raise ValueError("unexpected Team Rating availability mode")
        cutoff = legacy.prediction_cutoff
        candidates.append(
            RetrospectiveRow(
                match_id=match_id,
                score_key=legacy.score_key,
                formula_version=legacy.formula_version,
                prediction_cutoff=cutoff,
                pure_lineup_score=legacy.pure_lineup_score,
                radiant_win=outcome,
                series_id=normalized_series,
                series_key=(
                    f"series:{normalized_series}"
                    if normalized_series is not None
                    else f"match:{match_id}"
                ),
                event_id=str(row["event_id"]),
                patch=(row["patch"] if type(row["patch"]) is int else None),
                month=cutoff.strftime("%Y-%m"),
                team_probability=probability,
            )
        )
    ordered = tuple(sorted(candidates, key=lambda row: (row.prediction_cutoff, row.match_id)))
    paired = tuple(row for row in ordered if row.team_probability is not None)
    return CohortLoadResult(
        candidates=ordered,
        paired=paired,
        canonical_selection=selection,
        evidence_hash_valid=len(valid),
        formal_valid_results=len(ordered),
        missing_team_rating=len(ordered) - len(paired),
        formula_versions=tuple(sorted(Counter(row.formula_version for row in valid).items())),
        source_unchanged=audit.source_unchanged,
    )


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    finite = tuple(_finite(value, "score") for value in values)
    return {
        "support": len(finite),
        "mean": mean(finite) if finite else None,
        "median": median(finite) if finite else None,
        "standard_deviation": stdev(finite) if len(finite) > 1 else None,
        "minimum": min(finite) if finite else None,
        "maximum": max(finite) if finite else None,
    }


def _auc(outcomes: Sequence[int], values: Sequence[float]) -> float | None:
    if len(set(outcomes)) < 2:
        return None
    return float(roc_auc_score(outcomes, values))


def _correlation(outcomes: Sequence[int], scores: Sequence[float]) -> float | None:
    if len(set(outcomes)) < 2 or len(set(scores)) < 2:
        return None
    value = float(pointbiserialr(outcomes, scores).statistic)
    return value if math.isfinite(value) else None


def _neutral_accuracy(outcomes: Sequence[int], scores: Sequence[float]) -> float | None:
    if not scores:
        return None
    correct = 0.0
    for outcome, score in zip(outcomes, scores, strict=True):
        if score == ROSH_NEUTRAL_POINT:
            correct += 0.5
        else:
            correct += float((score > ROSH_NEUTRAL_POINT) == bool(outcome))
    return correct / len(scores)


def _quantile_bins(
    rows: Sequence[RetrospectiveRow],
    bins: int,
) -> tuple[dict[str, float | int], ...]:
    ordered = sorted(rows, key=lambda row: (row.pure_lineup_score, row.match_id))
    grouped: list[list[RetrospectiveRow]] = [[] for _ in range(bins)]
    for index, row in enumerate(ordered):
        grouped[min(bins - 1, (index * bins) // len(ordered))].append(row)
    result: list[dict[str, float | int]] = []
    for index, group in enumerate(grouped, 1):
        scores = [row.pure_lineup_score for row in group]
        result.append(
            {
                "bin": index,
                "support": len(group),
                "score_min": min(scores),
                "score_max": max(scores),
                "score_mean": mean(scores),
                "radiant_win_rate": mean(row.radiant_win for row in group),
            }
        )
    return tuple(result)


def _monotonicity(bins: Sequence[Mapping[str, float | int]]) -> dict[str, float | int | None]:
    rates = [float(row["radiant_win_rate"]) for row in bins]
    statistic = float(spearmanr(range(1, len(rates) + 1), rates).statistic)
    nondecreasing = sum(right >= left for left, right in zip(rates, rates[1:]))
    return {
        "spearman_rho": statistic if math.isfinite(statistic) else None,
        "nondecreasing_steps": nondecreasing,
        "total_steps": max(0, len(rates) - 1),
    }


def _percentile_interval(values: Sequence[float]) -> dict[str, float | int | None]:
    finite = np.asarray([value for value in values if math.isfinite(value)], dtype=np.float64)
    if not len(finite):
        return {"lower": None, "upper": None, "valid_samples": 0}
    lower, upper = np.quantile(finite, (0.025, 0.975))
    return {
        "lower": float(lower),
        "upper": float(upper),
        "valid_samples": int(len(finite)),
    }


def _cluster_sample(
    rows: Sequence[RetrospectiveRow],
    generator: np.random.Generator,
) -> tuple[RetrospectiveRow, ...]:
    clusters: dict[str, list[RetrospectiveRow]] = {}
    for row in rows:
        clusters.setdefault(row.series_key, []).append(row)
    keys = sorted(clusters)
    selected = generator.choice(len(keys), size=len(keys), replace=True)
    return tuple(row for index in selected for row in clusters[keys[int(index)]])


def _standalone_values(rows: Sequence[RetrospectiveRow]) -> dict[str, float | None]:
    outcomes = [row.radiant_win for row in rows]
    scores = [row.pure_lineup_score for row in rows]
    winners = [score for score, outcome in zip(scores, outcomes, strict=True) if outcome]
    losers = [score for score, outcome in zip(scores, outcomes, strict=True) if not outcome]
    return {
        "point_biserial_correlation": _correlation(outcomes, scores),
        "auc": _auc(outcomes, scores),
        "neutral_threshold_accuracy": _neutral_accuracy(outcomes, scores),
        "radiant_win_minus_loss_mean": (
            mean(winners) - mean(losers) if winners and losers else None
        ),
    }


def _standalone_slice(rows: Sequence[RetrospectiveRow]) -> dict[str, Any]:
    values = _standalone_values(rows)
    return {
        "support": len(rows),
        "radiant_win_rate": mean(row.radiant_win for row in rows) if rows else None,
        "score_mean": mean(row.pure_lineup_score for row in rows) if rows else None,
        **values,
    }


def _slice_rows(
    rows: Sequence[RetrospectiveRow],
    field: str,
) -> tuple[dict[str, Any], ...]:
    grouped: dict[str, list[RetrospectiveRow]] = {}
    for row in rows:
        value = getattr(row, field)
        grouped.setdefault("unknown" if value is None else str(value), []).append(row)
    return tuple(
        {"value": value, **_standalone_slice(group)}
        for value, group in sorted(grouped.items())
    )


def analyze_standalone(
    rows: Sequence[RetrospectiveRow],
    *,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
) -> dict[str, Any]:
    values = tuple(rows)
    if len(values) < 2:
        raise ValueError("standalone analysis requires at least two rows")
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")
    outcomes = [row.radiant_win for row in values]
    scores = [row.pure_lineup_score for row in values]
    radiant_win_scores = [score for score, outcome in zip(scores, outcomes, strict=True) if outcome]
    dire_win_scores = [score for score, outcome in zip(scores, outcomes, strict=True) if not outcome]
    winner_aligned = [score if outcome else -score for score, outcome in zip(scores, outcomes, strict=True)]
    quintiles = _quantile_bins(values, 5)
    deciles = _quantile_bins(values, 10)
    generator = np.random.default_rng(_seed(BOOTSTRAP_SEED))
    estimates: dict[str, list[float]] = {
        key: [] for key in _standalone_values(values)
    }
    for _sample in range(bootstrap_samples):
        sample_values = _standalone_values(_cluster_sample(values, generator))
        for key, value in sample_values.items():
            if value is not None:
                estimates[key].append(value)
    return {
        "support": len(values),
        "formula_direction": ROSH_SCORE_DIRECTION,
        "neutral_point": ROSH_NEUTRAL_POINT,
        "score_distribution": _distribution(scores),
        "radiant_winner_score_distribution": _distribution(radiant_win_scores),
        "dire_winner_score_distribution": _distribution(dire_win_scores),
        "winner_aligned_score_distribution": _distribution(winner_aligned),
        **_standalone_values(values),
        "series_clustered_bootstrap_95": {
            key: _percentile_interval(sample) for key, sample in estimates.items()
        },
        "quintiles": quintiles,
        "quintile_monotonicity": _monotonicity(quintiles),
        "deciles": deciles,
        "decile_monotonicity": _monotonicity(deciles),
        "slices": {
            "patch": _slice_rows(values, "patch"),
            "event": _slice_rows(values, "event_id"),
            "month": _slice_rows(values, "month"),
        },
    }


def _binary_metrics(outcomes: Sequence[int], probabilities: Sequence[float]) -> dict[str, float | int | None]:
    observed = np.asarray(outcomes, dtype=np.int64)
    predicted = np.clip(np.asarray(probabilities, dtype=np.float64), _PROBABILITY_EPSILON, 1.0 - _PROBABILITY_EPSILON)
    if observed.shape != predicted.shape or observed.ndim != 1:
        raise ValueError("outcomes and probabilities must be aligned vectors")
    return {
        "support": int(len(observed)),
        "brier_score": float(np.mean((predicted - observed) ** 2)),
        "log_loss": float(-np.mean(observed * np.log(predicted) + (1 - observed) * np.log1p(-predicted))),
        "auc": _auc(observed.tolist(), predicted.tolist()),
        "accuracy": float(np.mean((predicted >= 0.5) == observed)),
    }


def _fit_beta(scores: np.ndarray, offsets: np.ndarray, outcomes: np.ndarray) -> tuple[float, float]:
    def objective(parameters: np.ndarray) -> tuple[float, np.ndarray]:
        beta = float(parameters[0])
        logits = offsets + beta * scores
        loss = float(np.mean(np.logaddexp(0.0, logits) - outcomes * logits))
        gradient = np.asarray([np.mean((expit(logits) - outcomes) * scores)])
        return loss, gradient

    result = minimize(
        objective,
        np.zeros(1, dtype=np.float64),
        jac=True,
        method="L-BFGS-B",
    )
    if not result.success or not np.all(np.isfinite(result.x)):
        raise RuntimeError(f"R.O.S.H. beta optimization failed: {result.message}")
    return float(result.x[0]), float(result.fun)


def cross_validate_increment(
    rows: Sequence[RetrospectiveRow],
    *,
    score_override: Sequence[float] | None = None,
    outcome_override: Sequence[int] | None = None,
) -> dict[str, Any]:
    values = tuple(rows)
    if len(values) < 5:
        raise ValueError("incremental analysis requires at least five rows")
    scores = np.asarray(
        score_override if score_override is not None else [row.pure_lineup_score for row in values],
        dtype=np.float64,
    )
    outcomes = np.asarray(
        outcome_override if outcome_override is not None else [row.radiant_win for row in values],
        dtype=np.int64,
    )
    if scores.shape != (len(values),) or outcomes.shape != (len(values),):
        raise ValueError("override vectors must match the paired cohort")
    probabilities = np.asarray([_finite(row.team_probability, "team_probability") for row in values], dtype=np.float64)
    offsets = logit(np.clip(probabilities, _PROBABILITY_EPSILON, 1.0 - _PROBABILITY_EPSILON))
    groups = np.asarray([row.series_key for row in values], dtype=object)
    if len(set(groups.tolist())) < 5:
        raise ValueError("series-grouped 5-fold CV requires at least five groups")
    splitter = GroupKFold(n_splits=5)
    m1 = np.empty(len(values), dtype=np.float64)
    fold_ids = np.empty(len(values), dtype=np.int64)
    standardized = np.empty(len(values), dtype=np.float64)
    folds: list[dict[str, float | int]] = []
    for fold, (train, test) in enumerate(splitter.split(scores, outcomes, groups), 1):
        train_mean = float(np.mean(scores[train]))
        train_scale = float(np.std(scores[train]))
        if not math.isfinite(train_scale) or train_scale <= 0.0:
            raise ValueError("training-fold R.O.S.H. score variance is zero")
        train_scores = (scores[train] - train_mean) / train_scale
        test_scores = (scores[test] - train_mean) / train_scale
        beta, objective = _fit_beta(train_scores, offsets[train], outcomes[train])
        m1[test] = expit(offsets[test] + beta * test_scores)
        fold_ids[test] = fold
        standardized[test] = test_scores
        folds.append(
            {
                "fold": fold,
                "train_support": len(train),
                "test_support": len(test),
                "train_score_mean": train_mean,
                "train_score_scale": train_scale,
                "beta": beta,
                "train_log_loss": objective,
            }
        )
    oof = tuple(
        {
            "match_id": row.match_id,
            "score_key": row.score_key,
            "formula_version": row.formula_version,
            "prediction_cutoff": row.prediction_cutoff.isoformat(),
            "series_key": row.series_key,
            "event_id": row.event_id,
            "patch": row.patch,
            "month": row.month,
            "fold": int(fold_ids[index]),
            "outcome": int(outcomes[index]),
            "pure_lineup_score": float(scores[index]),
            "standardized_pure_lineup_score": float(standardized[index]),
            "m0_team_probability": float(probabilities[index]),
            "m1_probability": float(m1[index]),
        }
        for index, row in enumerate(values)
    )
    return {
        "folds": tuple(folds),
        "oof_predictions": oof,
        "m0": _binary_metrics(outcomes.tolist(), probabilities.tolist()),
        "m1": _binary_metrics(outcomes.tolist(), m1.tolist()),
    }


_METRIC_DIRECTIONS = {
    "brier_score": -1,
    "log_loss": -1,
    "auc": 1,
    "accuracy": 1,
}


def _metric_deltas(m0: Mapping[str, Any], m1: Mapping[str, Any]) -> dict[str, float | None]:
    return {
        metric: (
            None
            if m0[metric] is None or m1[metric] is None
            else float(m1[metric]) - float(m0[metric])
        )
        for metric in _METRIC_DIRECTIONS
    }


def _oof_metrics(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, float | None]]:
    outcomes = [int(row["outcome"]) for row in rows]
    m0 = _binary_metrics(outcomes, [float(row["m0_team_probability"]) for row in rows])
    m1 = _binary_metrics(outcomes, [float(row["m1_probability"]) for row in rows])
    return m0, m1, _metric_deltas(m0, m1)


def _increment_bootstrap(
    oof: Sequence[Mapping[str, Any]],
    *,
    samples: int,
) -> dict[str, dict[str, float | int | None]]:
    clusters: dict[str, list[Mapping[str, Any]]] = {}
    for row in oof:
        clusters.setdefault(str(row["series_key"]), []).append(row)
    keys = sorted(clusters)
    generator = np.random.default_rng(_seed(BOOTSTRAP_SEED + ":increment"))
    estimates: dict[str, list[float]] = {metric: [] for metric in _METRIC_DIRECTIONS}
    for _sample in range(samples):
        selected = generator.choice(len(keys), size=len(keys), replace=True)
        sample = tuple(row for index in selected for row in clusters[keys[int(index)]])
        _m0, _m1, deltas = _oof_metrics(sample)
        for metric, value in deltas.items():
            if value is not None:
                estimates[metric].append(value)
    return {metric: _percentile_interval(values) for metric, values in estimates.items()}


def _increment_slice(oof: Sequence[Mapping[str, Any]], field: str) -> tuple[dict[str, Any], ...]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in oof:
        value = row[field]
        grouped.setdefault("unknown" if value is None else str(value), []).append(row)
    result: list[dict[str, Any]] = []
    for value, rows in sorted(grouped.items()):
        m0, m1, deltas = _oof_metrics(rows)
        result.append(
            {
                "value": value,
                "support": len(rows),
                "m0": m0,
                "m1": m1,
                "delta_m1_minus_m0": deltas,
            }
        )
    return tuple(result)


def _slice_direction_stability(
    slices: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, float | int | None]]:
    result: dict[str, dict[str, float | int | None]] = {}
    for metric, direction in _METRIC_DIRECTIONS.items():
        deltas = [
            float(row["delta_m1_minus_m0"][metric])
            for row in slices
            if row["delta_m1_minus_m0"][metric] is not None
        ]
        favorable = sum(delta * direction > 0.0 for delta in deltas)
        result[metric] = {
            "evaluable_slices": len(deltas),
            "favorable_slices": favorable,
            "favorable_fraction": favorable / len(deltas) if deltas else None,
        }
    return result


def _sanity_summary(
    estimates: Mapping[str, Sequence[float]],
    observed: Mapping[str, float | None],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for metric, values in estimates.items():
        interval = _percentile_interval(values)
        direction = _METRIC_DIRECTIONS[metric]
        observed_value = observed[metric]
        result[metric] = {
            "mean_delta": mean(values) if values else None,
            "delta_95": interval,
            "favorable_fraction": (
                sum(value * direction > 0.0 for value in values) / len(values)
                if values
                else None
            ),
            "empirical_probability_as_or_more_favorable_than_observed": (
                None
                if observed_value is None or not values
                else (
                    1
                    + sum(value * direction >= observed_value * direction for value in values)
                )
                / (len(values) + 1)
            ),
        }
    return result


def run_sanity_checks(
    rows: Sequence[RetrospectiveRow],
    observed_deltas: Mapping[str, float | None],
    *,
    permutations: int = DEFAULT_SANITY_PERMUTATIONS,
) -> dict[str, Any]:
    if permutations < 1:
        raise ValueError("sanity permutations must be positive")
    values = tuple(rows)
    scores = np.asarray([row.pure_lineup_score for row in values], dtype=np.float64)
    outcomes = np.asarray([row.radiant_win for row in values], dtype=np.int64)
    generator = np.random.default_rng(_seed(SANITY_SEED))
    score_estimates: dict[str, list[float]] = {metric: [] for metric in _METRIC_DIRECTIONS}
    label_estimates: dict[str, list[float]] = {metric: [] for metric in _METRIC_DIRECTIONS}
    for _permutation in range(permutations):
        score_result = cross_validate_increment(values, score_override=generator.permutation(scores))
        score_deltas = _metric_deltas(score_result["m0"], score_result["m1"])
        label_result = cross_validate_increment(values, outcome_override=generator.permutation(outcomes))
        label_deltas = _metric_deltas(label_result["m0"], label_result["m1"])
        for metric in _METRIC_DIRECTIONS:
            if score_deltas[metric] is not None:
                score_estimates[metric].append(float(score_deltas[metric]))
            if label_deltas[metric] is not None:
                label_estimates[metric].append(float(label_deltas[metric]))
    return {
        "permutations": permutations,
        "score_permutation_baseline": _sanity_summary(score_estimates, observed_deltas),
        "label_permutation_baseline": _sanity_summary(label_estimates, observed_deltas),
    }


def _ci_excludes(interval: Mapping[str, Any], null: float) -> bool:
    lower = interval.get("lower")
    upper = interval.get("upper")
    return (
        isinstance(lower, (int, float))
        and isinstance(upper, (int, float))
        and (float(lower) > null or float(upper) < null)
    )


def _conclusion(
    standalone: Mapping[str, Any],
    incremental: Mapping[str, Any],
    sanity: Mapping[str, Any],
) -> str:
    standalone_ci = standalone["series_clustered_bootstrap_95"]
    standalone_stable = _ci_excludes(standalone_ci["point_biserial_correlation"], 0.0) and _ci_excludes(standalone_ci["auc"], 0.5)
    delta_ci = incremental["series_clustered_bootstrap_delta_95"]
    stable_losses = all(
        delta_ci[metric]["upper"] is not None
        and float(delta_ci[metric]["upper"]) < 0.0
        for metric in ("brier_score", "log_loss")
    )
    score_baseline = sanity["score_permutation_baseline"]
    permutation_distinct = all(
        score_baseline[metric]["empirical_probability_as_or_more_favorable_than_observed"] <= 0.05
        for metric in ("brier_score", "log_loss")
    )
    label_baseline = sanity["label_permutation_baseline"]
    label_not_stable = all(
        not _ci_excludes(label_baseline[metric]["delta_95"], 0.0)
        for metric in ("brier_score", "log_loss")
    )
    if stable_losses and permutation_distinct and label_not_stable:
        conclusion = "incremental retrospective information beyond Team Rating"
    elif standalone_stable:
        conclusion = "standalone retrospective association only"
    else:
        deltas = incremental["delta_m1_minus_m0"]
        point_signal = (
            abs(float(standalone["auc"]) - 0.5) >= 0.02
            or abs(float(standalone["point_biserial_correlation"])) >= 0.05
            or any(
                deltas[metric] is not None and float(deltas[metric]) < 0.0
                for metric in ("brier_score", "log_loss")
            )
        )
        conclusion = (
            "unstable / inconclusive retrospective evidence"
            if point_signal
            else "no retrospective association detected"
        )
    if conclusion not in ALLOWED_CONCLUSIONS:
        raise AssertionError("invalid retrospective conclusion")
    return conclusion


def analyze_incremental(
    rows: Sequence[RetrospectiveRow],
    *,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    sanity_permutations: int = DEFAULT_SANITY_PERMUTATIONS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cross_validated = cross_validate_increment(rows)
    deltas = _metric_deltas(cross_validated["m0"], cross_validated["m1"])
    oof = cross_validated["oof_predictions"]
    slices = {
        "patch": _increment_slice(oof, "patch"),
        "event": _increment_slice(oof, "event_id"),
        "month": _increment_slice(oof, "month"),
    }
    incremental = {
        "support": len(rows),
        "cv": "series_grouped_5_fold",
        "model_m0": "fixed Team Rating raw probability",
        "model_m1": "logit(P_team) + beta * train_fold_standardized_pure_lineup_score",
        "folds": cross_validated["folds"],
        "m0": cross_validated["m0"],
        "m1": cross_validated["m1"],
        "delta_m1_minus_m0": deltas,
        "series_clustered_bootstrap_delta_95": _increment_bootstrap(oof, samples=bootstrap_samples),
        "slices": slices,
        "slice_direction_stability": {
            dimension: _slice_direction_stability(rows)
            for dimension, rows in slices.items()
        },
        "oof_predictions_hash": _hash(oof),
        "oof_predictions": oof,
    }
    sanity = run_sanity_checks(rows, deltas, permutations=sanity_permutations)
    return incremental, sanity


def build_analysis(
    cohort: CohortLoadResult,
    *,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    sanity_permutations: int = DEFAULT_SANITY_PERMUTATIONS,
) -> dict[str, Any]:
    standalone = analyze_standalone(cohort.candidates, bootstrap_samples=bootstrap_samples)
    incremental, sanity = analyze_incremental(
        cohort.paired,
        bootstrap_samples=bootstrap_samples,
        sanity_permutations=sanity_permutations,
    )
    conclusion = _conclusion(standalone, incremental, sanity)
    return {
        "version": ROSH_RETROSPECTIVE_UTILITY_VERSION,
        "analysis_mode": "retrospective_exploratory",
        "leakage_free_oos": False,
        "deployment_evidence": False,
        "bootstrap_samples": bootstrap_samples,
        "sanity_permutations": sanity_permutations,
        "formula_version": LEGACY_ROSH_FORMULA_VERSION,
        "formula_direction": ROSH_SCORE_DIRECTION,
        "neutral_point": ROSH_NEUTRAL_POINT,
        "rosh_input_fields": ["pure_lineup_score"],
        "baseline_input": "team_rating_predictions.raw_probability",
        "forbidden_fields_used": [],
        "cohort": {
            "candidate_rows_before_dedup": cohort.canonical_selection.rows_before,
            "evidence_hash_valid": cohort.evidence_hash_valid,
            "canonical_selection": asdict(cohort.canonical_selection),
            "formal_valid_results": cohort.formal_valid_results,
            "paired_team_rating_support": len(cohort.paired),
            "missing_team_rating": cohort.missing_team_rating,
            "standalone_series_clusters": len(
                {row.series_key for row in cohort.candidates}
            ),
            "standalone_missing_series_ids": sum(
                row.series_id is None for row in cohort.candidates
            ),
            "paired_series_clusters": len({row.series_key for row in cohort.paired}),
            "paired_missing_series_ids": sum(
                row.series_id is None for row in cohort.paired
            ),
            "events": sorted({row.event_id for row in cohort.candidates}),
            "patches": sorted(
                {row.patch for row in cohort.candidates if row.patch is not None}
            ),
            "months": sorted({row.month for row in cohort.candidates}),
            "prediction_cutoff_start": min(
                row.prediction_cutoff for row in cohort.candidates
            ).isoformat(),
            "prediction_cutoff_end": max(
                row.prediction_cutoff for row in cohort.candidates
            ).isoformat(),
            "formula_versions": [
                {"value": value, "support": support}
                for value, support in cohort.formula_versions
            ],
            "source_unchanged": cohort.source_unchanged,
        },
        "standalone": standalone,
        "incremental": incremental,
        "sanity_checks": sanity,
        "conclusion": conclusion,
    }


def analysis_as_json(analysis: Mapping[str, Any]) -> str:
    return json.dumps(
        analysis,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _fmt(value: object, digits: int = 6) -> str:
    if value is None:
        return "NA"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _ci(value: Mapping[str, Any]) -> str:
    return f"[{_fmt(value['lower'])}, {_fmt(value['upper'])}]"


def _distribution_table(distributions: Sequence[tuple[str, Mapping[str, Any]]]) -> list[str]:
    lines = [
        "| Cohort | Support | Mean | Median | SD | Min | Max |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    lines.extend(
        "| " + " | ".join(
            (
                name,
                _fmt(row["support"]),
                _fmt(row["mean"]),
                _fmt(row["median"]),
                _fmt(row["standard_deviation"]),
                _fmt(row["minimum"]),
                _fmt(row["maximum"]),
            )
        ) + " |"
        for name, row in distributions
    )
    return lines


def _standalone_slice_table(slices: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| Slice | Support | Win rate | Score mean | Correlation | AUC | Threshold accuracy |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {row['value']} | {_fmt(row['support'])} | {_fmt(row['radiant_win_rate'])} | "
        f"{_fmt(row['score_mean'])} | {_fmt(row['point_biserial_correlation'])} | "
        f"{_fmt(row['auc'])} | {_fmt(row['neutral_threshold_accuracy'])} |"
        for row in slices
    )
    return lines


def _increment_slice_table(slices: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| Slice | Support | Brier delta | Log-loss delta | AUC delta | Accuracy delta |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {row['value']} | {_fmt(row['support'])} | "
        f"{_fmt(row['delta_m1_minus_m0']['brier_score'])} | "
        f"{_fmt(row['delta_m1_minus_m0']['log_loss'])} | "
        f"{_fmt(row['delta_m1_minus_m0']['auc'])} | "
        f"{_fmt(row['delta_m1_minus_m0']['accuracy'])} |"
        for row in slices
    )
    return lines


def analysis_as_markdown(analysis: Mapping[str, Any]) -> str:
    cohort = analysis["cohort"]
    standalone = analysis["standalone"]
    incremental = analysis["incremental"]
    sanity = analysis["sanity_checks"]
    selection = cohort["canonical_selection"]
    lines = [
        "# R.O.S.H. Retrospective Utility Analysis",
        "",
        "## Scope",
        "",
        "This is a retrospective exploratory analysis. It does not revalidate historical cutoff semantics, require official-v2 lineage, or constitute leakage-free OOS or deployment evidence. It uses only the legacy `pure_lineup_score`; player-adjusted/effective scores, Draft, Cluster, Player-Hero, and odds are excluded.",
        "",
        f"- Version: `{analysis['version']}`",
        f"- Formula: `{analysis['formula_version']}`",
        f"- Mode: `{analysis['analysis_mode']}`",
        f"- Series-clustered bootstrap samples: {analysis['bootstrap_samples']}",
        f"- Sanity permutations per baseline: {analysis['sanity_permutations']}",
        f"- Source unchanged: `{str(cohort['source_unchanged']).lower()}`",
        "",
        "## Direction and Canonical Selection",
        "",
        "The frozen formula defines positive `pure_lineup_score` as Radiant lineup advantage, negative as Dire lineup advantage, and `0.0` as neutral. This direction was fixed from repository code before reading outcomes and was not flipped after observing results.",
        "",
        f"Canonical rule: `{selection['rule']}`.",
        "",
        "| Stage | Support |",
        "| --- | ---: |",
        f"| candidate rows | {cohort['candidate_rows_before_dedup']} |",
        f"| evidence hash valid | {cohort['evidence_hash_valid']} |",
        f"| duplicate match/formula groups | {selection['duplicate_groups']} |",
        f"| duplicate rows | {selection['duplicate_rows']} |",
        f"| conflicting-score duplicate groups | {selection['conflicting_score_groups']} |",
        f"| canonical maps | {selection['rows_after']} |",
        f"| formal valid results | {cohort['formal_valid_results']} |",
        f"| paired Team Rating cohort | {cohort['paired_team_rating_support']} |",
        f"| missing Team Rating prediction | {cohort['missing_team_rating']} |",
        f"| standalone series clusters | {cohort['standalone_series_clusters']} |",
        f"| missing series IDs (singleton match clusters) | {cohort['standalone_missing_series_ids']} |",
        "",
        f"Scope: patch `{', '.join(str(value) for value in cohort['patches'])}`; events `{len(cohort['events'])}`; months `{', '.join(cohort['months'])}`; prediction cutoffs `{cohort['prediction_cutoff_start']}` through `{cohort['prediction_cutoff_end']}`.",
        "",
        "## Analysis 1: Standalone Utility",
        "",
        f"Support: **{standalone['support']}** maps.",
        "",
    ]
    lines.extend(
        _distribution_table(
            (
                ("All maps", standalone["score_distribution"]),
                ("Radiant winners", standalone["radiant_winner_score_distribution"]),
                ("Dire winners", standalone["dire_winner_score_distribution"]),
                ("Winner-aligned", standalone["winner_aligned_score_distribution"]),
            )
        )
    )
    bootstrap = standalone["series_clustered_bootstrap_95"]
    lines.extend(
        (
            "",
            "| Metric | Estimate | Series-clustered 95% CI |",
            "| --- | ---: | ---: |",
            f"| Point-biserial correlation | {_fmt(standalone['point_biserial_correlation'])} | {_ci(bootstrap['point_biserial_correlation'])} |",
            f"| AUC | {_fmt(standalone['auc'])} | {_ci(bootstrap['auc'])} |",
            f"| Neutral-threshold accuracy | {_fmt(standalone['neutral_threshold_accuracy'])} | {_ci(bootstrap['neutral_threshold_accuracy'])} |",
            f"| Radiant-win minus Dire-win mean score | {_fmt(standalone['radiant_win_minus_loss_mean'])} | {_ci(bootstrap['radiant_win_minus_loss_mean'])} |",
            "",
            "Exact-zero scores receive 0.5 credit in neutral-threshold accuracy; positive predicts Radiant and negative predicts Dire.",
            "",
        )
    )
    for name, bins, monotonicity in (
        ("Quintiles", standalone["quintiles"], standalone["quintile_monotonicity"]),
        ("Deciles", standalone["deciles"], standalone["decile_monotonicity"]),
    ):
        lines.extend((f"### {name}", "", "| Bin | Support | Score min | Score max | Score mean | Radiant win rate |", "| ---: | ---: | ---: | ---: | ---: | ---: |"))
        lines.extend(
            f"| {row['bin']} | {row['support']} | {_fmt(row['score_min'])} | {_fmt(row['score_max'])} | {_fmt(row['score_mean'])} | {_fmt(row['radiant_win_rate'])} |"
            for row in bins
        )
        lines.extend(("", f"Monotonicity: Spearman rho `{_fmt(monotonicity['spearman_rho'])}`; nondecreasing adjacent steps `{monotonicity['nondecreasing_steps']}/{monotonicity['total_steps']}`.", ""))
    lines.extend(("Quantile bins are equal-count bins ordered by fixed score direction and then `match_id`; identical boundary scores may appear in adjacent bins.", ""))
    for dimension in ("patch", "event", "month"):
        lines.extend((f"### Standalone {dimension} slices", ""))
        lines.extend(_standalone_slice_table(standalone["slices"][dimension]))
        lines.append("")
    lines.extend(
        (
            "## Analysis 2: Incremental Utility over Team Rating",
            "",
            f"Paired support: **{incremental['support']}** maps. Team Rating is a fixed logit offset. M1 adds exactly one coefficient on the training-fold-standardized pure R.O.S.H. score. Predictions are series-grouped five-fold out-of-fold predictions, but they remain retrospective and are not leakage-free OOS evidence.",
            "",
            f"OOF prediction hash: `{incremental['oof_predictions_hash']}`.",
            "",
            "| Model | Brier | Log loss | AUC | Accuracy |",
            "| --- | ---: | ---: | ---: | ---: |",
            f"| M0 Team Rating-only | {_fmt(incremental['m0']['brier_score'])} | {_fmt(incremental['m0']['log_loss'])} | {_fmt(incremental['m0']['auc'])} | {_fmt(incremental['m0']['accuracy'])} |",
            f"| M1 Team Rating + pure R.O.S.H. | {_fmt(incremental['m1']['brier_score'])} | {_fmt(incremental['m1']['log_loss'])} | {_fmt(incremental['m1']['auc'])} | {_fmt(incremental['m1']['accuracy'])} |",
            "",
            "| M1-M0 metric | Delta | Series-clustered 95% CI |",
            "| --- | ---: | ---: |",
        )
    )
    for metric in _METRIC_DIRECTIONS:
        lines.append(f"| {metric} | {_fmt(incremental['delta_m1_minus_m0'][metric])} | {_ci(incremental['series_clustered_bootstrap_delta_95'][metric])} |")
    lines.extend(("", "For Brier/log loss, negative deltas favor M1; for AUC/accuracy, positive deltas favor M1.", "", "### Fold coefficients", "", "| Fold | Train | Test | Train mean | Train scale | Beta |", "| ---: | ---: | ---: | ---: | ---: | ---: |"))
    lines.extend(
        f"| {row['fold']} | {row['train_support']} | {row['test_support']} | {_fmt(row['train_score_mean'])} | {_fmt(row['train_score_scale'])} | {_fmt(row['beta'])} |"
        for row in incremental["folds"]
    )
    lines.append("")
    for dimension in ("patch", "event", "month"):
        lines.extend((f"### Incremental {dimension} direction", ""))
        lines.extend(_increment_slice_table(incremental["slices"][dimension]))
        lines.extend(("", "Direction stability:", "", "| Metric | Favorable slices | Evaluable slices | Fraction |", "| --- | ---: | ---: | ---: |"))
        for metric in _METRIC_DIRECTIONS:
            row = incremental["slice_direction_stability"][dimension][metric]
            lines.append(f"| {metric} | {row['favorable_slices']} | {row['evaluable_slices']} | {_fmt(row['favorable_fraction'])} |")
        lines.append("")
    lines.extend(("## Sanity Checks", "", f"Each baseline uses {sanity['permutations']} deterministic permutations and reruns the same grouped CV with train-fold-only standardization.", ""))
    for title, key in (("R.O.S.H. score permutation", "score_permutation_baseline"), ("Outcome-label permutation", "label_permutation_baseline")):
        lines.extend((f"### {title}", "", "| Metric | Mean delta | 95% range | Favorable fraction | Empirical probability as/more favorable than observed |", "| --- | ---: | ---: | ---: | ---: |"))
        for metric in _METRIC_DIRECTIONS:
            row = sanity[key][metric]
            lines.append(f"| {metric} | {_fmt(row['mean_delta'])} | {_ci(row['delta_95'])} | {_fmt(row['favorable_fraction'])} | {_fmt(row['empirical_probability_as_or_more_favorable_than_observed'])} |")
        lines.append("")
    lines.extend(
        (
            "## Conclusion",
            "",
            f"**{analysis['conclusion']}**",
            "",
            "This conclusion is limited to association in the saved retrospective data. The legacy audit already established that these scores were generated after prediction cutoff and lack replayable raw normalized statistics. Grouped CV cannot remove that leakage risk. The result therefore does not authorize a model change, Calibration change, Deployment freeze, production prediction, or order creation.",
            "",
            "The complete local JSON contains every out-of-fold prediction and is intentionally kept under ignored `dogfood-output` rather than committed.",
        )
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "ALLOWED_CONCLUSIONS",
    "BOOTSTRAP_SEED",
    "CanonicalSelection",
    "CohortLoadResult",
    "DEFAULT_BOOTSTRAP_SAMPLES",
    "DEFAULT_SANITY_PERMUTATIONS",
    "LegacyPureScore",
    "ROSH_NEUTRAL_POINT",
    "ROSH_RETROSPECTIVE_UTILITY_VERSION",
    "ROSH_SCORE_DIRECTION",
    "RetrospectiveRow",
    "analysis_as_json",
    "analysis_as_markdown",
    "analyze_incremental",
    "analyze_standalone",
    "build_analysis",
    "canonicalize_legacy_scores",
    "cross_validate_increment",
    "load_retrospective_cohort",
    "run_sanity_checks",
]
