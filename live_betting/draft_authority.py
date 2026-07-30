"""Exact immutable authority for one prospective live-draft landmark."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from database.session import PostgresSession


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HORIZONS = frozenset({10, 20, 30, 40, 50})


def _utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("draft authority timestamps must include an offset")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class DraftLandmarkAuthority:
    """The database-verifiable identity and values used by one decision."""

    curve_key: str
    source_ref: str
    landmark_key: str
    horizon_minutes: int
    target: str
    radiant_probability: float
    quality: float
    uncertainty: float
    support: int
    radiant_team_side: str
    strict_mapping_id: int
    deployment_key: str
    target_snapshot_hash: str
    feature_hash: str
    model_hash: str
    calibration_hash: str
    model_version: str
    global_gate_ref: str
    input_snapshot_hash: str
    authority_revision: int
    dependency_revision: int

    def __post_init__(self) -> None:
        for name, value in (
            ("curve_key", self.curve_key),
            ("landmark_key", self.landmark_key),
            ("deployment_key", self.deployment_key),
            ("target_snapshot_hash", self.target_snapshot_hash),
            ("feature_hash", self.feature_hash),
            ("model_hash", self.model_hash),
            ("calibration_hash", self.calibration_hash),
            ("input_snapshot_hash", self.input_snapshot_hash),
        ):
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if self.source_ref != f"prospective-draft:{self.curve_key}":
            raise ValueError("source_ref must identify the exact prospective curve")
        if self.horizon_minutes not in _HORIZONS:
            raise ValueError("draft authority horizon is unsupported")
        if self.target != "radiant_win":
            raise ValueError("draft authority target must be radiant_win")
        for name, value in (
            ("radiant_probability", self.radiant_probability),
            ("quality", self.quality),
            ("uncertainty", self.uncertainty),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{name} must be finite")
        if not 0.0 <= float(self.radiant_probability) <= 1.0:
            raise ValueError("radiant_probability must be between 0 and 1")
        if not 0.0 <= float(self.quality) <= 1.0:
            raise ValueError("quality must be between 0 and 1")
        if not 0.0 <= float(self.uncertainty) <= 0.5:
            raise ValueError("uncertainty must be between 0 and 0.5")
        if isinstance(self.support, bool) or not isinstance(self.support, int):
            raise ValueError("support must be an integer")
        if self.support < 100:
            raise ValueError("passed live draft authority requires support >= 100")
        if self.radiant_team_side not in {"team_one", "team_two"}:
            raise ValueError("radiant_team_side must be team_one or team_two")
        if (
            isinstance(self.strict_mapping_id, bool)
            or not isinstance(self.strict_mapping_id, int)
            or self.strict_mapping_id < 1
        ):
            raise ValueError("strict_mapping_id must be a positive integer")
        if not isinstance(self.model_version, str) or not self.model_version.strip():
            raise ValueError("model_version must be non-empty")
        if not isinstance(self.global_gate_ref, str) or not self.global_gate_ref.strip():
            raise ValueError("global_gate_ref must be non-empty")
        for name, value in (
            ("authority_revision", self.authority_revision),
            ("dependency_revision", self.dependency_revision),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


def authority_from_curve(
    curve: Any,
    point: Any,
    *,
    radiant_team_side: str | None,
) -> DraftLandmarkAuthority | None:
    """Return authority only for a complete publisher-loaded passed landmark."""

    try:
        if (
            point is None
            or not point.passes_live_gate
            or curve.curve_key is None
            or curve.deployment_key is None
            or curve.target_snapshot_hash is None
            or curve.strict_mapping_id is None
            or curve.source_ref is None
            or curve.authority_revision is None
            or curve.dependency_revision is None
            or point.landmark_key is None
            or point.curve_key != curve.curve_key
            or point.deployment_key != curve.deployment_key
            or point.target_snapshot_hash != curve.target_snapshot_hash
            or point.uncertainty is None
            or point.feature_hash is None
            or point.model_hash is None
            or point.calibration_hash is None
            or point.input_snapshot_hash is None
        ):
            return None
        return DraftLandmarkAuthority(
            curve_key=curve.curve_key,
            source_ref=curve.source_ref,
            landmark_key=point.landmark_key,
            horizon_minutes=point.minute,
            target="radiant_win",
            radiant_probability=float(point.radiant_probability),
            quality=float(point.quality),
            uncertainty=float(point.uncertainty),
            support=int(point.support),
            radiant_team_side=str(radiant_team_side),
            strict_mapping_id=curve.strict_mapping_id,
            deployment_key=curve.deployment_key,
            target_snapshot_hash=curve.target_snapshot_hash,
            feature_hash=point.feature_hash,
            model_hash=point.model_hash,
            calibration_hash=point.calibration_hash,
            model_version=point.model_version,
            global_gate_ref=point.global_gate_ref,
            input_snapshot_hash=point.input_snapshot_hash,
            authority_revision=curve.authority_revision,
            dependency_revision=curve.dependency_revision,
        )
    except (AttributeError, TypeError, ValueError):
        return None


def authority_from_row(
    row: Any,
    *,
    prefix: str = "draft_",
) -> DraftLandmarkAuthority | None:
    """Decode persisted authority columns without accepting partial legacy rows."""

    try:
        return DraftLandmarkAuthority(
            curve_key=row[f"{prefix}curve_key"],
            source_ref=row[f"{prefix}source_ref"],
            landmark_key=row[f"{prefix}landmark_key"],
            horizon_minutes=row[f"{prefix}landmark_horizon_minutes"],
            target=row[f"{prefix}landmark_target"],
            radiant_probability=row[
                f"{prefix}landmark_radiant_probability"
            ],
            quality=row[f"{prefix}landmark_quality"],
            uncertainty=row[f"{prefix}landmark_uncertainty"],
            support=row[f"{prefix}landmark_support"],
            radiant_team_side=row[f"{prefix}radiant_team_side"],
            strict_mapping_id=row[f"{prefix}strict_mapping_id"],
            deployment_key=row[f"{prefix}deployment_key"],
            target_snapshot_hash=row[f"{prefix}target_snapshot_hash"],
            feature_hash=row[f"{prefix}feature_hash"],
            model_hash=row[f"{prefix}model_hash"],
            calibration_hash=row[f"{prefix}calibration_hash"],
            model_version=row[f"{prefix}model_version"],
            global_gate_ref=row[f"{prefix}global_gate_ref"],
            input_snapshot_hash=row[f"{prefix}input_snapshot_hash"],
            authority_revision=row[f"{prefix}authority_revision"],
            dependency_revision=row[f"{prefix}dependency_revision"],
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def draft_landmark_authority_matches(
    connection: PostgresSession,
    authority: DraftLandmarkAuthority,
    *,
    raybet_match_id: str,
    map_number: int,
    strict_mapping_id: int,
    radiant_hero_ids: tuple[int, ...] | None,
    dire_hero_ids: tuple[int, ...] | None,
    observed_at: datetime,
    require_current_revisions: bool,
    verify_curve: bool = True,
) -> bool:
    """Re-read every persisted identity/value needed by a live decision."""

    if not isinstance(authority, DraftLandmarkAuthority):
        return False
    if authority.strict_mapping_id != strict_mapping_id:
        return False
    try:
        row = connection.execute(
            """SELECT curve.raybet_match_id, curve.map_number,
                      curve.strict_mapping_id, curve.radiant_hero_ids_json,
                      curve.dire_hero_ids_json, curve.prediction_cutoff,
                      curve.first_usable_at, curve.created_at,
                      curve.availability_mode, curve.radiant_team_side,
                      curve.deployment_key, curve.target_snapshot_hash,
                      landmark.curve_key, landmark.horizon_minutes,
                      landmark.radiant_probability, landmark.quality,
                      landmark.validation_status, landmark.support,
                      landmark.uncertainty, landmark.feature_hash,
                      landmark.model_hash, landmark.calibration_hash,
                      landmark.global_calibration_passed,
                      landmark.model_version, landmark.model_kind,
                      landmark.availability_mode,
                      landmark.input_snapshot_hash, landmark.deployment_key,
                      model.model_version, model.model_kind,
                      model.horizon_minutes, model.feature_schema_hash,
                      calibration.model_hash, calibration.horizon_minutes,
                      calibration.support,
                      json_extract(deployment.model_hashes_json,
                                   '$."' || landmark.horizon_minutes || '"'),
                      json_extract(deployment.calibration_hashes_json,
                                   '$."' || landmark.horizon_minutes || '"'),
                      landmark.global_gate_ref
                 FROM prospective_draft_curves AS curve
                 JOIN prospective_draft_landmarks AS landmark
                   ON landmark.curve_key=curve.curve_key
                 JOIN draft_model_artifacts AS model
                   ON model.model_hash=landmark.model_hash
                 JOIN draft_calibration_artifacts AS calibration
                   ON calibration.calibration_hash=landmark.calibration_hash
                 JOIN draft_deployment_bundles AS deployment
                   ON deployment.deployment_key=curve.deployment_key
                WHERE curve.curve_key=? AND landmark.landmark_key=?""",
            (authority.curve_key, authority.landmark_key),
        ).fetchone()
        if row is None:
            return False
        radiant = tuple(json.loads(str(row[3])))
        dire = tuple(json.loads(str(row[4])))
        cutoff = _utc(row[5])
        first_usable = _utc(row[6])
        created = _utc(row[7])
        as_of = observed_at.astimezone(timezone.utc)
    except (json.JSONDecodeError, SQLAlchemyError, TypeError, ValueError):
        return False
    exact = (
        (str(row[0]), int(row[1]), int(row[2]))
        == (str(raybet_match_id), map_number, strict_mapping_id)
        and (
            radiant_hero_ids is None
            or dire_hero_ids is None
            or (radiant == radiant_hero_ids and dire == dire_hero_ids)
        )
        and len(radiant) == 5
        and len(dire) == 5
        and len(set(radiant + dire)) == 10
        and cutoff <= first_usable <= created <= as_of
        and str(row[8]) == "prospective"
        and str(row[9]) == authority.radiant_team_side
        and str(row[10]) == authority.deployment_key
        and str(row[11]) == authority.target_snapshot_hash
        and str(row[12]) == authority.curve_key
        and int(row[13]) == authority.horizon_minutes
        and float(row[14]) == authority.radiant_probability
        and float(row[15]) == authority.quality
        and str(row[16]) == "passed"
        and int(row[17]) == authority.support
        and row[18] is not None
        and float(row[18]) == authority.uncertainty
        and str(row[19]) == authority.feature_hash
        and str(row[20]) == authority.model_hash
        and str(row[21]) == authority.calibration_hash
        and int(row[22]) == 1
        and str(row[23]) == authority.model_version
        and str(row[24]) == "pure_draft"
        and str(row[25]) == "prospective"
        and str(row[26]) == authority.input_snapshot_hash
        and str(row[27]) == authority.deployment_key
        and str(row[28]) == authority.model_version
        and str(row[29]) == "pure_draft"
        and int(row[30]) == authority.horizon_minutes
        and str(row[31]) == authority.feature_hash
        and str(row[32]) == authority.model_hash
        and int(row[33]) == authority.horizon_minutes
        and int(row[34]) == authority.support
        and str(row[35]) == authority.model_hash
        and str(row[36]) == authority.calibration_hash
        and str(row[37]) == authority.global_gate_ref
    )
    if not exact:
        return False
    if require_current_revisions:
        try:
            revisions = connection.execute(
                """SELECT authority.authority_revision,
                          lineage.dependency_revision
                     FROM draft_authority_revisions AS authority
                     JOIN draft_lineage_revisions AS lineage
                       ON lineage.singleton=authority.singleton
                    WHERE authority.singleton=1"""
            ).fetchone()
        except SQLAlchemyError:
            return False
        if revisions is None or tuple(revisions) != (
            authority.authority_revision,
            authority.dependency_revision,
        ):
            return False
    if verify_curve:
        try:
            from .profiles.draft_curve import prospective_curve_authority_matches

            return prospective_curve_authority_matches(
                connection, authority.curve_key
            )
        except (ImportError, SQLAlchemyError, TypeError, ValueError):
            return False
    return True


__all__ = [
    "DraftLandmarkAuthority",
    "authority_from_curve",
    "authority_from_row",
    "draft_landmark_authority_matches",
]
