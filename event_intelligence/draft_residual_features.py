"""Team-adjusted draft residual features with causal replay authority."""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from itertools import combinations
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .draft_features import (
    FEATURE_SCHEMA_HASH as DRAFT_V3_SCHEMA_HASH,
    FEATURE_VERSION as DRAFT_V3_FEATURE_VERSION,
    MIN_FEATURE_SUPPORT,
    ROLE_CONFIDENCE_MIN,
    AvailabilityMode,
    DraftMapEvidence,
    DraftTarget,
    build_draft_feature_snapshot_with_authority,
    replay_draft_feature_snapshot,
)
from .raw_archive import canonical_json_bytes
from .team_rating_artifacts import verify_team_rating_artifact
from .team_rating_backtest import (
    TeamRatingWalkForwardRun,
    combined_team_rating_training_input_hash,
    team_rating_authority_fingerprint,
    team_rating_run_id,
)


UTC = timezone.utc
DRAFT_RESIDUAL_FEATURE_VERSION = "draft-residual-features-v1"
DRAFT_RESIDUAL_AUTHORITY_SCHEMA = "draft-residual-authority/v1"
SHRINKAGE_STRENGTH = 10.0
RECONSTRUCTION_RULE = "m2-reconstructed-map-start/v1"

DRAFT_RESIDUAL_PURE_SCHEMA = (
    "hero_residual_diff",
    "role_residual_diff",
    "synergy_residual_diff",
    "counter_residual_edge",
    "scaling_40m_residual_diff",
    "control_initiation_proxy_diff",
    "save_sustain_proxy_diff",
    "wave_clear_proxy_diff",
    "push_high_ground_proxy_diff",
    "farm_demand_balance_diff",
)
DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH = hashlib.sha256(
    canonical_json_bytes(list(DRAFT_RESIDUAL_PURE_SCHEMA))
).hexdigest()
DRAFT_RESIDUAL_MODEL_SCHEMA = tuple(
    projected
    for name in DRAFT_RESIDUAL_PURE_SCHEMA
    for projected in (
        name,
        f"{name}__log1p_support",
        f"{name}__coverage",
        f"{name}__missing",
    )
)
DRAFT_RESIDUAL_MODEL_SCHEMA_HASH = hashlib.sha256(
    canonical_json_bytes(list(DRAFT_RESIDUAL_MODEL_SCHEMA))
).hexdigest()

_PROXY_NAMES = DRAFT_RESIDUAL_PURE_SCHEMA[5:]


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    return _utc(parsed, field)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _probability(value: object, field: str) -> float:
    result = _finite(value, field)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{field} must be between zero and one")
    return result


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value


def _exact_object(
    value: object,
    fields: Sequence[str],
    field: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise ValueError(f"{field} fields do not match the schema")
    return value


@dataclass(frozen=True)
class TeamRatingResidualEvidence:
    """Bounded M2 claim; public builders create it only from a replayed run."""

    match_id: int
    target_started_at: datetime
    prediction_cutoff: datetime
    first_usable_at: datetime | None
    availability_mode: str
    cutoff_source: str
    radiant_team_id: int
    dire_team_id: int
    radiant_probability: float
    prediction_input_hash: str
    artifact_hash: str
    artifact_training_input_hash: str
    run_id: str
    authority_fingerprint: str
    combined_training_input_hash: str
    reconstruction_rule: str | None
    radiant_win: bool | None
    evidence_hash: str = ""

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "Team Rating evidence match_id")
        started_at = _utc(self.target_started_at, "Team Rating target_started_at")
        cutoff = _utc(self.prediction_cutoff, "Team Rating prediction_cutoff")
        usable = (
            None
            if self.first_usable_at is None
            else _utc(self.first_usable_at, "Team Rating first_usable_at")
        )
        mode = AvailabilityMode(self.availability_mode)
        cutoff_source = _nonempty(self.cutoff_source, "Team Rating cutoff_source")
        radiant_team_id = _positive_int(
            self.radiant_team_id, "Team Rating radiant_team_id"
        )
        dire_team_id = _positive_int(
            self.dire_team_id, "Team Rating dire_team_id"
        )
        if radiant_team_id == dire_team_id:
            raise ValueError("Team Rating evidence team IDs must differ")
        object.__setattr__(
            self,
            "radiant_probability",
            _probability(self.radiant_probability, "Team Rating probability"),
        )
        for name in (
            "prediction_input_hash",
            "artifact_hash",
            "artifact_training_input_hash",
            "run_id",
            "authority_fingerprint",
            "combined_training_input_hash",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if self.radiant_win is not None and not isinstance(self.radiant_win, bool):
            raise ValueError("Team Rating evidence outcome must be boolean or absent")
        if mode is AvailabilityMode.RECONSTRUCTED:
            if (
                cutoff_source != "reconstructed_map_start"
                or self.reconstruction_rule != RECONSTRUCTION_RULE
                or usable is not None
                or cutoff != started_at
            ):
                raise ValueError("reconstructed Team Rating evidence is not canonical")
        else:
            if (
                not cutoff_source.startswith("prospective_")
                or self.reconstruction_rule is not None
                or usable is None
                or usable > cutoff
                or cutoff > started_at
            ):
                raise ValueError("prospective Team Rating evidence is not cutoff-legal")
        object.__setattr__(self, "prediction_cutoff", cutoff)
        object.__setattr__(self, "target_started_at", started_at)
        object.__setattr__(self, "first_usable_at", usable)
        expected_hash = _hash(self.to_payload(include_hash=False))
        if not self.evidence_hash:
            object.__setattr__(self, "evidence_hash", expected_hash)
        elif not hmac.compare_digest(
            _sha256(self.evidence_hash, "Team Rating evidence_hash"),
            expected_hash,
        ):
            raise ValueError("Team Rating residual evidence hash does not recompute")

    @classmethod
    def from_walk_forward_run(
        cls,
        run: TeamRatingWalkForwardRun,
        *,
        include_outcome: bool,
    ) -> TeamRatingResidualEvidence:
        if not isinstance(run, TeamRatingWalkForwardRun):
            raise ValueError("Team Rating evidence requires a walk-forward run")
        if not isinstance(include_outcome, bool):
            raise ValueError("include_outcome must be boolean")
        verify_team_rating_artifact(run.artifact)
        expected_support = len(run.artifact.ordered_training_corpus)
        if run.selection.support != expected_support:
            raise ValueError("Team Rating selection support does not match its corpus")
        expected_status = (
            "trained" if expected_support > 0 else "insufficient_evidence"
        )
        if run.status != expected_status:
            raise ValueError("Team Rating run status does not match its corpus")
        if expected_status != "trained":
            raise ValueError("Team Rating residual evidence requires a trained run")
        if include_outcome and not isinstance(run.eventual_radiant_win, bool):
            raise ValueError("historical Team Rating evidence requires an outcome")
        if run.config != run.artifact.config:
            raise ValueError("Team Rating run config does not match its artifact")
        if run.target_source_authority.match_id != run.artifact.target.match_id:
            raise ValueError("Team Rating target source does not match its artifact")
        if tuple(source.match_id for source in run.ordered_training_sources) != tuple(
            row.match_id for row in run.artifact.ordered_training_corpus
        ):
            raise ValueError("Team Rating source manifest does not match its corpus")
        expected_authority = team_rating_authority_fingerprint(
            target_source=run.target_source_authority,
            ordered_training_sources=run.ordered_training_sources,
        )
        if not hmac.compare_digest(run.authority_fingerprint, expected_authority):
            raise ValueError("Team Rating authority fingerprint does not recompute")
        expected_training = combined_team_rating_training_input_hash(
            artifact_training_input_hash=run.artifact.training_input_hash,
            authority_fingerprint=expected_authority,
        )
        if not hmac.compare_digest(
            run.combined_training_input_hash,
            expected_training,
        ):
            raise ValueError("Team Rating combined training hash does not recompute")
        expected_run_id = team_rating_run_id(
            availability_mode=run.availability_mode,
            artifact_hash=run.artifact.artifact_hash,
            authority_fingerprint=expected_authority,
        )
        if not hmac.compare_digest(run.run_id, expected_run_id):
            raise ValueError("Team Rating run_id does not recompute")
        artifact = run.artifact
        prediction = artifact.prediction
        target = artifact.target
        mode = AvailabilityMode(run.availability_mode)
        return cls(
            match_id=target.match_id,
            target_started_at=target.started_at,
            prediction_cutoff=prediction.prediction_cutoff,
            first_usable_at=(
                None
                if mode is AvailabilityMode.RECONSTRUCTED
                else prediction.prediction_cutoff
            ),
            availability_mode=mode.value,
            cutoff_source=run.cutoff_source,
            radiant_team_id=target.radiant_team_id,
            dire_team_id=target.dire_team_id,
            radiant_probability=prediction.raw_probability,
            prediction_input_hash=prediction.input_hash,
            artifact_hash=artifact.artifact_hash,
            artifact_training_input_hash=artifact.training_input_hash,
            run_id=run.run_id,
            authority_fingerprint=run.authority_fingerprint,
            combined_training_input_hash=run.combined_training_input_hash,
            reconstruction_rule=(
                RECONSTRUCTION_RULE
                if mode is AvailabilityMode.RECONSTRUCTED
                else None
            ),
            radiant_win=run.eventual_radiant_win if include_outcome else None,
        )

    def to_payload(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "match_id": self.match_id,
            "target_started_at": self.target_started_at.isoformat(),
            "prediction_cutoff": self.prediction_cutoff.isoformat(),
            "first_usable_at": (
                None
                if self.first_usable_at is None
                else self.first_usable_at.isoformat()
            ),
            "availability_mode": self.availability_mode,
            "cutoff_source": self.cutoff_source,
            "radiant_team_id": self.radiant_team_id,
            "dire_team_id": self.dire_team_id,
            "radiant_probability": self.radiant_probability,
            "prediction_input_hash": self.prediction_input_hash,
            "artifact_hash": self.artifact_hash,
            "artifact_training_input_hash": self.artifact_training_input_hash,
            "run_id": self.run_id,
            "authority_fingerprint": self.authority_fingerprint,
            "combined_training_input_hash": self.combined_training_input_hash,
            "reconstruction_rule": self.reconstruction_rule,
            "radiant_win": self.radiant_win,
        }
        if include_hash:
            payload["evidence_hash"] = self.evidence_hash
        return payload

    def without_outcome(self) -> TeamRatingResidualEvidence:
        """Return the same verified claim with the target outcome removed."""

        if self.radiant_win is None:
            return self
        return replace(self, radiant_win=None, evidence_hash="")


@dataclass(frozen=True)
class TeamRatingResidualEvidenceCache:
    """Immutable, strictly verified Team Rating claims for one M6 replay."""

    entries: tuple[TeamRatingResidualEvidence, ...]
    cache_hash: str = ""
    _by_match: Mapping[int, TeamRatingResidualEvidence] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        if any(not isinstance(row, TeamRatingResidualEvidence) for row in entries):
            raise ValueError("Team Rating evidence cache entries are invalid")
        # Canonicalize only at the dedicated construction boundary.  A repeated
        # identical claim is harmless; contradictory claims for one map are not.
        ordered = sorted(entries, key=lambda row: (row.match_id, row.run_id))
        unique: list[TeamRatingResidualEvidence] = []
        by_match: dict[int, TeamRatingResidualEvidence] = {}
        for row in ordered:
            existing = by_match.get(row.match_id)
            if existing is not None:
                if existing != row:
                    raise ValueError(
                        f"conflicting Team Rating evidence cache for match {row.match_id}"
                    )
                continue
            if row.radiant_win is None:
                raise ValueError(
                    "Team Rating evidence cache entries require outcomes"
                )
            by_match[row.match_id] = row
            unique.append(row)
        canonical_entries = tuple(unique)
        object.__setattr__(self, "entries", canonical_entries)
        expected_hash = _hash(
            {
                "domain": "team-rating-residual-evidence-cache/v1",
                "entries": [row.to_payload() for row in canonical_entries],
            }
        )
        if not self.cache_hash:
            object.__setattr__(self, "cache_hash", expected_hash)
        elif not hmac.compare_digest(
            _sha256(self.cache_hash, "Team Rating evidence cache hash"),
            expected_hash,
        ):
            raise ValueError("Team Rating evidence cache hash does not recompute")
        object.__setattr__(self, "_by_match", MappingProxyType(by_match))

    def evidence_for_run(
        self,
        run: TeamRatingWalkForwardRun,
        *,
        include_outcome: bool,
    ) -> TeamRatingResidualEvidence:
        """Bind one cached claim to the supplied immutable run identity."""

        if not isinstance(run, TeamRatingWalkForwardRun):
            raise ValueError("Team Rating evidence requires a walk-forward run")
        if not isinstance(include_outcome, bool):
            raise ValueError("include_outcome must be boolean")
        evidence = self._by_match.get(run.artifact.target.match_id)
        if evidence is None:
            raise ValueError(
                "Team Rating evidence cache is missing match "
                f"{run.artifact.target.match_id}"
            )
        _assert_cached_evidence_matches_run(
            run,
            evidence,
            include_outcome=include_outcome,
        )
        if include_outcome:
            return evidence
        return evidence.without_outcome()


def build_team_rating_residual_evidence_cache(
    runs: Iterable[TeamRatingWalkForwardRun],
) -> TeamRatingResidualEvidenceCache:
    """Strictly verify each distinct Team Rating run once and freeze its claim."""

    by_run_id: dict[str, TeamRatingWalkForwardRun] = {}
    for run in runs:
        if not isinstance(run, TeamRatingWalkForwardRun):
            raise ValueError("Team Rating evidence requires walk-forward runs")
        existing = by_run_id.get(run.run_id)
        if existing is not None and existing != run:
            raise ValueError(f"conflicting Team Rating run {run.run_id}")
        by_run_id[run.run_id] = run
    evidence: list[TeamRatingResidualEvidence] = []
    for run in sorted(
        by_run_id.values(),
        key=lambda row: (row.artifact.target.match_id, row.run_id),
    ):
        if run.status == "insufficient_evidence":
            continue
        evidence.append(
            TeamRatingResidualEvidence.from_walk_forward_run(
                run,
                include_outcome=True,
            )
        )
    return TeamRatingResidualEvidenceCache(tuple(evidence))


def _assert_cached_evidence_matches_run(
    run: TeamRatingWalkForwardRun,
    evidence: TeamRatingResidualEvidence,
    *,
    include_outcome: bool,
) -> None:
    """Check cache/run binding without replaying the expensive artifact."""

    artifact = run.artifact
    target = artifact.target
    prediction = artifact.prediction
    expected = {
        "match_id": target.match_id,
        "target_started_at": target.started_at,
        "prediction_cutoff": prediction.prediction_cutoff,
        "cutoff_source": run.cutoff_source,
        "availability_mode": run.availability_mode,
        "radiant_team_id": target.radiant_team_id,
        "dire_team_id": target.dire_team_id,
        "radiant_probability": prediction.raw_probability,
        "prediction_input_hash": prediction.input_hash,
        "artifact_hash": artifact.artifact_hash,
        "artifact_training_input_hash": artifact.training_input_hash,
        "run_id": run.run_id,
        "authority_fingerprint": run.authority_fingerprint,
        "combined_training_input_hash": run.combined_training_input_hash,
    }
    for field_name, expected_value in expected.items():
        if getattr(evidence, field_name) != expected_value:
            raise ValueError(
                "Team Rating evidence cache does not match run "
                f"{run.run_id}: {field_name}"
            )
    if include_outcome and evidence.radiant_win != run.eventual_radiant_win:
        raise ValueError(
            "Team Rating evidence cache does not match run "
            f"{run.run_id}: radiant_win"
        )
    mode = AvailabilityMode(run.availability_mode)
    if mode is AvailabilityMode.RECONSTRUCTED:
        if evidence.first_usable_at is not None:
            raise ValueError(
                "reconstructed Team Rating evidence cache timing is invalid"
            )
        if evidence.reconstruction_rule != RECONSTRUCTION_RULE:
            raise ValueError(
                "Team Rating evidence cache reconstruction rule is invalid"
            )
    elif (
        evidence.first_usable_at is None
        or evidence.first_usable_at > evidence.prediction_cutoff
        or evidence.reconstruction_rule is not None
    ):
        raise ValueError("prospective Team Rating evidence cache timing is invalid")


def team_rating_residual_evidence(
    run: TeamRatingWalkForwardRun,
    *,
    include_outcome: bool,
    evidence_cache: TeamRatingResidualEvidenceCache | None = None,
) -> TeamRatingResidualEvidence:
    """Resolve one claim through a verified cache or the strict public path."""

    if evidence_cache is None:
        return TeamRatingResidualEvidence.from_walk_forward_run(
            run,
            include_outcome=include_outcome,
        )
    if not isinstance(evidence_cache, TeamRatingResidualEvidenceCache):
        raise ValueError("Team Rating evidence cache has an unsupported type")
    return evidence_cache.evidence_for_run(
        run,
        include_outcome=include_outcome,
    )


@dataclass(frozen=True)
class ResidualFeatureEstimate:
    name: str
    value: float | None
    support: int
    effective_support: float
    coverage: float
    standard_error: float | None
    missing_reason: str | None
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty(self.name, "residual feature name")
        if self.value is not None:
            object.__setattr__(self, "value", _finite(self.value, self.name))
        _nonnegative_int(self.support, f"{self.name} support")
        effective = _finite(
            self.effective_support, f"{self.name} effective_support"
        )
        if effective < 0.0:
            raise ValueError("effective_support cannot be negative")
        object.__setattr__(self, "effective_support", effective)
        object.__setattr__(self, "coverage", _probability(self.coverage, "coverage"))
        if self.standard_error is not None:
            standard_error = _finite(self.standard_error, "standard_error")
            if standard_error < 0.0:
                raise ValueError("standard_error cannot be negative")
            object.__setattr__(self, "standard_error", standard_error)
        if self.value is None and not self.missing_reason:
            raise ValueError("an unavailable residual feature needs a missing reason")
        if self.missing_reason is not None:
            _nonempty(self.missing_reason, "missing_reason")
        if tuple(sorted(set(self.evidence_ids))) != self.evidence_ids:
            raise ValueError("residual evidence IDs must be sorted and unique")


@dataclass(frozen=True)
class DraftResidualSnapshot:
    match_id: int
    prediction_cutoff: datetime
    availability_mode: str
    feature_version: str
    feature_schema: tuple[str, ...]
    feature_schema_hash: str
    team_rating_input_hash: str
    authority_fingerprint: str
    pure_features: tuple[ResidualFeatureEstimate, ...]
    context_features: tuple[ResidualFeatureEstimate, ...]
    support: int
    coverage: float
    input_hash: str

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "Draft residual match_id")
        object.__setattr__(
            self,
            "prediction_cutoff",
            _utc(self.prediction_cutoff, "Draft residual prediction_cutoff"),
        )
        AvailabilityMode(self.availability_mode)
        if self.feature_version != DRAFT_RESIDUAL_FEATURE_VERSION:
            raise ValueError("unsupported Draft residual feature version")
        if self.feature_schema != DRAFT_RESIDUAL_PURE_SCHEMA:
            raise ValueError("Draft residual feature schema does not match")
        if self.feature_schema_hash != DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH:
            raise ValueError("Draft residual feature schema hash does not match")
        for name in (
            "team_rating_input_hash",
            "authority_fingerprint",
            "input_hash",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if tuple(row.name for row in self.pure_features) != self.feature_schema:
            raise ValueError("Draft residual estimates do not match the schema")
        if self.context_features:
            raise ValueError("Draft residual v1 has no context features")
        _nonnegative_int(self.support, "Draft residual support")
        object.__setattr__(
            self,
            "coverage",
            _probability(self.coverage, "Draft residual coverage"),
        )

    def feature(self, name: str) -> ResidualFeatureEstimate:
        for row in self.pure_features:
            if row.name == name:
                return row
        raise KeyError(name)


@dataclass(frozen=True)
class _Observation:
    match_id: int
    evidence_id: str
    residual: float


class _Component:
    def __init__(self, observations: Sequence[_Observation] = ()) -> None:
        self.observations = tuple(observations)

    def count(self, excluded_match_id: int | None = None) -> int:
        return sum(
            row.match_id != excluded_match_id for row in self.observations
        )

    def effect(self, excluded_match_id: int | None = None) -> float | None:
        selected = tuple(
            row for row in self.observations if row.match_id != excluded_match_id
        )
        if not selected:
            return None
        return math.fsum(row.residual for row in selected) / (
            len(selected) + SHRINKAGE_STRENGTH
        )

    def match_ids(self) -> set[int]:
        return {row.match_id for row in self.observations}

    def evidence_ids(self) -> set[str]:
        return {row.evidence_id for row in self.observations}


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def _jackknife(
    match_ids: Sequence[int],
    estimator: Any,
) -> float | None:
    if len(match_ids) < 2:
        return None
    estimates = tuple(estimator(match_id) for match_id in match_ids)
    if any(value is None for value in estimates):
        return None
    numeric = tuple(float(value) for value in estimates)
    center = _mean(numeric)
    variance = (len(numeric) - 1.0) / len(numeric) * math.fsum(
        (value - center) ** 2 for value in numeric
    )
    return math.sqrt(max(0.0, variance))


def _estimate(
    name: str,
    *,
    value: float | None,
    components: Sequence[_Component],
    expected_components: int,
    estimator: Any,
    missing_reason: str | None,
) -> ResidualFeatureEstimate:
    if expected_components < 1:
        raise ValueError("expected_components must be positive")
    match_ids = sorted(
        {match_id for component in components for match_id in component.match_ids()}
    )
    evidence_ids = tuple(
        sorted(
            {
                evidence_id
                for component in components
                for evidence_id in component.evidence_ids()
            }
        )
    )
    coverage = math.fsum(
        min(1.0, component.count() / MIN_FEATURE_SUPPORT)
        for component in components
    ) / expected_components
    reason = missing_reason
    if value is not None and coverage < 1.0 and reason is None:
        reason = "insufficient_historical_support"
    return ResidualFeatureEstimate(
        name=name,
        value=None if value is None else round(value, 12),
        support=len(match_ids),
        effective_support=float(len(match_ids)),
        coverage=round(max(0.0, min(1.0, coverage)), 6),
        standard_error=(
            None
            if value is None
            else (
                None
                if (error := _jackknife(match_ids, estimator)) is None
                else round(error, 12)
            )
        ),
        missing_reason=reason,
        evidence_ids=evidence_ids,
    )


def _side_difference(
    name: str,
    radiant_keys: Sequence[object],
    dire_keys: Sequence[object],
    index: Mapping[object, _Component],
    *,
    expected_components: int | None = None,
) -> ResidualFeatureEstimate:
    radiant_components = tuple(index.get(key, _Component()) for key in radiant_keys)
    dire_components = tuple(index.get(key, _Component()) for key in dire_keys)

    def calculate(excluded_match_id: int | None = None) -> float | None:
        radiant = tuple(
            value
            for component in radiant_components
            if (value := component.effect(excluded_match_id)) is not None
        )
        dire = tuple(
            value
            for component in dire_components
            if (value := component.effect(excluded_match_id)) is not None
        )
        if not radiant or not dire:
            return None
        return _mean(radiant) - _mean(dire)

    value = calculate()
    return _estimate(
        name,
        value=value,
        components=(*radiant_components, *dire_components),
        expected_components=(
            len(radiant_keys) + len(dire_keys)
            if expected_components is None
            else expected_components
        ),
        estimator=calculate,
        missing_reason=(
            "both_team_histories_required" if value is None else None
        ),
    )


def _counter_feature(
    target_pairs: Sequence[tuple[int, int]],
    index: Mapping[object, _Component],
) -> ResidualFeatureEstimate:
    components = tuple(index.get(pair, _Component()) for pair in target_pairs)

    def calculate(excluded_match_id: int | None = None) -> float | None:
        values = tuple(
            value
            for component in components
            if (value := component.effect(excluded_match_id)) is not None
        )
        return None if not values else _mean(values)

    value = calculate()
    return _estimate(
        "counter_residual_edge",
        value=value,
        components=components,
        expected_components=len(target_pairs),
        estimator=calculate,
        missing_reason="counter_history_unavailable" if value is None else None,
    )


def _players(team: object) -> tuple[Mapping[str, Any], ...]:
    row = _exact_object(team, ("team_id", "players"), "draft team")
    players = row["players"]
    if not isinstance(players, list):
        raise ValueError("draft team players must be an array")
    return tuple(
        _exact_object(player, ("player_id", "hero_id", "expected_role"), "player")
        for player in players
    )


def _hero_ids(team: object) -> tuple[int, ...]:
    return tuple(int(player["hero_id"]) for player in _players(team))


def _target_roles(team: object) -> tuple[tuple[int, int], ...]:
    roles: list[tuple[int, int]] = []
    for player in _players(team):
        role = player["expected_role"]
        if not isinstance(role, Mapping):
            continue
        position = role.get("position")
        confidence = role.get("confidence")
        if (
            isinstance(position, int)
            and isinstance(confidence, (int, float))
            and float(confidence) >= ROLE_CONFIDENCE_MIN
        ):
            roles.append((int(player["hero_id"]), position))
    return tuple(roles)


def _observed_roles(row: Mapping[str, Any], side: str) -> tuple[tuple[int, int], ...]:
    raw = row[f"{side}_hero_evidence"]
    if not isinstance(raw, list):
        raise ValueError("historical hero evidence must be an array")
    roles = []
    for value in raw:
        if not isinstance(value, Mapping):
            raise ValueError("historical hero evidence must contain objects")
        position = value.get("observed_position")
        confidence = value.get("observed_position_confidence")
        if (
            isinstance(position, int)
            and isinstance(confidence, (int, float))
            and float(confidence) >= ROLE_CONFIDENCE_MIN
        ):
            roles.append((int(value["hero_id"]), position))
    return tuple(roles)


def _append(
    index: dict[object, list[_Observation]],
    key: object,
    observation: _Observation,
) -> None:
    index.setdefault(key, []).append(observation)


def _residual_features(
    draft_authority: Mapping[str, Any],
    evidence_by_match: Mapping[int, TeamRatingResidualEvidence],
) -> tuple[ResidualFeatureEstimate, ...]:
    target = _exact_object(
        draft_authority["target"],
        (
            "match_id",
            "prediction_cutoff",
            "event_id",
            "patch",
            "series_id",
            "map_number",
            "radiant",
            "dire",
            "availability_mode",
            "radiant_style",
            "dire_style",
        ),
        "draft target",
    )
    history = draft_authority["eligible_history"]
    if not isinstance(history, list):
        raise ValueError("draft eligible_history must be an array")
    hero: dict[object, list[_Observation]] = {}
    role: dict[object, list[_Observation]] = {}
    synergy: dict[object, list[_Observation]] = {}
    counter: dict[object, list[_Observation]] = {}
    scaling: dict[object, list[_Observation]] = {}

    for raw in history:
        if not isinstance(raw, Mapping):
            raise ValueError("draft eligible history must contain objects")
        match_id = int(raw["match_id"])
        baseline = evidence_by_match.get(match_id)
        if baseline is None:
            continue
        outcome = raw["radiant_win"]
        if not isinstance(outcome, bool) or baseline.radiant_win is not outcome:
            raise ValueError("Draft and Team Rating outcomes disagree")
        radiant_team = _exact_object(raw["radiant"], ("team_id", "players"), "radiant")
        dire_team = _exact_object(raw["dire"], ("team_id", "players"), "dire")
        if (
            int(radiant_team["team_id"]) != baseline.radiant_team_id
            or int(dire_team["team_id"]) != baseline.dire_team_id
        ):
            raise ValueError("Draft and Team Rating team identities disagree")
        completed_at = _parse_utc(raw["completed_at"], "completed_at")
        duration = _positive_int(raw["duration_seconds"], "duration_seconds")
        draft_started_at = completed_at - timedelta(seconds=duration)
        if baseline.target_started_at != draft_started_at:
            raise ValueError("Team Rating and Draft historical starts disagree")
        evidence_id = _nonempty(raw["evidence_id"], "evidence_id")
        residual = float(outcome) - baseline.radiant_probability
        radiant_observation = _Observation(match_id, evidence_id, residual)
        dire_observation = _Observation(match_id, evidence_id, -residual)
        radiant_heroes = _hero_ids(raw["radiant"])
        dire_heroes = _hero_ids(raw["dire"])
        for hero_id in radiant_heroes:
            _append(hero, hero_id, radiant_observation)
            if duration >= 40 * 60:
                _append(scaling, hero_id, radiant_observation)
        for hero_id in dire_heroes:
            _append(hero, hero_id, dire_observation)
            if duration >= 40 * 60:
                _append(scaling, hero_id, dire_observation)
        for hero_role in _observed_roles(raw, "radiant"):
            _append(role, hero_role, radiant_observation)
        for hero_role in _observed_roles(raw, "dire"):
            _append(role, hero_role, dire_observation)
        for pair in combinations(sorted(radiant_heroes), 2):
            _append(synergy, pair, radiant_observation)
        for pair in combinations(sorted(dire_heroes), 2):
            _append(synergy, pair, dire_observation)
        for radiant_hero in radiant_heroes:
            for dire_hero in dire_heroes:
                _append(counter, (radiant_hero, dire_hero), radiant_observation)
                _append(counter, (dire_hero, radiant_hero), dire_observation)

    hero_index = {key: _Component(value) for key, value in hero.items()}
    role_index = {key: _Component(value) for key, value in role.items()}
    synergy_index = {key: _Component(value) for key, value in synergy.items()}
    counter_index = {key: _Component(value) for key, value in counter.items()}
    scaling_index = {key: _Component(value) for key, value in scaling.items()}
    radiant_heroes = _hero_ids(target["radiant"])
    dire_heroes = _hero_ids(target["dire"])
    radiant_roles = _target_roles(target["radiant"])
    dire_roles = _target_roles(target["dire"])
    return (
        _side_difference(
            "hero_residual_diff", radiant_heroes, dire_heroes, hero_index
        ),
        _side_difference(
            "role_residual_diff",
            radiant_roles,
            dire_roles,
            role_index,
            expected_components=10,
        ),
        _side_difference(
            "synergy_residual_diff",
            tuple(combinations(sorted(radiant_heroes), 2)),
            tuple(combinations(sorted(dire_heroes), 2)),
            synergy_index,
        ),
        _counter_feature(
            tuple(
                (radiant_hero, dire_hero)
                for radiant_hero in radiant_heroes
                for dire_hero in dire_heroes
            ),
            counter_index,
        ),
        _side_difference(
            "scaling_40m_residual_diff",
            radiant_heroes,
            dire_heroes,
            scaling_index,
        ),
    )


def _proxy_features(draft_snapshot: Any) -> tuple[ResidualFeatureEstimate, ...]:
    # Frozen Draft v3 exposes aggregate support but no map-level variance claim.
    rows = []
    for name in _PROXY_NAMES:
        source = draft_snapshot.feature(name)
        rows.append(
            ResidualFeatureEstimate(
                name=name,
                value=source.value,
                support=source.support,
                effective_support=float(source.support),
                coverage=source.coverage,
                standard_error=None,
                missing_reason=source.missing_reason,
                evidence_ids=tuple(sorted(set(source.evidence_ids))),
            )
        )
    return tuple(rows)


_EVIDENCE_FIELDS = (
    "match_id",
    "target_started_at",
    "prediction_cutoff",
    "first_usable_at",
    "availability_mode",
    "cutoff_source",
    "radiant_team_id",
    "dire_team_id",
    "radiant_probability",
    "prediction_input_hash",
    "artifact_hash",
    "artifact_training_input_hash",
    "run_id",
    "authority_fingerprint",
    "combined_training_input_hash",
    "reconstruction_rule",
    "radiant_win",
    "evidence_hash",
)


def _evidence_from_payload(value: object) -> TeamRatingResidualEvidence:
    row = _exact_object(value, _EVIDENCE_FIELDS, "Team Rating residual evidence")
    outcome = row["radiant_win"]
    if outcome is not None and not isinstance(outcome, bool):
        raise ValueError("Team Rating residual outcome must be boolean or absent")
    first_usable = row["first_usable_at"]
    return TeamRatingResidualEvidence(
        match_id=row["match_id"],
        target_started_at=_parse_utc(
            row["target_started_at"],
            "target_started_at",
        ),
        prediction_cutoff=_parse_utc(row["prediction_cutoff"], "prediction_cutoff"),
        first_usable_at=(
            None
            if first_usable is None
            else _parse_utc(first_usable, "first_usable_at")
        ),
        availability_mode=row["availability_mode"],
        cutoff_source=row["cutoff_source"],
        radiant_team_id=row["radiant_team_id"],
        dire_team_id=row["dire_team_id"],
        radiant_probability=row["radiant_probability"],
        prediction_input_hash=row["prediction_input_hash"],
        artifact_hash=row["artifact_hash"],
        artifact_training_input_hash=row["artifact_training_input_hash"],
        run_id=row["run_id"],
        authority_fingerprint=row["authority_fingerprint"],
        combined_training_input_hash=row["combined_training_input_hash"],
        reconstruction_rule=row["reconstruction_rule"],
        radiant_win=outcome,
        evidence_hash=row["evidence_hash"],
    )


_AUTHORITY_FIELDS = (
    "schema",
    "feature_version",
    "feature_schema_hash",
    "draft_feature_version",
    "draft_feature_schema_hash",
    "draft_feature_input_hash",
    "draft_authority",
    "target_team_rating",
    "eligible_team_rating_history",
    "missing_team_rating_match_ids",
)


def _team_rating_input_hash(
    target: TeamRatingResidualEvidence,
    history: Sequence[TeamRatingResidualEvidence],
) -> str:
    return _hash(
        {
            "domain": "draft-residual-team-rating-input/v1",
            "target": target.to_payload(),
            "ordered_history": [row.to_payload() for row in history],
        }
    )


def _snapshot_from_authority(authority_payload: Mapping[str, Any]) -> DraftResidualSnapshot:
    authority = _exact_object(
        authority_payload,
        _AUTHORITY_FIELDS,
        "Draft residual authority",
    )
    if authority["schema"] != DRAFT_RESIDUAL_AUTHORITY_SCHEMA:
        raise ValueError("unsupported Draft residual authority schema")
    if authority["feature_version"] != DRAFT_RESIDUAL_FEATURE_VERSION:
        raise ValueError("unsupported Draft residual feature version")
    if authority["feature_schema_hash"] != DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH:
        raise ValueError("Draft residual schema hash does not match")
    if (
        authority["draft_feature_version"] != DRAFT_V3_FEATURE_VERSION
        or authority["draft_feature_schema_hash"] != DRAFT_V3_SCHEMA_HASH
    ):
        raise ValueError("frozen Draft v3 identity does not match")
    draft_authority = authority["draft_authority"]
    if not isinstance(draft_authority, Mapping):
        raise ValueError("Draft v3 authority must be an object")
    draft_snapshot = replay_draft_feature_snapshot(draft_authority)
    if not hmac.compare_digest(
        _sha256(authority["draft_feature_input_hash"], "draft_feature_input_hash"),
        draft_snapshot.input_hash,
    ):
        raise ValueError("Draft v3 input hash does not recompute")
    target_rating = _evidence_from_payload(authority["target_team_rating"])
    if target_rating.radiant_win is not None:
        raise ValueError("target Team Rating evidence must not contain outcome")
    history_raw = authority["eligible_team_rating_history"]
    if not isinstance(history_raw, list):
        raise ValueError("eligible Team Rating history must be an array")
    history = tuple(_evidence_from_payload(row) for row in history_raw)
    if any(row.radiant_win is None for row in history):
        raise ValueError("historical Team Rating evidence requires outcome")
    target = draft_authority.get("target")
    eligible_draft = draft_authority.get("eligible_history")
    if not isinstance(target, Mapping) or not isinstance(eligible_draft, list):
        raise ValueError("Draft v3 authority is incomplete")
    mode = AvailabilityMode(target["availability_mode"])
    if target_rating.availability_mode != mode.value:
        raise ValueError("target Team Rating evidence mode does not match Draft")
    if target_rating.match_id != int(target["match_id"]):
        raise ValueError("target Team Rating and Draft match IDs disagree")
    target_radiant = _exact_object(target["radiant"], ("team_id", "players"), "target radiant")
    target_dire = _exact_object(target["dire"], ("team_id", "players"), "target dire")
    if (
        target_rating.radiant_team_id != int(target_radiant["team_id"])
        or target_rating.dire_team_id != int(target_dire["team_id"])
    ):
        raise ValueError("target Team Rating and Draft team identities disagree")
    prediction_cutoff = _parse_utc(target["prediction_cutoff"], "prediction_cutoff")
    if mode is AvailabilityMode.RECONSTRUCTED:
        if target_rating.target_started_at != prediction_cutoff:
            raise ValueError("target Team Rating and Draft starts disagree")
    elif target_rating.prediction_cutoff > prediction_cutoff:
        raise ValueError("target Team Rating prediction follows Draft cutoff")
    if (
        mode is AvailabilityMode.PROSPECTIVE
        and (
            target_rating.first_usable_at is None
            or target_rating.first_usable_at > prediction_cutoff
        )
    ):
        raise ValueError("target Team Rating evidence was unavailable at cutoff")

    draft_match_ids = tuple(int(row["match_id"]) for row in eligible_draft)
    history_match_ids = tuple(row.match_id for row in history)
    if len(set(history_match_ids)) != len(history_match_ids):
        raise ValueError("duplicate Team Rating history match")
    if any(match_id not in draft_match_ids for match_id in history_match_ids):
        raise ValueError("Team Rating history is outside Draft eligibility")
    expected_history_order = tuple(
        match_id for match_id in draft_match_ids if match_id in set(history_match_ids)
    )
    if history_match_ids != expected_history_order:
        raise ValueError("Team Rating history is not in canonical Draft order")
    missing = authority["missing_team_rating_match_ids"]
    if not isinstance(missing, list) or any(
        isinstance(value, bool) or not isinstance(value, int) for value in missing
    ):
        raise ValueError("missing Team Rating match IDs must be integers")
    expected_missing = [
        match_id for match_id in draft_match_ids if match_id not in set(history_match_ids)
    ]
    if missing != expected_missing:
        raise ValueError("missing Team Rating history manifest does not recompute")
    if any(row.availability_mode != mode.value for row in history):
        raise ValueError("Team Rating history mode does not match Draft")
    if mode is AvailabilityMode.PROSPECTIVE and any(
        row.first_usable_at is None or row.first_usable_at > prediction_cutoff
        for row in history
    ):
        raise ValueError("Team Rating history was unavailable at Draft cutoff")
    history_by_match = {row.match_id: row for row in history}
    residual = _residual_features(draft_authority, history_by_match)
    pure = (*residual, *_proxy_features(draft_snapshot))
    if tuple(row.name for row in pure) != DRAFT_RESIDUAL_PURE_SCHEMA:
        raise AssertionError("Draft residual implementation does not match schema")
    team_rating_hash = _team_rating_input_hash(target_rating, history)
    authority_fingerprint = _hash(
        {"domain": "draft-residual-authority-fingerprint/v1", "authority": authority}
    )
    input_hash = _hash(
        {"domain": "draft-residual-input/v1", "authority": authority}
    )
    coverage = math.fsum(row.coverage for row in pure) / len(pure)
    return DraftResidualSnapshot(
        match_id=int(target["match_id"]),
        prediction_cutoff=prediction_cutoff,
        availability_mode=mode.value,
        feature_version=DRAFT_RESIDUAL_FEATURE_VERSION,
        feature_schema=DRAFT_RESIDUAL_PURE_SCHEMA,
        feature_schema_hash=DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
        team_rating_input_hash=team_rating_hash,
        authority_fingerprint=authority_fingerprint,
        pure_features=tuple(pure),
        context_features=(),
        support=len(history),
        coverage=round(coverage, 6),
        input_hash=input_hash,
    )


def _draft_residual_authority_payload(
    target: DraftTarget,
    history: Iterable[DraftMapEvidence],
    *,
    target_team_rating: TeamRatingWalkForwardRun,
    team_rating_history: Iterable[TeamRatingWalkForwardRun],
    team_rating_evidence_cache: TeamRatingResidualEvidenceCache | None = None,
) -> dict[str, Any]:
    draft_snapshot, draft_authority = build_draft_feature_snapshot_with_authority(
        target,
        history,
    )
    target_evidence = team_rating_residual_evidence(
        target_team_rating,
        include_outcome=False,
        evidence_cache=team_rating_evidence_cache,
    )
    eligible_raw = draft_authority["eligible_history"]
    if not isinstance(eligible_raw, list):
        raise AssertionError("Draft v3 authority history is not an array")
    eligible_ids = tuple(int(row["match_id"]) for row in eligible_raw)
    by_match = _verified_history_evidence(
        eligible_ids,
        team_rating_history,
        evidence_cache=team_rating_evidence_cache,
    )
    ordered = tuple(by_match[match_id] for match_id in eligible_ids if match_id in by_match)
    missing = [match_id for match_id in eligible_ids if match_id not in by_match]
    return {
        "schema": DRAFT_RESIDUAL_AUTHORITY_SCHEMA,
        "feature_version": DRAFT_RESIDUAL_FEATURE_VERSION,
        "feature_schema_hash": DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
        "draft_feature_version": DRAFT_V3_FEATURE_VERSION,
        "draft_feature_schema_hash": DRAFT_V3_SCHEMA_HASH,
        "draft_feature_input_hash": draft_snapshot.input_hash,
        "draft_authority": draft_authority,
        "target_team_rating": target_evidence.to_payload(),
        "eligible_team_rating_history": [row.to_payload() for row in ordered],
        "missing_team_rating_match_ids": missing,
    }


def _verified_history_evidence(
    eligible_match_ids: Sequence[int],
    runs: Iterable[TeamRatingWalkForwardRun],
    *,
    evidence_cache: TeamRatingResidualEvidenceCache | None = None,
) -> dict[int, TeamRatingResidualEvidence]:
    eligible = set(eligible_match_ids)
    by_match: dict[int, TeamRatingResidualEvidence] = {}
    for run in runs:
        if not isinstance(run, TeamRatingWalkForwardRun):
            raise ValueError("Team Rating history requires walk-forward runs")
        match_id = run.artifact.target.match_id
        if match_id not in eligible:
            continue
        if run.status == "insufficient_evidence":
            continue
        evidence = team_rating_residual_evidence(
            run,
            include_outcome=True,
            evidence_cache=evidence_cache,
        )
        existing = by_match.get(match_id)
        if existing is not None and existing != evidence:
            raise ValueError(f"conflicting Team Rating evidence for match {match_id}")
        by_match[match_id] = evidence
    return by_match


def _verify_team_rating_authority_claims(
    authority_payload: Mapping[str, Any],
    *,
    target_team_rating: TeamRatingWalkForwardRun,
    team_rating_history: Iterable[TeamRatingWalkForwardRun],
    team_rating_evidence_cache: TeamRatingResidualEvidenceCache | None = None,
) -> None:
    authority = _exact_object(
        authority_payload,
        _AUTHORITY_FIELDS,
        "Draft residual authority",
    )
    draft_authority = authority["draft_authority"]
    if not isinstance(draft_authority, Mapping):
        raise ValueError("Draft v3 authority must be an object")
    eligible_raw = draft_authority.get("eligible_history")
    if not isinstance(eligible_raw, list):
        raise ValueError("Draft v3 eligible history must be an array")
    eligible_ids = tuple(int(row["match_id"]) for row in eligible_raw)
    target_evidence = team_rating_residual_evidence(
        target_team_rating,
        include_outcome=False,
        evidence_cache=team_rating_evidence_cache,
    )
    by_match = _verified_history_evidence(
        eligible_ids,
        team_rating_history,
        evidence_cache=team_rating_evidence_cache,
    )
    ordered = tuple(by_match[match_id] for match_id in eligible_ids if match_id in by_match)
    expected_history = [row.to_payload() for row in ordered]
    expected_missing = [match_id for match_id in eligible_ids if match_id not in by_match]
    if authority["target_team_rating"] != target_evidence.to_payload():
        raise ValueError("target Team Rating authority claim does not replay")
    if authority["eligible_team_rating_history"] != expected_history:
        raise ValueError("historical Team Rating authority claims do not replay")
    if authority["missing_team_rating_match_ids"] != expected_missing:
        raise ValueError("missing Team Rating authority claims do not replay")


def draft_residual_authority_payload(
    target: DraftTarget,
    history: Iterable[DraftMapEvidence],
    *,
    target_team_rating: TeamRatingWalkForwardRun,
    team_rating_history: Iterable[TeamRatingWalkForwardRun],
    team_rating_evidence_cache: TeamRatingResidualEvidenceCache | None = None,
) -> dict[str, Any]:
    authority = _draft_residual_authority_payload(
        target,
        history,
        target_team_rating=target_team_rating,
        team_rating_history=team_rating_history,
        team_rating_evidence_cache=team_rating_evidence_cache,
    )
    _snapshot_from_authority(authority)
    return authority


def build_draft_residual_snapshot_with_authority(
    target: DraftTarget,
    history: Iterable[DraftMapEvidence],
    *,
    target_team_rating: TeamRatingWalkForwardRun,
    team_rating_history: Iterable[TeamRatingWalkForwardRun],
    team_rating_evidence_cache: TeamRatingResidualEvidenceCache | None = None,
) -> tuple[DraftResidualSnapshot, dict[str, Any]]:
    authority = _draft_residual_authority_payload(
        target,
        history,
        target_team_rating=target_team_rating,
        team_rating_history=team_rating_history,
        team_rating_evidence_cache=team_rating_evidence_cache,
    )
    return _snapshot_from_authority(authority), authority


def build_draft_residual_snapshot(
    target: DraftTarget,
    history: Iterable[DraftMapEvidence],
    *,
    target_team_rating: TeamRatingWalkForwardRun,
    team_rating_history: Iterable[TeamRatingWalkForwardRun],
    team_rating_evidence_cache: TeamRatingResidualEvidenceCache | None = None,
) -> DraftResidualSnapshot:
    snapshot, _authority = build_draft_residual_snapshot_with_authority(
        target,
        history,
        target_team_rating=target_team_rating,
        team_rating_history=team_rating_history,
        team_rating_evidence_cache=team_rating_evidence_cache,
    )
    return snapshot


def replay_draft_residual_snapshot(
    authority_payload: Mapping[str, Any],
    *,
    target_team_rating: TeamRatingWalkForwardRun,
    team_rating_history: Iterable[TeamRatingWalkForwardRun],
    team_rating_evidence_cache: TeamRatingResidualEvidenceCache | None = None,
) -> DraftResidualSnapshot:
    """Replay Draft authority and reverify every bounded M2 claim externally."""

    _verify_team_rating_authority_claims(
        authority_payload,
        target_team_rating=target_team_rating,
        team_rating_history=team_rating_history,
        team_rating_evidence_cache=team_rating_evidence_cache,
    )
    return _snapshot_from_authority(authority_payload)


def project_draft_residual_features(
    snapshot: DraftResidualSnapshot,
) -> dict[str, float | None]:
    if not isinstance(snapshot, DraftResidualSnapshot):
        raise ValueError("snapshot must be a DraftResidualSnapshot")
    projected: dict[str, float | None] = {}
    for row in snapshot.pure_features:
        projected[row.name] = row.value
        projected[f"{row.name}__log1p_support"] = math.log1p(
            row.effective_support
        )
        projected[f"{row.name}__coverage"] = row.coverage
        projected[f"{row.name}__missing"] = 1.0 if row.value is None else 0.0
    if tuple(projected) != DRAFT_RESIDUAL_MODEL_SCHEMA:
        raise AssertionError("Draft residual projection does not match model schema")
    return projected


__all__ = [
    "DRAFT_RESIDUAL_AUTHORITY_SCHEMA",
    "DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH",
    "DRAFT_RESIDUAL_FEATURE_VERSION",
    "DRAFT_RESIDUAL_MODEL_SCHEMA",
    "DRAFT_RESIDUAL_MODEL_SCHEMA_HASH",
    "DRAFT_RESIDUAL_PURE_SCHEMA",
    "RECONSTRUCTION_RULE",
    "SHRINKAGE_STRENGTH",
    "DraftResidualSnapshot",
    "ResidualFeatureEstimate",
    "TeamRatingResidualEvidence",
    "TeamRatingResidualEvidenceCache",
    "build_draft_residual_snapshot",
    "build_draft_residual_snapshot_with_authority",
    "build_team_rating_residual_evidence_cache",
    "draft_residual_authority_payload",
    "project_draft_residual_features",
    "replay_draft_residual_snapshot",
    "team_rating_residual_evidence",
]
