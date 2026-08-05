"""Canonical M5 feature snapshots for offset prematch models."""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from live_betting.rosh_parity_storage import RoshRunMatchLink, StoredRoshRun

from .draft_features import AvailabilityMode
from .draft_residual_features import (
    DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
    DRAFT_RESIDUAL_FEATURE_VERSION,
    DRAFT_RESIDUAL_MODEL_SCHEMA,
    DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
    DraftResidualSnapshot,
    TeamRatingResidualEvidenceCache,
    project_draft_residual_features,
    replay_draft_residual_snapshot,
    team_rating_residual_evidence,
)
from .raw_archive import canonical_json_bytes
from .rosh_features import (
    ROSH_AUTHORITY_SCHEMA,
    ROSH_FEATURE_VERSION,
    ROSH_MODEL_SCHEMA,
    ROSH_MODEL_SCHEMA_HASH,
    ROSH_UNAVAILABLE_AUTHORITY_SCHEMA,
    RoshFeatureSnapshot,
    project_rosh_features,
    replay_rosh_feature_snapshot,
)
from .team_rating_backtest import TeamRatingWalkForwardRun


UTC = timezone.utc
PREMATCH_FEATURE_VERSION = "prematch-features-v1"
PREMATCH_MODEL_KINDS = (
    "team_only",
    "team_plus_draft",
    "team_plus_rosh",
    "team_plus_draft_rosh",
)

TEAM_ONLY_SCHEMA: tuple[str, ...] = ()
TEAM_PLUS_DRAFT_SCHEMA = DRAFT_RESIDUAL_MODEL_SCHEMA
TEAM_PLUS_ROSH_SCHEMA = ROSH_MODEL_SCHEMA
TEAM_PLUS_DRAFT_ROSH_SCHEMA = DRAFT_RESIDUAL_MODEL_SCHEMA + ROSH_MODEL_SCHEMA

_SCHEMAS = {
    "team_only": TEAM_ONLY_SCHEMA,
    "team_plus_draft": TEAM_PLUS_DRAFT_SCHEMA,
    "team_plus_rosh": TEAM_PLUS_ROSH_SCHEMA,
    "team_plus_draft_rosh": TEAM_PLUS_DRAFT_ROSH_SCHEMA,
}
PREMATCH_FEATURE_SCHEMAS: Mapping[str, tuple[str, ...]] = MappingProxyType(_SCHEMAS)
PREMATCH_FEATURE_SCHEMA_HASHES: Mapping[str, str] = MappingProxyType(
    {
        kind: hashlib.sha256(
            canonical_json_bytes(
                {
                    "feature_version": PREMATCH_FEATURE_VERSION,
                    "model_kind": kind,
                    "feature_names": list(names),
                }
            )
        ).hexdigest()
        for kind, names in _SCHEMAS.items()
    }
)

FeatureValue = float | None
FeaturePairs = tuple[tuple[str, FeatureValue], ...]


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


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be finite")
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


def _optional_sha256(value: object, field: str) -> str | None:
    return None if value is None else _sha256(value, field)


def _optional_nonempty(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty or null")
    return value


def _normalize_features(
    values: Sequence[tuple[str, FeatureValue]],
    schema: tuple[str, ...],
    field: str,
) -> FeaturePairs:
    rows = tuple(values)
    if tuple(name for name, _value in rows) != schema:
        raise ValueError(f"{field} do not match the fixed schema")
    normalized: list[tuple[str, FeatureValue]] = []
    for name, value in rows:
        if value is None:
            normalized.append((name, None))
            continue
        number = _finite(value, f"{field}.{name}")
        if name.endswith("__missing") and number not in (0.0, 1.0):
            raise ValueError(f"{field}.{name} must be binary")
        normalized.append((name, number))

    projected = dict(normalized)
    for name in schema:
        missing_name = f"{name}__missing"
        if missing_name not in projected:
            continue
        flag = projected[missing_name]
        if flag is None or flag != (1.0 if projected[name] is None else 0.0):
            raise ValueError(f"{field}.{missing_name} disagrees with {name}")
    return tuple(normalized)


def prematch_feature_schema(model_kind: str) -> tuple[str, ...]:
    try:
        return PREMATCH_FEATURE_SCHEMAS[model_kind]
    except (KeyError, TypeError) as error:
        raise ValueError("unsupported prematch model kind") from error


def prematch_feature_schema_hash(model_kind: str) -> str:
    prematch_feature_schema(model_kind)
    return PREMATCH_FEATURE_SCHEMA_HASHES[model_kind]


@dataclass(frozen=True)
class PrematchFeatureSnapshot:
    match_id: int
    prediction_cutoff: datetime
    availability_mode: str
    feature_version: str
    team_base_logit: float
    team_rating_run_id: str
    team_rating_artifact_hash: str
    team_rating_prediction_input_hash: str
    team_rating_combined_training_input_hash: str
    team_rating_support: int
    draft_residual_input_hash: str
    draft_residual_authority_fingerprint: str
    draft_residual_team_rating_input_hash: str
    draft_residual_feature_schema_hash: str
    draft_residual_model_schema_hash: str
    draft_support: int
    draft_coverage: float
    draft_features: FeaturePairs
    rosh_status: str
    rosh_missing_reason: str | None
    rosh_input_hash: str
    rosh_model_schema_hash: str
    rosh_run_id: str | None
    rosh_evidence_hash: str | None
    rosh_formula_version: str | None
    rosh_profile_hash: str | None
    rosh_result_hash: str | None
    rosh_coverage: float
    rosh_features: FeaturePairs
    input_hash: str = ""

    @property
    def support(self) -> int:
        return min(self.team_rating_support, self.draft_support)

    @property
    def coverage(self) -> float:
        draft_weight = len(DRAFT_RESIDUAL_MODEL_SCHEMA) // 4
        rosh_weight = len(ROSH_MODEL_SCHEMA) // 2
        return (
            self.draft_coverage * draft_weight + self.rosh_coverage * rosh_weight
        ) / (draft_weight + rosh_weight)

    @property
    def missing_reason(self) -> str | None:
        if self.rosh_status == "unavailable":
            return f"rosh:{self.rosh_missing_reason}"
        return None

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "prematch match_id")
        object.__setattr__(
            self,
            "prediction_cutoff",
            _utc(self.prediction_cutoff, "prematch prediction_cutoff"),
        )
        mode = AvailabilityMode(self.availability_mode)
        object.__setattr__(self, "availability_mode", mode.value)
        if self.feature_version != PREMATCH_FEATURE_VERSION:
            raise ValueError("unsupported prematch feature version")
        object.__setattr__(
            self,
            "team_base_logit",
            _finite(self.team_base_logit, "team_base_logit"),
        )
        for name in (
            "team_rating_run_id",
            "team_rating_artifact_hash",
            "team_rating_prediction_input_hash",
            "team_rating_combined_training_input_hash",
            "draft_residual_input_hash",
            "draft_residual_authority_fingerprint",
            "draft_residual_team_rating_input_hash",
            "draft_residual_feature_schema_hash",
            "draft_residual_model_schema_hash",
            "rosh_input_hash",
            "rosh_model_schema_hash",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        for name in (
            "rosh_run_id",
            "rosh_evidence_hash",
            "rosh_profile_hash",
            "rosh_result_hash",
        ):
            object.__setattr__(
                self,
                name,
                _optional_sha256(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            "rosh_formula_version",
            _optional_nonempty(self.rosh_formula_version, "rosh_formula_version"),
        )
        if (
            self.draft_residual_feature_schema_hash
            != DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH
        ):
            raise ValueError("Draft residual feature schema hash does not match")
        if self.draft_residual_model_schema_hash != DRAFT_RESIDUAL_MODEL_SCHEMA_HASH:
            raise ValueError("Draft residual model schema hash does not match")
        if self.rosh_model_schema_hash != ROSH_MODEL_SCHEMA_HASH:
            raise ValueError("R.O.S.H. model schema hash does not match")
        _nonnegative_int(self.team_rating_support, "team_rating_support")
        _nonnegative_int(self.draft_support, "draft_support")
        object.__setattr__(
            self,
            "draft_coverage",
            _probability(self.draft_coverage, "draft_coverage"),
        )
        object.__setattr__(
            self,
            "rosh_coverage",
            _probability(self.rosh_coverage, "rosh_coverage"),
        )
        object.__setattr__(
            self,
            "draft_features",
            _normalize_features(
                self.draft_features,
                DRAFT_RESIDUAL_MODEL_SCHEMA,
                "draft_features",
            ),
        )
        object.__setattr__(
            self,
            "rosh_features",
            _normalize_features(
                self.rosh_features,
                ROSH_MODEL_SCHEMA,
                "rosh_features",
            ),
        )
        if self.rosh_status not in {"available", "unavailable"}:
            raise ValueError("unsupported R.O.S.H. status")
        identities = (
            self.rosh_run_id,
            self.rosh_evidence_hash,
            self.rosh_formula_version,
            self.rosh_profile_hash,
            self.rosh_result_hash,
        )
        if self.rosh_status == "available":
            if self.rosh_missing_reason is not None or any(
                value is None for value in identities
            ):
                raise ValueError("available R.O.S.H. identity is incomplete")
            core_signals = (
                "relative_advantage",
                "direction_flip_count",
                "position_min_support",
                "synergy_min_support",
            )
            projected = dict(self.rosh_features)
            if any(projected[name] is None for name in core_signals):
                raise ValueError("available R.O.S.H. core signals are incomplete")
        else:
            _optional_nonempty(self.rosh_missing_reason, "rosh_missing_reason")
            if self.rosh_missing_reason is None or any(
                value is not None for value in identities
            ):
                raise ValueError("unavailable R.O.S.H. identity is invalid")
            if self.rosh_coverage != 0.0:
                raise ValueError("unavailable R.O.S.H. coverage must be zero")
            projected = dict(self.rosh_features)
            for name in ROSH_MODEL_SCHEMA:
                if name == "coverage":
                    expected: float | None = 0.0
                elif name == "coverage__missing":
                    expected = 0.0
                elif name.endswith("__missing"):
                    expected = 1.0
                else:
                    expected = None
                if projected[name] != expected:
                    raise ValueError("unavailable R.O.S.H. projection is not canonical")
        projected = dict(self.rosh_features)
        if (
            projected["coverage"] != self.rosh_coverage
            or projected["coverage__missing"] != 0.0
        ):
            raise ValueError("R.O.S.H. coverage projection does not match")

        expected = _hash(self.to_payload(include_hash=False))
        if not self.input_hash:
            object.__setattr__(self, "input_hash", expected)
        elif not hmac.compare_digest(_sha256(self.input_hash, "input_hash"), expected):
            raise ValueError("prematch feature input hash does not recompute")

    def to_payload(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "feature_version": self.feature_version,
            "match_id": self.match_id,
            "prediction_cutoff": self.prediction_cutoff.isoformat(),
            "availability_mode": self.availability_mode,
            "support": self.support,
            "coverage": self.coverage,
            "missing_reason": self.missing_reason,
            "team_base_logit": self.team_base_logit,
            "team_rating": {
                "run_id": self.team_rating_run_id,
                "artifact_hash": self.team_rating_artifact_hash,
                "prediction_input_hash": self.team_rating_prediction_input_hash,
                "combined_training_input_hash": (
                    self.team_rating_combined_training_input_hash
                ),
                "support": self.team_rating_support,
            },
            "draft_residual": {
                "feature_version": DRAFT_RESIDUAL_FEATURE_VERSION,
                "feature_schema_hash": self.draft_residual_feature_schema_hash,
                "model_schema_hash": self.draft_residual_model_schema_hash,
                "input_hash": self.draft_residual_input_hash,
                "authority_fingerprint": self.draft_residual_authority_fingerprint,
                "team_rating_input_hash": self.draft_residual_team_rating_input_hash,
                "support": self.draft_support,
                "coverage": self.draft_coverage,
                "features": dict(self.draft_features),
            },
            "rosh": {
                "feature_version": ROSH_FEATURE_VERSION,
                "model_schema_hash": self.rosh_model_schema_hash,
                "status": self.rosh_status,
                "missing_reason": self.rosh_missing_reason,
                "input_hash": self.rosh_input_hash,
                "run_id": self.rosh_run_id,
                "evidence_hash": self.rosh_evidence_hash,
                "formula_version": self.rosh_formula_version,
                "profile_hash": self.rosh_profile_hash,
                "result_hash": self.rosh_result_hash,
                "coverage": self.rosh_coverage,
                "features": dict(self.rosh_features),
            },
        }
        if include_hash:
            payload["input_hash"] = self.input_hash
        return payload


def verify_prematch_feature_snapshot(snapshot: PrematchFeatureSnapshot) -> None:
    if not isinstance(snapshot, PrematchFeatureSnapshot):
        raise ValueError("snapshot must be a PrematchFeatureSnapshot")
    expected = _hash(snapshot.to_payload(include_hash=False))
    claimed = _sha256(snapshot.input_hash, "input_hash")
    if not hmac.compare_digest(claimed, expected):
        raise ValueError("prematch feature input hash does not recompute")


def _compose_prematch_feature_snapshot(
    team_rating_run: TeamRatingWalkForwardRun,
    draft_residual_snapshot: DraftResidualSnapshot,
    rosh_snapshot: RoshFeatureSnapshot,
    *,
    team_rating_evidence_cache: TeamRatingResidualEvidenceCache | None = None,
) -> PrematchFeatureSnapshot:
    """Compose snapshots only after their public replay boundaries verified them."""

    if not isinstance(team_rating_run, TeamRatingWalkForwardRun):
        raise ValueError("team_rating_run must be a TeamRatingWalkForwardRun")
    if not isinstance(draft_residual_snapshot, DraftResidualSnapshot):
        raise ValueError("draft_residual_snapshot must be a DraftResidualSnapshot")
    if not isinstance(rosh_snapshot, RoshFeatureSnapshot):
        raise ValueError("rosh_snapshot must be a RoshFeatureSnapshot")

    team = team_rating_residual_evidence(
        team_rating_run,
        include_outcome=False,
        evidence_cache=team_rating_evidence_cache,
    )
    probability = team.radiant_probability
    if not 0.0 < probability < 1.0:
        raise ValueError(
            "Team Rating probability must be strictly between zero and one"
        )
    identities = (
        ("match_id", team.match_id, draft_residual_snapshot.match_id),
        (
            "prediction_cutoff",
            team.prediction_cutoff,
            draft_residual_snapshot.prediction_cutoff,
        ),
        (
            "availability_mode",
            team.availability_mode,
            draft_residual_snapshot.availability_mode,
        ),
        ("match_id", team.match_id, rosh_snapshot.match_id),
        (
            "prediction_cutoff",
            team.prediction_cutoff,
            rosh_snapshot.prediction_cutoff,
        ),
        (
            "availability_mode",
            team.availability_mode,
            rosh_snapshot.availability_mode,
        ),
    )
    for field, expected, actual in identities:
        if expected != actual:
            raise ValueError(f"M2/M3/M4 {field} values disagree")
    if draft_residual_snapshot.feature_version != DRAFT_RESIDUAL_FEATURE_VERSION:
        raise ValueError("unsupported Draft residual feature version")
    if rosh_snapshot.feature_version != ROSH_FEATURE_VERSION:
        raise ValueError("unsupported R.O.S.H. feature version")

    draft_projection = project_draft_residual_features(draft_residual_snapshot)
    rosh_projection = project_rosh_features(rosh_snapshot)
    prediction = team_rating_run.artifact.prediction
    return PrematchFeatureSnapshot(
        match_id=team.match_id,
        prediction_cutoff=team.prediction_cutoff,
        availability_mode=team.availability_mode,
        feature_version=PREMATCH_FEATURE_VERSION,
        team_base_logit=math.log(probability) - math.log1p(-probability),
        team_rating_run_id=team.run_id,
        team_rating_artifact_hash=team.artifact_hash,
        team_rating_prediction_input_hash=team.prediction_input_hash,
        team_rating_combined_training_input_hash=team.combined_training_input_hash,
        team_rating_support=prediction.support,
        draft_residual_input_hash=draft_residual_snapshot.input_hash,
        draft_residual_authority_fingerprint=(
            draft_residual_snapshot.authority_fingerprint
        ),
        draft_residual_team_rating_input_hash=(
            draft_residual_snapshot.team_rating_input_hash
        ),
        draft_residual_feature_schema_hash=(
            draft_residual_snapshot.feature_schema_hash
        ),
        draft_residual_model_schema_hash=DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
        draft_support=draft_residual_snapshot.support,
        draft_coverage=draft_residual_snapshot.coverage,
        draft_features=tuple(draft_projection.items()),
        rosh_status=rosh_snapshot.status,
        rosh_missing_reason=rosh_snapshot.missing_reason,
        rosh_input_hash=rosh_snapshot.input_hash,
        rosh_model_schema_hash=ROSH_MODEL_SCHEMA_HASH,
        rosh_run_id=rosh_snapshot.run_id,
        rosh_evidence_hash=rosh_snapshot.evidence_hash,
        rosh_formula_version=rosh_snapshot.formula_version,
        rosh_profile_hash=rosh_snapshot.profile_hash,
        rosh_result_hash=rosh_snapshot.result_hash,
        rosh_coverage=rosh_snapshot.coverage,
        rosh_features=tuple(rosh_projection.items()),
    )


def _side_draft_hero_identity(
    target: Mapping[str, Any],
    side: str,
) -> tuple[tuple[int, ...], tuple[int, ...] | None]:
    team = target.get(side)
    if not isinstance(team, Mapping):
        raise ValueError(f"verified Draft authority has no {side} team")
    players = team.get("players")
    if not isinstance(players, list) or len(players) != 5:
        raise ValueError(f"verified Draft authority has invalid {side} players")
    hero_ids: list[int] = []
    by_position: dict[int, int] = {}
    positions_complete = True
    for player in players:
        if not isinstance(player, Mapping):
            raise ValueError(f"verified Draft authority has invalid {side} player")
        hero_id = player.get("hero_id")
        if isinstance(hero_id, bool) or not isinstance(hero_id, int) or hero_id <= 0:
            raise ValueError(f"verified Draft authority has invalid {side} hero")
        hero_ids.append(hero_id)
        role = player.get("expected_role")
        if not isinstance(role, Mapping):
            raise ValueError(f"verified Draft authority has invalid {side} role")
        position = role.get("position")
        if position is None:
            positions_complete = False
            continue
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or position not in range(1, 6)
            or position in by_position
        ):
            raise ValueError(f"verified Draft authority has invalid {side} positions")
        by_position[position] = hero_id
    if len(set(hero_ids)) != 5:
        raise ValueError(f"verified Draft authority has duplicate {side} heroes")
    ordered = (
        tuple(by_position[position] for position in range(1, 6))
        if positions_complete and len(by_position) == 5
        else None
    )
    return tuple(sorted(hero_ids)), ordered


def _draft_authority_hero_identity(
    authority_payload: Mapping[str, Any],
) -> tuple[
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...] | None,
    tuple[int, ...] | None,
]:
    draft_authority = authority_payload.get("draft_authority")
    if not isinstance(draft_authority, Mapping):
        raise ValueError("verified Draft residual authority is incomplete")
    target = draft_authority.get("target")
    if not isinstance(target, Mapping):
        raise ValueError("verified Draft target authority is incomplete")
    radiant_set, radiant_positions = _side_draft_hero_identity(target, "radiant")
    dire_set, dire_positions = _side_draft_hero_identity(target, "dire")
    if len(set((*radiant_set, *dire_set))) != 10:
        raise ValueError("verified Draft target heroes are not unique")
    return radiant_set, dire_set, radiant_positions, dire_positions


def _rosh_authority_hero_identity(
    authority_payload: Mapping[str, Any],
) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
    schema = authority_payload.get("schema")
    if schema == ROSH_AUTHORITY_SCHEMA:
        target = authority_payload.get("target")
        if not isinstance(target, Mapping):
            raise ValueError("verified R.O.S.H. target authority is incomplete")
        radiant_raw = target.get("radiant_hero_ids")
        dire_raw = target.get("dire_hero_ids")
    elif schema == ROSH_UNAVAILABLE_AUTHORITY_SCHEMA:
        radiant_raw = authority_payload.get("radiant_hero_ids")
        dire_raw = authority_payload.get("dire_hero_ids")
    else:
        raise ValueError("verified R.O.S.H. authority schema is unsupported")
    if not isinstance(radiant_raw, list) or not isinstance(dire_raw, list):
        raise ValueError("verified R.O.S.H. authority heroes are incomplete")
    for side, values in (("radiant", radiant_raw), ("dire", dire_raw)):
        if len(values) != 5 or any(
            isinstance(hero_id, bool) or not isinstance(hero_id, int) or hero_id <= 0
            for hero_id in values
        ):
            raise ValueError(f"verified R.O.S.H. authority has invalid {side} heroes")
    radiant = tuple(radiant_raw)
    dire = tuple(dire_raw)
    if len(set((*radiant, *dire))) != 10:
        raise ValueError("verified R.O.S.H. authority heroes are not unique")
    return schema, radiant, dire


def _validate_draft_rosh_hero_identity(
    draft_authority_payload: Mapping[str, Any],
    rosh_authority_payload: Mapping[str, Any],
) -> None:
    draft_radiant, draft_dire, radiant_positions, dire_positions = (
        _draft_authority_hero_identity(draft_authority_payload)
    )
    rosh_schema, rosh_radiant, rosh_dire = _rosh_authority_hero_identity(
        rosh_authority_payload
    )
    positions_complete = radiant_positions is not None and dire_positions is not None
    if positions_complete:
        if rosh_schema != ROSH_AUTHORITY_SCHEMA:
            raise ValueError(
                "complete Draft positions require standard R.O.S.H. authority"
            )
        if radiant_positions != rosh_radiant or dire_positions != rosh_dire:
            raise ValueError("Draft and R.O.S.H. position hero identities disagree")
        return
    if rosh_schema != ROSH_UNAVAILABLE_AUTHORITY_SCHEMA:
        raise ValueError(
            "incomplete Draft positions require unavailable R.O.S.H. authority"
        )
    if draft_radiant != tuple(sorted(rosh_radiant)) or draft_dire != tuple(
        sorted(rosh_dire)
    ):
        raise ValueError("Draft and R.O.S.H. hero sets disagree")


def build_prematch_feature_snapshot(
    draft_residual_authority_payload: Mapping[str, Any],
    rosh_authority_payload: Mapping[str, Any],
    *,
    target_team_rating: TeamRatingWalkForwardRun,
    team_rating_history: Iterable[TeamRatingWalkForwardRun],
    rosh_runs: Iterable[StoredRoshRun],
    artifact_root: str | Path,
    match_links: Iterable[RoshRunMatchLink] = (),
    team_rating_evidence_cache: TeamRatingResidualEvidenceCache | None = None,
) -> PrematchFeatureSnapshot:
    """Replay external M3/M4 authority before composing an M5 snapshot."""

    if not isinstance(draft_residual_authority_payload, Mapping):
        raise ValueError("Draft residual authority payload must be an object")
    if not isinstance(rosh_authority_payload, Mapping):
        raise ValueError("R.O.S.H. authority payload must be an object")
    draft_replay_kwargs: dict[str, Any] = {
        "target_team_rating": target_team_rating,
        "team_rating_history": team_rating_history,
    }
    if team_rating_evidence_cache is not None:
        draft_replay_kwargs["team_rating_evidence_cache"] = team_rating_evidence_cache
    draft_snapshot = replay_draft_residual_snapshot(
        draft_residual_authority_payload,
        **draft_replay_kwargs,
    )
    rosh_snapshot = replay_rosh_feature_snapshot(
        rosh_authority_payload,
        runs=rosh_runs,
        artifact_root=artifact_root,
        match_links=match_links,
    )
    _validate_draft_rosh_hero_identity(
        draft_residual_authority_payload,
        rosh_authority_payload,
    )
    return _compose_prematch_feature_snapshot(
        target_team_rating,
        draft_snapshot,
        rosh_snapshot,
        team_rating_evidence_cache=team_rating_evidence_cache,
    )


def project_prematch_features(
    snapshot: PrematchFeatureSnapshot,
    model_kind: str,
) -> dict[str, FeatureValue]:
    verify_prematch_feature_snapshot(snapshot)
    schema = prematch_feature_schema(model_kind)
    available = dict((*snapshot.draft_features, *snapshot.rosh_features))
    return {name: available[name] for name in schema}


__all__ = [
    "PREMATCH_FEATURE_SCHEMA_HASHES",
    "PREMATCH_FEATURE_SCHEMAS",
    "PREMATCH_FEATURE_VERSION",
    "PREMATCH_MODEL_KINDS",
    "TEAM_ONLY_SCHEMA",
    "TEAM_PLUS_DRAFT_ROSH_SCHEMA",
    "TEAM_PLUS_DRAFT_SCHEMA",
    "TEAM_PLUS_ROSH_SCHEMA",
    "PrematchFeatureSnapshot",
    "build_prematch_feature_snapshot",
    "prematch_feature_schema",
    "prematch_feature_schema_hash",
    "project_prematch_features",
    "verify_prematch_feature_snapshot",
]
