"""Publish immutable prospective draft curves from strict live draft anchors."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from event_intelligence.backtest import HORIZONS, draft_dependency_fingerprint
from event_intelligence.deployment import (
    DEPLOYMENT_VERSION,
    FrozenDraftDeployment,
    build_frozen_draft_deployment,
    load_prospective_history,
    split_calibration_samples,
)
from event_intelligence.draft_artifacts import (
    CalibrationSample,
    DraftCalibrationArtifact,
    assert_model_calibration_compatible,
    build_calibration_artifact,
    calibration_artifact_from_payload,
    canonical_hash,
    canonical_json_bytes,
    model_artifact_from_payload,
)
from event_intelligence.draft_features import (
    AvailabilityMode,
    DerivedFactProvenance,
    DraftFeatureSnapshot,
    DraftMapEvidence,
    DraftPlayer,
    DraftTarget,
    DraftTeam,
    ExpectedRoleAssignment,
    build_draft_feature_snapshot,
)
from event_intelligence.draft_model import (
    DraftModelArtifact,
    PredictionStatus,
    predict_draft,
)
from event_intelligence.models import RolePurpose
from event_intelligence.roles import RoleSource
from shared.sqlite import connect

from .database_protocol import verify_prepared_database
from .health import record_health
from .storage import LiveBettingStore
from .strict_eligibility import StrictLiveMapMapping, query_strict_live_eligibility


ROOT = Path(__file__).resolve().parents[1]
PUBLISHER_VERSION = "prospective-draft-publisher-v1"
PUBLISHER_COMPONENT = "draft_publisher_worker"
LINEUP_ROLE_VERSION = "live-unknown-role-v1"


@dataclass(frozen=True)
class DraftAnchor:
    raybet_match_id: str
    map_number: int
    draft_hash: str
    radiant_heroes: tuple[int, ...]
    dire_heroes: tuple[int, ...]
    radiant_team_side: str
    anchored_at: datetime
    source_frame_ref: str
    team_side_anchored_at: datetime
    team_side_source_frame_ref: str


@dataclass(frozen=True)
class PublicationResult:
    status: str
    raybet_match_id: str
    map_number: int
    curve_key: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PublisherCycleReport:
    deployment_key: str
    candidates: int
    inserted: int
    unchanged: int
    skipped: int
    outcomes_inserted: int
    results: tuple[PublicationResult, ...]


def _parse_utc(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _json_text(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _deployment_identity(
    *,
    training_cutoff: datetime,
    dependency_fingerprint: str,
    dependency_revision: int,
    models: Sequence[DraftModelArtifact],
    calibrations: Sequence[DraftCalibrationArtifact],
    evidence_mode: str,
) -> dict[str, object]:
    return {
        "deployment_version": DEPLOYMENT_VERSION,
        "training_cutoff": training_cutoff.isoformat(),
        "dependency_fingerprint": dependency_fingerprint,
        "dependency_revision": dependency_revision,
        "model_hashes": {
            str(row.horizon_minutes): row.model_hash for row in models
        },
        "calibration_hashes": {
            str(row.horizon_minutes): row.calibration_hash
            for row in calibrations
        },
        "evidence_mode": evidence_mode,
    }


def _expected_artifact_row(
    artifact: DraftModelArtifact,
) -> tuple[object, ...]:
    return (
        artifact.model_version,
        artifact.model_kind,
        artifact.horizon_minutes,
        artifact.training_cutoff.isoformat(),
        artifact.feature_schema_hash,
        artifact.training_input_hash,
        _json_text(artifact.to_payload()),
    )


def _expected_calibration_row(
    artifact: DraftCalibrationArtifact,
) -> tuple[object, ...]:
    return (
        artifact.model_hash,
        artifact.calibration_version,
        artifact.horizon_minutes,
        artifact.evidence_mode,
        artifact.support,
        _json_text(artifact.to_payload()),
    )


def persist_frozen_deployment(
    connection: sqlite3.Connection,
    deployment: FrozenDraftDeployment,
    *,
    created_at: datetime,
) -> bool:
    """Publish a complete five-horizon bundle after rechecking source lineage."""

    created = created_at.astimezone(timezone.utc)
    if {row.horizon_minutes for row in deployment.models} != set(HORIZONS):
        raise ValueError("deployment must contain five model horizons")
    if {row.horizon_minutes for row in deployment.calibrations} != set(HORIZONS):
        raise ValueError("deployment must contain five calibration horizons")
    for model in deployment.models:
        assert_model_calibration_compatible(
            model, deployment.calibration(model.horizon_minutes)
        )
        if model.model_kind != "pure_draft":
            raise ValueError("live deployment only accepts pure_draft models")
    identity = _deployment_identity(
        training_cutoff=deployment.training_cutoff,
        dependency_fingerprint=deployment.dependency_fingerprint,
        dependency_revision=deployment.dependency_revision,
        models=deployment.models,
        calibrations=deployment.calibrations,
        evidence_mode=deployment.evidence_mode,
    )
    if canonical_hash(identity) != deployment.deployment_key:
        raise ValueError("deployment key does not match its artifacts")

    connection.execute("BEGIN IMMEDIATE")
    try:
        revision_row = connection.execute(
            """SELECT dependency_revision FROM draft_lineage_revisions
                 WHERE singleton=1"""
        ).fetchone()
        if revision_row is None or int(revision_row[0]) != deployment.dependency_revision:
            raise RuntimeError("draft dependencies changed before deployment publish")
        if draft_dependency_fingerprint(connection) != deployment.dependency_fingerprint:
            raise RuntimeError("draft dependency fingerprint changed before publish")

        for model in deployment.models:
            expected = _expected_artifact_row(model)
            existing = connection.execute(
                """SELECT model_version, model_kind, horizon_minutes,
                          training_cutoff, feature_schema_hash,
                          training_input_hash, artifact_json
                     FROM draft_model_artifacts WHERE model_hash=?""",
                (model.model_hash,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO draft_model_artifacts
                       (model_hash, model_version, model_kind, horizon_minutes,
                        training_cutoff, feature_schema_hash, training_input_hash,
                        artifact_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (model.model_hash, *expected, created.isoformat()),
                )
            elif tuple(existing) != expected:
                raise ValueError(f"immutable model artifact conflict: {model.model_hash}")

        for calibration in deployment.calibrations:
            expected = _expected_calibration_row(calibration)
            existing = connection.execute(
                """SELECT model_hash, calibration_version, horizon_minutes,
                          evidence_mode, support, artifact_json
                     FROM draft_calibration_artifacts WHERE calibration_hash=?""",
                (calibration.calibration_hash,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO draft_calibration_artifacts
                       (calibration_hash, model_hash, calibration_version,
                        horizon_minutes, evidence_mode, support, artifact_json,
                        created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        calibration.calibration_hash,
                        *expected,
                        created.isoformat(),
                    ),
                )
            elif tuple(existing) != expected:
                raise ValueError(
                    "immutable calibration artifact conflict: "
                    f"{calibration.calibration_hash}"
                )

        model_hashes = identity["model_hashes"]
        calibration_hashes = identity["calibration_hashes"]
        expected_bundle = (
            _json_text(model_hashes),
            _json_text(calibration_hashes),
            deployment.training_cutoff.isoformat(),
            deployment.dependency_fingerprint,
            deployment.dependency_revision,
            deployment.evidence_mode,
        )
        existing_bundle = connection.execute(
            """SELECT model_hashes_json, calibration_hashes_json,
                      training_cutoff, dependency_fingerprint,
                      dependency_revision, evidence_mode
                 FROM draft_deployment_bundles WHERE deployment_key=?""",
            (deployment.deployment_key,),
        ).fetchone()
        inserted = existing_bundle is None
        if inserted:
            connection.execute(
                """INSERT INTO draft_deployment_bundles
                   (deployment_key, model_hashes_json, calibration_hashes_json,
                    training_cutoff, dependency_fingerprint,
                    dependency_revision, evidence_mode, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    deployment.deployment_key,
                    *expected_bundle,
                    created.isoformat(),
                ),
            )
        elif tuple(existing_bundle) != expected_bundle:
            raise ValueError(
                f"immutable deployment conflict: {deployment.deployment_key}"
            )
        connection.commit()
        return inserted
    except BaseException:
        connection.rollback()
        raise


def _hash_map(value: object, field: str) -> dict[int, str]:
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"{field} is invalid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{field} must be an object")
    result: dict[int, str] = {}
    for horizon in HORIZONS:
        digest = payload.get(str(horizon))
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"{field} lacks horizon {horizon}")
        result[horizon] = digest
    if set(payload) != {str(value) for value in HORIZONS}:
        raise ValueError(f"{field} contains unsupported horizons")
    return result


def load_latest_frozen_deployment(
    connection: sqlite3.Connection,
) -> FrozenDraftDeployment | None:
    row = connection.execute(
        """SELECT deployment_key, model_hashes_json,
                  calibration_hashes_json, training_cutoff,
                  dependency_fingerprint, dependency_revision,
                  evidence_mode, created_at
             FROM draft_deployment_bundles
            ORDER BY julianday(created_at) DESC, deployment_key DESC
            LIMIT 1"""
    ).fetchone()
    if row is None:
        return None
    model_hashes = _hash_map(row[1], "model_hashes_json")
    calibration_hashes = _hash_map(row[2], "calibration_hashes_json")
    models: list[DraftModelArtifact] = []
    calibrations: list[DraftCalibrationArtifact] = []
    for horizon in HORIZONS:
        model_row = connection.execute(
            """SELECT artifact_json FROM draft_model_artifacts
                WHERE model_hash=? AND horizon_minutes=?""",
            (model_hashes[horizon], horizon),
        ).fetchone()
        calibration_row = connection.execute(
            """SELECT artifact_json FROM draft_calibration_artifacts
                WHERE calibration_hash=? AND model_hash=?
                  AND horizon_minutes=?""",
            (calibration_hashes[horizon], model_hashes[horizon], horizon),
        ).fetchone()
        if model_row is None or calibration_row is None:
            raise ValueError("deployment references a missing artifact")
        try:
            model_payload = json.loads(str(model_row[0]))
            calibration_payload = json.loads(str(calibration_row[0]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("deployment artifact JSON is invalid") from error
        model = model_artifact_from_payload(model_payload)
        calibration = calibration_artifact_from_payload(calibration_payload)
        assert_model_calibration_compatible(model, calibration)
        models.append(model)
        calibrations.append(calibration)
    cutoff = _parse_utc(row[3], "deployment training_cutoff")
    dependency_fingerprint = str(row[4])
    revision = int(row[5])
    evidence_mode = str(row[6])
    identity = _deployment_identity(
        training_cutoff=cutoff,
        dependency_fingerprint=dependency_fingerprint,
        dependency_revision=revision,
        models=models,
        calibrations=calibrations,
        evidence_mode=evidence_mode,
    )
    deployment_key = str(row[0])
    if canonical_hash(identity) != deployment_key:
        raise ValueError("deployment bundle hash does not recompute")
    deployment = FrozenDraftDeployment(
        deployment_key=deployment_key,
        training_cutoff=cutoff,
        dependency_fingerprint=dependency_fingerprint,
        dependency_revision=revision,
        models=tuple(models),
        calibrations=tuple(calibrations),
    )
    if deployment.evidence_mode != evidence_mode:
        raise ValueError("deployment evidence mode disagrees with calibrations")
    if _parse_utc(row[7], "deployment created_at") < cutoff:
        raise ValueError("deployment was created before its training cutoff")
    return deployment


def _heroes(value: object, field: str) -> tuple[int, ...]:
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"{field} is invalid JSON") from error
    if (
        not isinstance(payload, list)
        or len(payload) != 5
        or any(type(hero) is not int or hero <= 0 for hero in payload)
        or len(set(payload)) != 5
    ):
        raise ValueError(f"{field} must contain five unique hero IDs")
    return tuple(payload)


def _anchor_from_row(row: sqlite3.Row | Sequence[object]) -> DraftAnchor:
    radiant = _heroes(row[3], "radiant heroes")
    dire = _heroes(row[4], "dire heroes")
    if set(radiant) & set(dire):
        raise ValueError("draft anchor contains duplicate heroes")
    team_side = str(row[5])
    if team_side not in {"team_one", "team_two"}:
        raise ValueError("draft anchor lacks a trusted radiant team side")
    anchor = DraftAnchor(
        raybet_match_id=str(row[0]),
        map_number=int(row[1]),
        draft_hash=str(row[2]),
        radiant_heroes=radiant,
        dire_heroes=dire,
        radiant_team_side=team_side,
        anchored_at=_parse_utc(row[6], "anchor anchored_at"),
        source_frame_ref=str(row[7]),
        team_side_anchored_at=_parse_utc(row[8], "team_side_anchored_at"),
        team_side_source_frame_ref=str(row[9]),
    )
    expected_hash = hashlib.sha256(
        json.dumps(
            {"radiant": list(radiant), "dire": list(dire)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if anchor.draft_hash != expected_hash:
        raise ValueError("draft anchor hash does not match its heroes")
    if not anchor.source_frame_ref or not anchor.team_side_source_frame_ref:
        raise ValueError("draft anchor source frames are required")
    return anchor


def ready_draft_anchors(connection: sqlite3.Connection) -> tuple[DraftAnchor, ...]:
    rows = connection.execute(
        """SELECT anchor.raybet_match_id, anchor.map_number,
                  anchor.draft_hash, anchor.radiant_hero_ids,
                  anchor.dire_hero_ids, anchor.radiant_team_side,
                  anchor.anchored_at, anchor.source_frame_ref,
                  anchor.team_side_anchored_at,
                  anchor.team_side_source_frame_ref
             FROM vision_draft_anchors AS anchor
            WHERE anchor.status='anchored'
              AND anchor.radiant_team_side IN ('team_one', 'team_two')
              AND anchor.team_side_anchored_at IS NOT NULL
              AND anchor.team_side_source_frame_ref IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM vision_draft_conflicts AS conflict
                   WHERE conflict.raybet_match_id=anchor.raybet_match_id
                     AND conflict.map_number=anchor.map_number
              )
            ORDER BY anchor.anchored_at, anchor.raybet_match_id,
                     anchor.map_number"""
    ).fetchall()
    anchors = []
    for row in rows:
        try:
            anchors.append(_anchor_from_row(row))
        except (TypeError, ValueError):
            continue
    return tuple(anchors)


def _synthetic_id(payload: Mapping[str, object], *, negative: bool = False) -> int:
    value = (1 << 61) + int(canonical_hash(payload)[:15], 16) + 1
    return -value if negative else value


def _unknown_player(
    *,
    raybet_match_id: str,
    map_number: int,
    team_id: int,
    side: str,
    index: int,
    hero_id: int,
    cutoff: datetime,
) -> DraftPlayer:
    role_hash = canonical_hash(
        {
            "version": LINEUP_ROLE_VERSION,
            "raybet_match_id": raybet_match_id,
            "map_number": map_number,
            "team_id": team_id,
            "side": side,
            "index": index,
            "hero_id": hero_id,
            "status": "player_identity_not_observed",
        }
    )
    return DraftPlayer(
        player_id=_synthetic_id(
            {
                "raybet_match_id": raybet_match_id,
                "map_number": map_number,
                "team_id": team_id,
                "side": side,
                "index": index,
            },
            negative=True,
        ),
        hero_id=hero_id,
        expected_role=ExpectedRoleAssignment(
            purpose=RolePurpose.EXPECTED_POSITION,
            source=RoleSource.UNKNOWN,
            position=None,
            confidence=0.0,
            provenance=DerivedFactProvenance(
                cutoff=cutoff,
                first_usable_at=cutoff,
                input_hash=role_hash,
                version=LINEUP_ROLE_VERSION,
            ),
        ),
    )


def _latest_patch(connection: sqlite3.Connection, cutoff: datetime) -> int | None:
    row = connection.execute(
        """SELECT patch FROM matches
            WHERE patch IS NOT NULL AND start_time IS NOT NULL
              AND start_time<=?
            ORDER BY start_time DESC, match_id DESC LIMIT 1""",
        (int(cutoff.timestamp()),),
    ).fetchone()
    return None if row is None else int(row[0])


def _live_target(
    connection: sqlite3.Connection,
    anchor: DraftAnchor,
    mapping: StrictLiveMapMapping,
    cutoff: datetime,
) -> DraftTarget:
    radiant_team_id = (
        mapping.canonical_team_one_id
        if anchor.radiant_team_side == "team_one"
        else mapping.canonical_team_two_id
    )
    dire_team_id = (
        mapping.canonical_team_two_id
        if anchor.radiant_team_side == "team_one"
        else mapping.canonical_team_one_id
    )

    def team(team_id: int, side: str, heroes: tuple[int, ...]) -> DraftTeam:
        return DraftTeam(
            team_id=team_id,
            players=tuple(
                _unknown_player(
                    raybet_match_id=anchor.raybet_match_id,
                    map_number=anchor.map_number,
                    team_id=team_id,
                    side=side,
                    index=index,
                    hero_id=hero_id,
                    cutoff=cutoff,
                )
                for index, hero_id in enumerate(heroes)
            ),
        )

    return DraftTarget(
        match_id=_synthetic_id(
            {
                "source": "raybet-live",
                "raybet_match_id": anchor.raybet_match_id,
                "map_number": anchor.map_number,
                "strict_mapping_id": mapping.mapping_id,
            }
        ),
        prediction_cutoff=cutoff,
        event_id=mapping.event_id,
        patch=_latest_patch(connection, cutoff),
        radiant=team(radiant_team_id, "radiant", anchor.radiant_heroes),
        dire=team(dire_team_id, "dire", anchor.dire_heroes),
        availability_mode=AvailabilityMode.PROSPECTIVE,
        map_number=anchor.map_number,
    )


def _curve_key(
    *,
    anchor: DraftAnchor,
    mapping_id: int,
    deployment_key: str,
    input_snapshot_hash: str,
) -> str:
    return canonical_hash(
        {
            "publisher_version": PUBLISHER_VERSION,
            "raybet_match_id": anchor.raybet_match_id,
            "map_number": anchor.map_number,
            "strict_mapping_id": mapping_id,
            "anchor_draft_hash": anchor.draft_hash,
            "radiant_team_side": anchor.radiant_team_side,
            "deployment_key": deployment_key,
            "input_snapshot_hash": input_snapshot_hash,
        }
    )


def _feature_snapshot_payload(snapshot: DraftFeatureSnapshot) -> dict[str, object]:
    return {
        "match_id": snapshot.match_id,
        "prediction_cutoff": snapshot.prediction_cutoff.isoformat(),
        "availability_mode": snapshot.availability_mode.value,
        "feature_version": snapshot.feature_version,
        "feature_schema": list(snapshot.feature_schema),
        "feature_schema_hash": snapshot.feature_schema_hash,
        "input_hash": snapshot.input_hash,
        "pure_features": [
            {
                "name": row.name,
                "value": row.value,
                "support": row.support,
                "evidence_ids": list(row.evidence_ids),
                "coverage": row.coverage,
                "missing_reason": row.missing_reason,
            }
            for row in snapshot.pure_features
        ],
        "support": snapshot.support,
        "pure_coverage": snapshot.pure_coverage,
        "evidence_ids": list(snapshot.evidence_ids),
    }


def _existing_curve(
    connection: sqlite3.Connection,
    anchor: DraftAnchor,
    mapping_id: int,
    deployment_key: str,
) -> str | None:
    row = connection.execute(
        """SELECT curve_key FROM prospective_draft_curves
            WHERE raybet_match_id=? AND map_number=? AND strict_mapping_id=?
              AND anchor_draft_hash=? AND radiant_team_side=?
              AND deployment_key=?
            ORDER BY first_usable_at, curve_key LIMIT 1""",
        (
            anchor.raybet_match_id,
            anchor.map_number,
            mapping_id,
            anchor.draft_hash,
            anchor.radiant_team_side,
            deployment_key,
        ),
    ).fetchone()
    return None if row is None else str(row[0])


def _frame_is_authoritative(
    connection: sqlite3.Connection,
    anchor: DraftAnchor,
) -> bool:
    refs = {
        (anchor.anchored_at.isoformat(), anchor.source_frame_ref),
        (
            anchor.team_side_anchored_at.isoformat(),
            anchor.team_side_source_frame_ref,
        ),
    }
    for captured_at, frame_ref in refs:
        row = connection.execute(
            """SELECT radiant_hero_ids, dire_hero_ids, radiant_team_side,
                      confirmed
                 FROM vision_observations
                WHERE raybet_match_id=? AND map_number=?
                  AND captured_at=? AND source_frame_ref=?
                  AND NOT EXISTS (
                      SELECT 1 FROM vision_observation_invalidations AS invalidation
                       WHERE invalidation.raybet_match_id=vision_observations.raybet_match_id
                         AND invalidation.captured_at=vision_observations.captured_at
                         AND invalidation.source_frame_ref=vision_observations.source_frame_ref
                  )""",
            (
                anchor.raybet_match_id,
                anchor.map_number,
                captured_at,
                frame_ref,
            ),
        ).fetchone()
        if row is None or int(row[3]) != 1:
            return False
        try:
            radiant = _heroes(row[0], "frame radiant heroes")
            dire = _heroes(row[1], "frame dire heroes")
        except ValueError:
            return False
        if radiant != anchor.radiant_heroes or dire != anchor.dire_heroes:
            return False
        if frame_ref == anchor.team_side_source_frame_ref and row[2] != anchor.radiant_team_side:
            return False
    return True


def publish_anchor_curve(
    connection: sqlite3.Connection,
    *,
    anchor: DraftAnchor,
    deployment: FrozenDraftDeployment,
    history: Iterable[DraftMapEvidence],
    published_at: datetime,
) -> PublicationResult:
    now = published_at.astimezone(timezone.utc)
    eligibility = query_strict_live_eligibility(
        connection,
        raybet_match_id=anchor.raybet_match_id,
        map_number=anchor.map_number,
        transport_observed_at=now,
    )
    if not eligibility.eligible or eligibility.mapping is None:
        return PublicationResult(
            "skipped",
            anchor.raybet_match_id,
            anchor.map_number,
            reason=f"strict_mapping:{eligibility.reason}",
        )
    mapping = eligibility.mapping
    existing = _existing_curve(
        connection, anchor, mapping.mapping_id, deployment.deployment_key
    )
    if existing is not None:
        return PublicationResult(
            "unchanged", anchor.raybet_match_id, anchor.map_number, existing
        )
    if not _frame_is_authoritative(connection, anchor):
        return PublicationResult(
            "skipped",
            anchor.raybet_match_id,
            anchor.map_number,
            reason="vision_anchor_evidence_invalid",
        )

    target = _live_target(connection, anchor, mapping, now)
    snapshot = build_draft_feature_snapshot(target, history)
    predictions: list[dict[str, object]] = []
    for horizon in HORIZONS:
        model = deployment.model(horizon)
        calibration = deployment.calibration(horizon)
        prediction = predict_draft(model, snapshot.pure_values())
        if prediction.status is not PredictionStatus.PREDICTED or prediction.probability is None:
            raise ValueError(f"deployment horizon {horizon} could not predict")
        raw_probability = prediction.probability
        probability = calibration.apply(raw_probability)
        uncertainty = prediction.uncertainty
        if uncertainty is not None:
            uncertainty = min(
                0.5,
                uncertainty * max(1.0, abs(calibration.slope)),
            )
        passed = calibration.passes_live_gate and calibration.support >= 100
        predictions.append(
            {
                "horizon": horizon,
                "model": model,
                "calibration": calibration,
                "raw_probability": raw_probability,
                "probability": probability,
                "uncertainty": uncertainty,
                "raw_uncertainty": prediction.uncertainty,
                "model_input_hash": prediction.input_snapshot_hash,
                "passed": passed,
                "validation_reason": (
                    None
                    if passed
                    else (
                        "prospective_calibration_gate_not_passed:"
                        + (
                            ",".join(calibration.gate.reasons)
                            if calibration.evidence_mode == "prospective"
                            else "prospective_evidence_required"
                        )
                    )
                ),
            }
        )
    curve_key = _curve_key(
        anchor=anchor,
        mapping_id=mapping.mapping_id,
        deployment_key=deployment.deployment_key,
        input_snapshot_hash=snapshot.input_hash,
    )
    lineup_hash = canonical_hash(
        {
            "dire": list(anchor.dire_heroes),
            "radiant": list(anchor.radiant_heroes),
        }
    )
    scaling = snapshot.feature("scaling_40m_win_rate_diff").value or 0.0
    synergy = snapshot.feature("synergy_win_rate_diff").value or 0.0
    input_refs = [
        f"strict-mapping:{mapping.mapping_id}",
        f"vision-draft:{anchor.draft_hash}",
        f"vision-frame:{anchor.source_frame_ref}",
        f"draft-snapshot:{snapshot.input_hash}",
        f"draft-deployment:{deployment.deployment_key}",
    ]

    connection.execute("BEGIN IMMEDIATE")
    try:
        current = connection.execute(
            """SELECT draft_hash, radiant_team_side, status,
                      anchored_at, source_frame_ref
                 FROM vision_draft_anchors
                WHERE raybet_match_id=? AND map_number=?""",
            (anchor.raybet_match_id, anchor.map_number),
        ).fetchone()
        if current is None or tuple(current) != (
            anchor.draft_hash,
            anchor.radiant_team_side,
            "anchored",
            anchor.anchored_at.isoformat(),
            anchor.source_frame_ref,
        ):
            raise RuntimeError("vision anchor changed before publication")
        if connection.execute(
            """SELECT 1 FROM vision_draft_conflicts
                WHERE raybet_match_id=? AND map_number=? LIMIT 1""",
            (anchor.raybet_match_id, anchor.map_number),
        ).fetchone() is not None:
            raise RuntimeError("vision draft conflict appeared before publication")
        strict = query_strict_live_eligibility(
            connection,
            raybet_match_id=anchor.raybet_match_id,
            map_number=anchor.map_number,
            transport_observed_at=now,
        )
        if (
            not strict.eligible
            or strict.mapping is None
            or strict.mapping.mapping_id != mapping.mapping_id
        ):
            raise RuntimeError("strict mapping changed before publication")
        duplicate = _existing_curve(
            connection, anchor, mapping.mapping_id, deployment.deployment_key
        )
        if duplicate is not None:
            connection.rollback()
            return PublicationResult(
                "unchanged", anchor.raybet_match_id, anchor.map_number, duplicate
            )
        connection.execute(
            """INSERT INTO prospective_draft_curves
               (curve_key, raybet_match_id, map_number, strict_mapping_id,
                lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
                prediction_cutoff, first_usable_at, availability_mode,
                created_at, radiant_team_side, anchor_draft_hash,
                anchor_source_frame_ref, anchor_anchored_at, deployment_key,
                target_snapshot_hash, feature_snapshot_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prospective', ?, ?, ?, ?,
                       ?, ?, ?, ?)""",
            (
                curve_key,
                anchor.raybet_match_id,
                anchor.map_number,
                mapping.mapping_id,
                lineup_hash,
                _json_text(list(anchor.radiant_heroes)),
                _json_text(list(anchor.dire_heroes)),
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
                anchor.radiant_team_side,
                anchor.draft_hash,
                anchor.source_frame_ref,
                anchor.anchored_at.isoformat(),
                deployment.deployment_key,
                snapshot.input_hash,
                _json_text(_feature_snapshot_payload(snapshot)),
            ),
        )
        for row in predictions:
            horizon = int(row["horizon"])
            model = row["model"]
            calibration = row["calibration"]
            assert isinstance(model, DraftModelArtifact)
            assert isinstance(calibration, DraftCalibrationArtifact)
            passed = bool(row["passed"])
            landmark_key = canonical_hash(
                {"curve_key": curve_key, "horizon_minutes": horizon}
            )
            refs = [
                *input_refs,
                f"draft-model:{model.model_hash}",
                f"draft-calibration:{calibration.calibration_hash}",
            ]
            connection.execute(
                """INSERT INTO prospective_draft_landmarks
                   (landmark_key, curve_key, horizon_minutes,
                    radiant_probability, scaling_edge, synergy_edge, quality,
                    validation_status, support, calibration_ref,
                    input_refs_json, uncertainty, validation_reason,
                    feature_hash, model_hash, calibration_hash,
                    global_calibration_passed, global_gate_ref, model_version,
                    model_kind, availability_mode, input_snapshot_hash,
                    created_at, raw_radiant_probability, deployment_key,
                    model_input_hash, raw_uncertainty)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, 'pure_draft', 'prospective', ?, ?, ?, ?, ?, ?)""",
                (
                    landmark_key,
                    curve_key,
                    horizon,
                    float(row["probability"]),
                    float(scaling),
                    float(synergy),
                    float(snapshot.pure_coverage),
                    "passed" if passed else "failed",
                    calibration.support,
                    f"draft-calibration:{calibration.calibration_hash}",
                    _json_text(refs),
                    row["uncertainty"],
                    row["validation_reason"],
                    model.feature_schema_hash,
                    model.model_hash,
                    calibration.calibration_hash,
                    int(passed),
                    f"draft-calibration:{calibration.calibration_hash}",
                    model.model_version,
                    snapshot.input_hash,
                    now.isoformat(),
                    float(row["raw_probability"]),
                    deployment.deployment_key,
                    row["model_input_hash"],
                    row["raw_uncertainty"],
                ),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return PublicationResult(
        "inserted", anchor.raybet_match_id, anchor.map_number, curve_key
    )


def append_prospective_outcomes(
    connection: sqlite3.Connection,
    *,
    created_at: datetime,
) -> int:
    """Append exact two-source outcomes without modifying original predictions."""

    now = created_at.astimezone(timezone.utc)
    rows = connection.execute(
        """SELECT curve.curve_key, curve.raybet_match_id, curve.map_number,
                  curve.strict_mapping_id, curve.radiant_team_side,
                  curve.first_usable_at, result.dota_match_id,
                  result.winner_side, result.evidence_ref, result.settled_at,
                  reconciliation.raybet_evidence_ref,
                  reconciliation.opendota_evidence_ref,
                  reconciliation.first_observed_at
             FROM prospective_draft_curves AS curve
             JOIN map_results AS result
               ON result.raybet_match_id=curve.raybet_match_id
              AND result.map_number=curve.map_number
             JOIN settlement_reconciliations AS reconciliation
               ON reconciliation.raybet_match_id=curve.raybet_match_id
              AND reconciliation.map_number=curve.map_number
              AND reconciliation.dota_match_id=result.dota_match_id
              AND reconciliation.status='confirmed'
              AND reconciliation.raybet_winner_side=result.winner_side
              AND reconciliation.opendota_winner_side=result.winner_side
             LEFT JOIN prospective_draft_outcomes AS outcome
               ON outcome.curve_key=curve.curve_key
            WHERE outcome.curve_key IS NULL
              AND julianday(curve.first_usable_at)<julianday(result.settled_at)
            ORDER BY result.settled_at, curve.curve_key"""
    ).fetchall()
    inserted = 0
    for row in rows:
        first_usable = _parse_utc(row[5], "curve first_usable_at")
        eligibility = query_strict_live_eligibility(
            connection,
            raybet_match_id=str(row[1]),
            map_number=int(row[2]),
            transport_observed_at=first_usable,
        )
        if (
            not eligibility.eligible
            or eligibility.mapping is None
            or eligibility.mapping.mapping_id != int(row[3])
        ):
            continue
        if connection.execute(
            """SELECT 1 FROM vision_draft_conflicts
                WHERE raybet_match_id=? AND map_number=? LIMIT 1""",
            (str(row[1]), int(row[2])),
        ).fetchone() is not None:
            continue
        evidence_rows = connection.execute(
            """SELECT source, status, winner_side, evidence_ref, facts_json
                 FROM settlement_result_evidence
                WHERE raybet_match_id=? AND map_number=?
                  AND dota_match_id=?
                  AND evidence_ref IN (?, ?)
                ORDER BY source""",
            (
                str(row[1]),
                int(row[2]),
                int(row[6]),
                str(row[10]),
                str(row[11]),
            ),
        ).fetchall()
        if (
            len(evidence_rows) != 2
            or {str(value[0]) for value in evidence_rows} != {"raybet", "opendota"}
            or any(str(value[1]) != "confirmed" for value in evidence_rows)
            or any(str(value[2]) != str(row[7]) for value in evidence_rows)
        ):
            continue
        evidence_hash = canonical_hash(
            {
                "curve_key": str(row[0]),
                "dota_match_id": int(row[6]),
                "winner_side": str(row[7]),
                "map_result_ref": str(row[8]),
                "reconciliation_observed_at": str(row[12]),
                "evidence": [
                    {
                        "source": str(value[0]),
                        "status": str(value[1]),
                        "winner_side": str(value[2]),
                        "evidence_ref": str(value[3]),
                        "facts_hash": hashlib.sha256(
                            str(value[4]).encode("utf-8")
                        ).hexdigest(),
                    }
                    for value in evidence_rows
                ],
            }
        )
        radiant_team_side = str(row[4])
        if radiant_team_side not in {"team_one", "team_two"}:
            continue
        cursor = connection.execute(
            """INSERT OR IGNORE INTO prospective_draft_outcomes
               (curve_key, strict_mapping_id, dota_match_id, radiant_win,
                winner_side, evidence_ref, evidence_hash, settled_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(row[0]),
                int(row[3]),
                int(row[6]),
                int(str(row[7]) == radiant_team_side),
                str(row[7]),
                str(row[8]),
                evidence_hash,
                str(row[9]),
                now.isoformat(),
            ),
        )
        inserted += int(cursor.rowcount == 1)
    connection.commit()
    return inserted


def _prospective_calibration_samples(
    connection: sqlite3.Connection,
    *,
    model_hash: str,
    horizon_minutes: int,
) -> tuple[CalibrationSample, ...]:
    rows = connection.execute(
        """SELECT curve.curve_key, landmark.raw_radiant_probability,
                  outcome.radiant_win, curve.first_usable_at,
                  outcome.settled_at, curve.raybet_match_id,
                  mapping.event_id
             FROM prospective_draft_curves AS curve
             JOIN prospective_draft_landmarks AS landmark
               ON landmark.curve_key=curve.curve_key
              AND landmark.horizon_minutes=?
              AND landmark.model_hash=?
             JOIN prospective_draft_outcomes AS outcome
               ON outcome.curve_key=curve.curve_key
              AND outcome.strict_mapping_id=curve.strict_mapping_id
             JOIN map_results AS result
               ON result.raybet_match_id=curve.raybet_match_id
              AND result.map_number=curve.map_number
              AND result.dota_match_id=outcome.dota_match_id
              AND result.winner_side=outcome.winner_side
             JOIN settlement_reconciliations AS reconciliation
               ON reconciliation.raybet_match_id=curve.raybet_match_id
              AND reconciliation.map_number=curve.map_number
              AND reconciliation.dota_match_id=outcome.dota_match_id
              AND reconciliation.status='confirmed'
              AND reconciliation.raybet_winner_side=outcome.winner_side
              AND reconciliation.opendota_winner_side=outcome.winner_side
             JOIN settlement_result_evidence AS raybet_evidence
               ON raybet_evidence.raybet_match_id=curve.raybet_match_id
              AND raybet_evidence.map_number=curve.map_number
              AND raybet_evidence.dota_match_id=outcome.dota_match_id
              AND raybet_evidence.source='raybet'
              AND raybet_evidence.status='confirmed'
              AND raybet_evidence.winner_side=outcome.winner_side
              AND raybet_evidence.evidence_ref=
                  reconciliation.raybet_evidence_ref
             JOIN settlement_result_evidence AS opendota_evidence
               ON opendota_evidence.raybet_match_id=curve.raybet_match_id
              AND opendota_evidence.map_number=curve.map_number
              AND opendota_evidence.dota_match_id=outcome.dota_match_id
              AND opendota_evidence.source='opendota'
              AND opendota_evidence.status='confirmed'
              AND opendota_evidence.winner_side=outcome.winner_side
              AND opendota_evidence.evidence_ref=
                  reconciliation.opendota_evidence_ref
             JOIN strict_live_map_mappings AS mapping
               ON mapping.mapping_id=curve.strict_mapping_id
              AND mapping.raybet_match_id=curve.raybet_match_id
              AND mapping.map_number=curve.map_number
             LEFT JOIN strict_live_map_mapping_invalidations AS invalidation
               ON invalidation.mapping_id=mapping.mapping_id
            WHERE invalidation.mapping_id IS NULL
              AND landmark.raw_radiant_probability IS NOT NULL
              AND julianday(curve.first_usable_at)<julianday(outcome.settled_at)
              AND NOT EXISTS (
                  SELECT 1 FROM vision_draft_conflicts AS conflict
                   WHERE conflict.raybet_match_id=curve.raybet_match_id
                     AND conflict.map_number=curve.map_number
              )
            ORDER BY curve.first_usable_at, curve.curve_key""",
        (horizon_minutes, model_hash),
    ).fetchall()
    return tuple(
        CalibrationSample(
            sample_id=f"{row[0]}:{horizon_minutes}",
            probability=float(row[1]),
            outcome=int(row[2]),
            observed_at=_parse_utc(row[3], "curve first_usable_at"),
            settled_at=_parse_utc(row[4], "prospective outcome settled_at"),
            cluster_id=f"raybet:{row[5]}",
            event_id=str(row[6]),
        )
        for row in rows
    )


def build_prospective_calibration_deployment(
    connection: sqlite3.Connection,
    current: FrozenDraftDeployment,
) -> FrozenDraftDeployment | None:
    """Recalibrate one frozen model family from exact prospective outcomes."""

    samples_by_horizon = {
        horizon: _prospective_calibration_samples(
            connection,
            model_hash=current.model(horizon).model_hash,
            horizon_minutes=horizon,
        )
        for horizon in HORIZONS
    }
    if not any(samples_by_horizon.values()):
        return None
    calibrations = []
    for horizon in HORIZONS:
        fit, evaluation = split_calibration_samples(samples_by_horizon[horizon])
        calibrations.append(
            build_calibration_artifact(
                current.model(horizon),
                evidence_mode="prospective",
                source_ref="prospective-draft-outcomes-v1",
                fit_samples=fit,
                evaluation_samples=evaluation,
            )
        )
    revision_row = connection.execute(
        """SELECT dependency_revision FROM draft_lineage_revisions
             WHERE singleton=1"""
    ).fetchone()
    if revision_row is None:
        raise ValueError("draft dependency revision is unavailable")
    revision = int(revision_row[0])
    fingerprint = draft_dependency_fingerprint(connection)
    identity = _deployment_identity(
        training_cutoff=current.training_cutoff,
        dependency_fingerprint=fingerprint,
        dependency_revision=revision,
        models=current.models,
        calibrations=calibrations,
        evidence_mode="prospective",
    )
    candidate = FrozenDraftDeployment(
        deployment_key=canonical_hash(identity),
        training_cutoff=current.training_cutoff,
        dependency_fingerprint=fingerprint,
        dependency_revision=revision,
        models=current.models,
        calibrations=tuple(calibrations),
    )
    return None if candidate.deployment_key == current.deployment_key else candidate


def publish_cycle(
    connection: sqlite3.Connection,
    *,
    deployment: FrozenDraftDeployment,
    history: Iterable[DraftMapEvidence],
    now: datetime,
) -> PublisherCycleReport:
    anchors = ready_draft_anchors(connection)
    results = tuple(
        publish_anchor_curve(
            connection,
            anchor=anchor,
            deployment=deployment,
            history=history,
            published_at=now,
        )
        for anchor in anchors
    )
    outcomes = append_prospective_outcomes(connection, created_at=now)
    return PublisherCycleReport(
        deployment_key=deployment.deployment_key,
        candidates=len(anchors),
        inserted=sum(row.status == "inserted" for row in results),
        unchanged=sum(row.status == "unchanged" for row in results),
        skipped=sum(row.status == "skipped" for row in results),
        outcomes_inserted=outcomes,
        results=results,
    )


def _build_and_persist(database: Path, now: datetime) -> FrozenDraftDeployment:
    reader = connect(database, read_only=True, row_factory=sqlite3.Row)
    try:
        reader.execute("BEGIN")
        deployment = build_frozen_draft_deployment(
            reader,
            training_cutoff=now,
        )
        reader.rollback()
    finally:
        reader.close()
    with LiveBettingStore(database) as store:
        persist_frozen_deployment(store.connection, deployment, created_at=now)
    return deployment


def _load_history(database: Path) -> tuple[DraftMapEvidence, ...]:
    reader = connect(database, read_only=True, row_factory=sqlite3.Row)
    try:
        reader.execute("BEGIN")
        history = load_prospective_history(reader)
        reader.rollback()
        return history
    finally:
        reader.close()


def _record_cycle_health(
    connection: sqlite3.Connection,
    *,
    status: str,
    now: datetime,
    report: PublisherCycleReport | None = None,
    error: str | None = None,
) -> None:
    details: dict[str, Any] = {
        "publisher_version": PUBLISHER_VERSION,
    }
    if report is not None:
        details.update(
            {
                "deployment_key": report.deployment_key,
                "candidates": report.candidates,
                "inserted": report.inserted,
                "unchanged": report.unchanged,
                "skipped": report.skipped,
                "outcomes_inserted": report.outcomes_inserted,
            }
        )
    record_health(
        connection,
        PUBLISHER_COMPONENT,
        status,
        heartbeat_at=now,
        success_at=now if status == "healthy" else None,
        error_at=now if error else None,
        error=error,
        details=details,
    )


def run_publisher(
    database: Path,
    *,
    once: bool,
    interval_seconds: float,
    rebuild_artifacts: bool,
) -> int:
    now = datetime.now(timezone.utc)
    with LiveBettingStore(database) as store:
        deployment = None if rebuild_artifacts else load_latest_frozen_deployment(
            store.connection
        )
        if deployment is None:
            _record_cycle_health(store.connection, status="starting", now=now)
    if deployment is None:
        deployment = _build_and_persist(database, now)
    history = _load_history(database)
    while True:
        cycle_at = datetime.now(timezone.utc)
        try:
            with LiveBettingStore(database) as store:
                report = publish_cycle(
                    store.connection,
                    deployment=deployment,
                    history=history,
                    now=cycle_at,
                )
                refreshed = build_prospective_calibration_deployment(
                    store.connection,
                    deployment,
                )
                if refreshed is not None:
                    persist_frozen_deployment(
                        store.connection,
                        refreshed,
                        created_at=cycle_at,
                    )
                _record_cycle_health(
                    store.connection,
                    status="healthy",
                    now=cycle_at,
                    report=report,
                )
            if refreshed is not None:
                deployment = refreshed
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "deployment_key": report.deployment_key,
                        "candidates": report.candidates,
                        "inserted": report.inserted,
                        "unchanged": report.unchanged,
                        "skipped": report.skipped,
                        "outcomes_inserted": report.outcomes_inserted,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        except Exception as error:
            message = f"{type(error).__name__}: {' '.join(str(error).split())[:500]}"
            with LiveBettingStore(database) as store:
                _record_cycle_health(
                    store.connection,
                    status="degraded",
                    now=cycle_at,
                    error=message,
                )
            if once:
                raise
        if once:
            return 0
        time.sleep(interval_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--rebuild-artifacts", action="store_true")
    parser.add_argument("--schema-prepared", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0.0:
        parser.error("--interval must be positive")
    if args.schema_prepared:
        verify_prepared_database(args.database)
    else:
        with LiveBettingStore(args.database) as store:
            store.init_schema()
    return run_publisher(
        args.database.resolve(),
        once=args.once,
        interval_seconds=float(args.interval),
        rebuild_artifacts=bool(args.rebuild_artifacts),
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DraftAnchor",
    "PublicationResult",
    "PublisherCycleReport",
    "append_prospective_outcomes",
    "build_prospective_calibration_deployment",
    "load_latest_frozen_deployment",
    "persist_frozen_deployment",
    "publish_anchor_curve",
    "publish_cycle",
    "ready_draft_anchors",
]
