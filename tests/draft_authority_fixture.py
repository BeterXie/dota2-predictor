from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live_betting.draft_authority import DraftLandmarkAuthority
from live_betting.vision import VisionObservation
from live_betting.vision_frame_registry import (
    publish_vision_frame_bytes,
    register_vision_frame_artifact,
)


_VISION_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="dota2-test-vision-"
)
_VISION_ROOT = Path(_VISION_TEMPORARY_DIRECTORY.name)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def make_test_vision_observation(
    *,
    raybet_match_id: str,
    map_number: int,
    captured_at: datetime,
    game_clock_seconds: int = 600,
    radiant_hero_ids: tuple[int, ...] = (1, 2, 3, 4, 5),
    dire_hero_ids: tuple[int, ...] = (6, 7, 8, 9, 10),
    radiant_team_side: str | None = "team_one",
    clock_confidence: float = 0.95,
    draft_confidence: float = 0.95,
    label: str = "test-vision",
) -> VisionObservation:
    payload = json.dumps(
        {
            "label": label,
            "match": raybet_match_id,
            "map": map_number,
            "captured_at": captured_at.astimezone(timezone.utc).isoformat(),
            "radiant": list(radiant_hero_ids),
            "dire": list(dire_hero_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    receipt = publish_vision_frame_bytes(_VISION_ROOT, payload)
    return VisionObservation(
        raybet_match_id=raybet_match_id,
        map_number=map_number,
        captured_at=captured_at,
        game_clock_seconds=game_clock_seconds,
        is_paused=False,
        radiant_hero_ids=radiant_hero_ids,
        dire_hero_ids=dire_hero_ids,
        clock_confidence=clock_confidence,
        draft_confidence=draft_confidence,
        source_frame_ref=receipt.frame_ref,
        screen_state="game",
        radiant_team_side=radiant_team_side,
        source_frame_sha256=receipt.content_sha256,
        source_frame_bytes=receipt.byte_length,
        source_frame_path=str(receipt.storage_path),
    )


def seed_test_draft_authority(
    connection: sqlite3.Connection,
    *,
    raybet_match_id: str,
    map_number: int,
    strict_mapping_id: int,
    observed_at: datetime,
    radiant_hero_ids: tuple[int, ...] = (1, 2, 3, 4, 5),
    dire_hero_ids: tuple[int, ...] = (6, 7, 8, 9, 10),
    radiant_team_side: str = "team_one",
    horizon_minutes: int = 10,
    radiant_probability: float = 0.6,
    label: str = "test-draft-authority",
) -> DraftLandmarkAuthority:
    """Insert one exact immutable authority graph for protocol unit tests."""

    as_of = observed_at.astimezone(timezone.utc)
    model_created = as_of - timedelta(seconds=5)
    calibration_created = as_of - timedelta(seconds=4)
    deployment_created = as_of - timedelta(seconds=3)
    cutoff = as_of - timedelta(seconds=2)
    first_usable = as_of - timedelta(seconds=1)
    draft_payload = json.dumps(
        {
            "radiant": list(radiant_hero_ids),
            "dire": list(dire_hero_ids),
        },
        separators=(",", ":"),
    )
    anchor_draft_hash = hashlib.sha256(draft_payload.encode("utf-8")).hexdigest()
    anchor = connection.execute(
        """SELECT draft_hash, radiant_hero_ids, dire_hero_ids,
                  radiant_team_side, team_side_anchored_at,
                  team_side_source_frame_ref, anchored_at, source_frame_ref,
                  status
             FROM vision_draft_anchors
            WHERE raybet_match_id=? AND map_number=?""",
        (raybet_match_id, map_number),
    ).fetchone()
    if anchor is None:
        receipt = publish_vision_frame_bytes(
            _VISION_ROOT,
            (
                f"{label}:{raybet_match_id}:{map_number}:"
                f"{cutoff.isoformat()}:{draft_payload}"
            ).encode("utf-8"),
        )
        register_vision_frame_artifact(
            connection,
            receipt,
            registered_at=cutoff,
        )
        anchor_frame_ref = receipt.frame_ref
        connection.execute(
            """INSERT INTO vision_observations
               (raybet_match_id, map_number, captured_at, game_clock_seconds,
                is_paused, radiant_hero_ids, dire_hero_ids,
                radiant_team_side, clock_confidence, draft_confidence,
                source_frame_ref, source_frame_sha256, source_frame_bytes,
                screen_state, confirmed)
               VALUES (?, ?, ?, 590, 0, ?, ?, ?, 0.95, 0.95, ?, ?, ?,
                       'game', 1)""",
            (
                raybet_match_id,
                map_number,
                cutoff.isoformat(),
                json.dumps(list(radiant_hero_ids), separators=(",", ":")),
                json.dumps(list(dire_hero_ids), separators=(",", ":")),
                radiant_team_side,
                anchor_frame_ref,
                receipt.content_sha256,
                receipt.byte_length,
            ),
        )
        connection.execute(
            """INSERT INTO vision_draft_anchors
               (raybet_match_id, map_number, draft_hash, radiant_hero_ids,
                dire_hero_ids, radiant_team_side, team_side_anchored_at,
                team_side_source_frame_ref, anchored_at, source_frame_ref,
                status, conflict_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'anchored', NULL)""",
            (
                raybet_match_id,
                map_number,
                anchor_draft_hash,
                json.dumps(list(radiant_hero_ids), separators=(",", ":")),
                json.dumps(list(dire_hero_ids), separators=(",", ":")),
                radiant_team_side,
                cutoff.isoformat(),
                anchor_frame_ref,
                cutoff.isoformat(),
                anchor_frame_ref,
            ),
        )
        anchor = connection.execute(
            """SELECT draft_hash, radiant_hero_ids, dire_hero_ids,
                      radiant_team_side, team_side_anchored_at,
                      team_side_source_frame_ref, anchored_at, source_frame_ref,
                      status
                 FROM vision_draft_anchors
                WHERE raybet_match_id=? AND map_number=?""",
            (raybet_match_id, map_number),
        ).fetchone()
    if anchor is None or str(anchor[8]) != "anchored":
        raise RuntimeError("test draft anchor is unavailable")
    if (
        tuple(json.loads(str(anchor[1]))) != radiant_hero_ids
        or tuple(json.loads(str(anchor[2]))) != dire_hero_ids
        or str(anchor[3]) != radiant_team_side
        or str(anchor[0]) != anchor_draft_hash
    ):
        raise RuntimeError("test draft anchor identity conflicts")
    anchor_team_side_at = datetime.fromisoformat(str(anchor[4]))
    anchor_at = datetime.fromisoformat(str(anchor[6]))
    cutoff = max(cutoff, anchor_team_side_at, anchor_at)
    first_usable = max(first_usable, cutoff)
    curve_created = max(as_of, first_usable)
    horizons = (10, 20, 30, 40, 50)
    model_hashes = {
        horizon: _hash(f"{label}:model:{horizon}") for horizon in horizons
    }
    calibration_hashes = {
        horizon: _hash(f"{label}:calibration:{horizon}")
        for horizon in horizons
    }
    model_hash = model_hashes[horizon_minutes]
    calibration_hash = calibration_hashes[horizon_minutes]
    deployment_key = _hash(f"{label}:deployment")
    curve_key = _hash(
        f"{label}:curve:{raybet_match_id}:{map_number}:{strict_mapping_id}"
    )
    landmark_key = _hash(f"{curve_key}:{horizon_minutes}")
    feature_hash = _hash(f"{label}:feature")
    target_snapshot_hash = _hash(f"{label}:target")
    input_snapshot_hash = _hash(f"{label}:input")
    lineup_hash = _hash(
        json.dumps(
            {
                "radiant": list(radiant_hero_ids),
                "dire": list(dire_hero_ids),
            },
            separators=(",", ":"),
        )
    )
    dependency = int(
        connection.execute(
            """SELECT dependency_revision FROM draft_lineage_revisions
                WHERE singleton=1"""
        ).fetchone()[0]
    )
    model_version = "test-draft-model-v1"
    model_payload = json.dumps(
        {
            "artifact_version": "draft-model-artifact-v2",
            "support": 0,
            "training_corpus": [],
        },
        separators=(",", ":"),
    )
    deployment_exists = connection.execute(
        "SELECT 1 FROM draft_deployment_bundles WHERE deployment_key=?",
        (deployment_key,),
    ).fetchone()
    if deployment_exists is None:
        for horizon in horizons:
            connection.execute(
                """INSERT INTO draft_model_artifacts
                   (model_hash, model_version, model_kind, horizon_minutes,
                    training_cutoff, feature_schema_hash, training_input_hash,
                    artifact_json, created_at)
                   VALUES (?, ?, 'pure_draft', ?, ?, ?, ?, ?, ?)""",
                (
                    model_hashes[horizon],
                    model_version,
                    horizon,
                    model_created.isoformat(),
                    feature_hash,
                    _hash(f"{label}:training:{horizon}"),
                    model_payload,
                    model_created.isoformat(),
                ),
            )
            connection.execute(
                """INSERT INTO draft_calibration_artifacts
                   (calibration_hash, model_hash, calibration_version,
                    horizon_minutes, evidence_mode, support, artifact_json,
                    created_at)
                   VALUES (?, ?, 'test-calibration-v1', ?, 'prospective', 500,
                           '{}', ?)""",
                (
                    calibration_hashes[horizon],
                    model_hashes[horizon],
                    horizon,
                    calibration_created.isoformat(),
                ),
            )
        connection.execute(
            """INSERT INTO draft_deployment_bundles
               (deployment_key, model_hashes_json, calibration_hashes_json,
                training_cutoff, dependency_fingerprint, dependency_revision,
                evidence_mode, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'prospective', ?)""",
            (
                deployment_key,
                json.dumps(
                    {str(horizon): model_hashes[horizon] for horizon in horizons},
                    separators=(",", ":"),
                ),
                json.dumps(
                    {
                        str(horizon): calibration_hashes[horizon]
                        for horizon in horizons
                    },
                    separators=(",", ":"),
                ),
                model_created.isoformat(),
                _hash(f"{label}:dependency"),
                dependency,
                deployment_created.isoformat(),
            ),
        )
    connection.execute(
        """INSERT OR IGNORE INTO prospective_draft_curves
           (curve_key, raybet_match_id, map_number, strict_mapping_id,
            lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
            prediction_cutoff, first_usable_at, availability_mode, created_at,
            radiant_team_side, anchor_draft_hash, anchor_source_frame_ref,
            anchor_anchored_at, anchor_team_side_source_frame_ref,
            anchor_team_side_anchored_at, deployment_key,
            target_snapshot_hash, feature_snapshot_json,
            feature_dependency_fingerprint, feature_dependency_revision)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prospective', ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, '{}', ?, ?)""",
        (
            curve_key,
            raybet_match_id,
            map_number,
            strict_mapping_id,
            lineup_hash,
            json.dumps(list(radiant_hero_ids), separators=(",", ":")),
            json.dumps(list(dire_hero_ids), separators=(",", ":")),
            cutoff.isoformat(),
            first_usable.isoformat(),
            curve_created.isoformat(),
            radiant_team_side,
            str(anchor[0]),
            str(anchor[7]),
            str(anchor[6]),
            str(anchor[5]),
            str(anchor[4]),
            deployment_key,
            target_snapshot_hash,
            _hash(f"{label}:feature-dependency"),
            dependency,
        ),
    )
    connection.execute(
        """INSERT OR IGNORE INTO prospective_draft_landmarks
           (landmark_key, curve_key, horizon_minutes, radiant_probability,
            scaling_edge, synergy_edge, quality, validation_status, support,
            calibration_ref, input_refs_json, uncertainty, validation_reason,
            feature_hash, model_hash, calibration_hash,
            global_calibration_passed, global_gate_ref, model_version,
            model_kind, availability_mode, input_snapshot_hash, created_at,
            raw_radiant_probability, deployment_key, model_input_hash,
            raw_uncertainty)
           VALUES (?, ?, ?, ?, 0.0, 0.0, 0.8, 'passed', 500, ?, '["test"]',
                   0.02, NULL, ?, ?, ?, 1, ?, ?, 'pure_draft', 'prospective',
                   ?, ?, ?, ?, ?, 0.02)""",
        (
            landmark_key,
            curve_key,
            horizon_minutes,
            radiant_probability,
            f"draft-calibration:{calibration_hash}",
            feature_hash,
            model_hash,
            calibration_hash,
            f"draft-calibration:{calibration_hash}",
            model_version,
            input_snapshot_hash,
            first_usable.isoformat(),
            radiant_probability,
            deployment_key,
            input_snapshot_hash,
        ),
    )
    connection.commit()
    authority_revision, dependency_revision = connection.execute(
        """SELECT authority.authority_revision, lineage.dependency_revision
             FROM draft_authority_revisions AS authority
             JOIN draft_lineage_revisions AS lineage
               ON lineage.singleton=authority.singleton
            WHERE authority.singleton=1"""
    ).fetchone()
    return DraftLandmarkAuthority(
        curve_key=curve_key,
        source_ref=f"prospective-draft:{curve_key}",
        landmark_key=landmark_key,
        horizon_minutes=horizon_minutes,
        target="radiant_win",
        radiant_probability=radiant_probability,
        quality=0.8,
        uncertainty=0.02,
        support=500,
        radiant_team_side=radiant_team_side,
        strict_mapping_id=strict_mapping_id,
        deployment_key=deployment_key,
        target_snapshot_hash=target_snapshot_hash,
        feature_hash=feature_hash,
        model_hash=model_hash,
        calibration_hash=calibration_hash,
        model_version=model_version,
        global_gate_ref=f"draft-calibration:{calibration_hash}",
        input_snapshot_hash=input_snapshot_hash,
        authority_revision=int(authority_revision),
        dependency_revision=int(dependency_revision),
    )


__all__ = ["seed_test_draft_authority"]
