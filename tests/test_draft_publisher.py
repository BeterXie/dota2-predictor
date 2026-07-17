from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from event_intelligence.backtest import HORIZONS, draft_dependency_fingerprint
from event_intelligence.deployment import FrozenDraftDeployment
from event_intelligence.draft_artifacts import (
    CalibrationSample,
    build_calibration_artifact,
    canonical_hash,
    canonical_json_bytes,
)
from event_intelligence.draft_features import (
    FEATURE_SCHEMA,
    FEATURE_SCHEMA_HASH,
    FEATURE_VERSION,
    PURE_FEATURE_SCHEMA,
)
from event_intelligence.draft_model import (
    DraftTrainingRow,
    FeatureSchema,
    fit_draft_model,
    predict_draft,
)
from live_betting.database_protocol import prepare_database
from live_betting.draft_publisher import (
    _deployment_identity,
    build_prospective_calibration_deployment,
    load_latest_frozen_deployment,
    persist_frozen_deployment,
    publish_anchor_curve,
    publish_cycle,
    ready_draft_anchors,
)
from live_betting.profiles.draft_curve import build_draft_curve
from live_betting.storage import LiveBettingStore
from live_betting.vision import VisionObservation
from shared.sqlite import connect


UTC = timezone.utc
CUTOFF = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)


def _deployment(database: Path) -> FrozenDraftDeployment:
    rows = tuple(
        DraftTrainingRow(
            match_id=index + 1,
            input_snapshot_hash=f"{index + 1:064x}",
            cutoff=CUTOFF - timedelta(days=60 - index),
            completed_at=CUTOFF - timedelta(days=59 - index),
            result_usable_at=CUTOFF - timedelta(days=58 - index),
            outcome=index % 2,
            duration_minutes=60.0,
            series_id=f"series-{index // 2}",
            features={
                name: (
                    (-1.0 if index % 2 == 0 else 1.0)
                    if name == "hero_win_rate_diff"
                    else 0.0
                )
                for name in PURE_FEATURE_SCHEMA
            },
        )
        for index in range(40)
    )
    schema = FeatureSchema.from_names(PURE_FEATURE_SCHEMA)
    models = tuple(
        fit_draft_model(rows, schema, CUTOFF, horizon, model_kind="pure_draft")
        for horizon in HORIZONS
    )
    calibrations = tuple(
        build_calibration_artifact(
            model,
            evidence_mode="reconstructed_walk_forward",
            source_ref="strict-draft-walk-forward-v1:test",
            fit_samples=(),
            evaluation_samples=(),
        )
        for model in models
    )
    connection = connect(database, read_only=True, row_factory=sqlite3.Row)
    try:
        fingerprint = draft_dependency_fingerprint(connection)
        revision = int(
            connection.execute(
                "SELECT dependency_revision FROM draft_lineage_revisions"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    identity = _deployment_identity(
        training_cutoff=CUTOFF,
        dependency_fingerprint=fingerprint,
        dependency_revision=revision,
        models=models,
        calibrations=calibrations,
        evidence_mode="reconstructed_walk_forward",
    )
    return FrozenDraftDeployment(
        deployment_key=canonical_hash(identity),
        training_cutoff=CUTOFF,
        dependency_fingerprint=fingerprint,
        dependency_revision=revision,
        models=models,
        calibrations=calibrations,
    )


@pytest.fixture
def prepared_database(tmp_path: Path) -> Path:
    database = tmp_path / "publisher.db"
    prepare_database(database, tmp_path / "backups")
    return database


def _strict_result():
    mapping = SimpleNamespace(
        mapping_id=7,
        event_id="event-1",
        canonical_team_one_id=101,
        canonical_team_two_id=202,
    )
    return SimpleNamespace(eligible=True, reason="eligible", mapping=mapping)


def _insert_anchor(store: LiveBettingStore, *, captured_at: datetime = CUTOFF) -> None:
    assert store.insert_vision_observation(
        VisionObservation(
            raybet_match_id="match-1",
            map_number=1,
            captured_at=captured_at,
            game_clock_seconds=120,
            is_paused=False,
            radiant_hero_ids=(1, 2, 3, 4, 5),
            dire_hero_ids=(6, 7, 8, 9, 10),
            clock_confidence=0.99,
            draft_confidence=0.99,
            source_frame_ref="frame-1.jpg",
            screen_state="game",
            radiant_team_side="team_one",
        )
    )


def _insert_event(connection: sqlite3.Connection) -> None:
    connection.execute(
        """INSERT INTO event_registry
           (event_id, canonical_name, tier, prize_pool_usd,
            main_event_start_at, main_event_end_at, opendota_league_id,
            secondary_provider_ids_json, official_evidence_urls_json,
            evidence_status, scope_policy_version, scope, approval_status,
            approved_by, approved_at, reconciliation_status,
            expected_map_count, observed_map_count, public_map_count,
            reconciliation_note, included_stages_json,
            excluded_categories_json, include_internal_lcq,
            excludes_qualifiers, excludes_division_2, excludes_exhibitions,
            excludes_forfeits, excludes_void_remakes, created_at, updated_at)
           VALUES ('event-1', 'Event 1', 'tier_1', 1000000, ?, ?, 999,
                   '{}', '["https://example.invalid/event"]',
                   'manually_audited', 'scope-v1', 'formal_main_event',
                   'approved', 'tester', ?, 'not_required', NULL, NULL, NULL,
                   NULL, '["main_event"]', '[]', 0, 1, 1, 1, 1, 1, ?, ?)""",
        (
            (CUTOFF - timedelta(days=1)).isoformat(),
            (CUTOFF + timedelta(days=1)).isoformat(),
            (CUTOFF - timedelta(days=2)).isoformat(),
            (CUTOFF - timedelta(days=2)).isoformat(),
            (CUTOFF - timedelta(days=2)).isoformat(),
        ),
    )


def _insert_mapping(
    connection: sqlite3.Connection,
    *,
    mapping_id: int,
    match_id: str,
    observed_at: datetime,
) -> None:
    identity_json = "{}"
    identity_hash = hashlib.sha256(identity_json.encode()).hexdigest()
    connection.execute(
        """INSERT INTO strict_live_map_mappings
           (mapping_id, raybet_match_id, map_number, event_id,
            team_one_id, team_two_id, canonical_team_one_id,
            canonical_team_one_name, canonical_team_two_id,
            canonical_team_two_name, canonical_identity_json,
            canonical_identity_hash, crosswalk_evidence_json,
            crosswalk_evidence_hash, stage_scope, scheduled_at_utc,
            raybet_best_of, raybet_identity_json, raybet_identity_hash,
            raybet_metadata_updated_at, source, evidence_json, evidence_hash,
            mapping_version, acceptance_mode, automatic_approval_id,
            accepted_by, accepted_at, recorded_at, created_at)
           VALUES (?, ?, 1, 'event-1', 501, 502, 101, 'Alpha', 202, 'Beta',
                   ?, ?, ?, ?, 'main_event', ?, 1, ?, ?, ?, 'test', ?, ?,
                   'strict-live-map-v3', 'manual_exact', NULL, 'tester', ?, ?, ?)""",
        (
            mapping_id,
            match_id,
            identity_json,
            identity_hash,
            identity_json,
            identity_hash,
            observed_at.isoformat(),
            identity_json,
            identity_hash,
            (observed_at - timedelta(minutes=1)).isoformat(),
            identity_json,
            identity_hash,
            (observed_at - timedelta(minutes=1)).isoformat(),
            (observed_at - timedelta(minutes=1)).isoformat(),
            (observed_at - timedelta(minutes=1)).isoformat(),
        ),
    )


def _feature_snapshot_json(
    *,
    values: dict[str, float],
    observed_at: datetime,
    target_hash: str,
    match_id: int,
) -> str:
    payload = {
        "match_id": match_id,
        "prediction_cutoff": observed_at.isoformat(),
        "availability_mode": "prospective",
        "feature_version": FEATURE_VERSION,
        "feature_schema": list(FEATURE_SCHEMA),
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        "input_hash": target_hash,
        "pure_features": [
            {
                "name": name,
                "value": values[name],
                "support": 200,
                "evidence_ids": [],
                "coverage": 1.0,
                "missing_reason": None,
            }
            for name in PURE_FEATURE_SCHEMA
        ],
        "support": 200,
        "pure_coverage": 1.0,
        "evidence_ids": [],
    }
    return canonical_json_bytes(payload).decode()


def _insert_prospective_sample(
    connection: sqlite3.Connection,
    *,
    deployment: FrozenDraftDeployment,
    index: int,
    hero_edge: float,
    outcome: int,
) -> dict[int, CalibrationSample]:
    observed_at = CUTOFF + timedelta(minutes=index + 1)
    settled_at = observed_at + timedelta(hours=1)
    match_id = f"sample-{index:03d}"
    mapping_id = 1_000 + index
    dota_match_id = 9_000_000 + index
    curve_key = canonical_hash({"sample": index})
    target_hash = canonical_hash({"sample-target": index})
    radiant = [1, 2, 3, 4, 5]
    dire = [6, 7, 8, 9, 10]
    anchor_hash = hashlib.sha256(
        json.dumps(
            {"radiant": radiant, "dire": dire},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    values = {name: 0.0 for name in PURE_FEATURE_SCHEMA}
    values["hero_win_rate_diff"] = hero_edge
    _insert_mapping(
        connection,
        mapping_id=mapping_id,
        match_id=match_id,
        observed_at=observed_at,
    )
    connection.execute(
        """INSERT INTO vision_draft_anchors
           (raybet_match_id, map_number, draft_hash, radiant_hero_ids,
            dire_hero_ids, radiant_team_side, team_side_anchored_at,
            team_side_source_frame_ref, anchored_at, source_frame_ref,
            status, conflict_at)
           VALUES (?, 1, ?, ?, ?, 'team_one', ?, 'frame.jpg', ?, 'frame.jpg',
                   'anchored', NULL)""",
        (
            match_id,
            anchor_hash,
            json.dumps(radiant),
            json.dumps(dire),
            observed_at.isoformat(),
            observed_at.isoformat(),
        ),
    )
    connection.execute(
        """INSERT INTO prospective_draft_curves
           (curve_key, raybet_match_id, map_number, strict_mapping_id,
            lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
            prediction_cutoff, first_usable_at, availability_mode, created_at,
            radiant_team_side, anchor_draft_hash, anchor_source_frame_ref,
            anchor_anchored_at, deployment_key, target_snapshot_hash,
            feature_snapshot_json)
           VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, 'prospective', ?, 'team_one', ?,
                   'frame.jpg', ?, ?, ?, ?)""",
        (
            curve_key,
            match_id,
            mapping_id,
            canonical_hash({"dire": dire, "radiant": radiant}),
            json.dumps(radiant),
            json.dumps(dire),
            observed_at.isoformat(),
            observed_at.isoformat(),
            observed_at.isoformat(),
            anchor_hash,
            observed_at.isoformat(),
            deployment.deployment_key,
            target_hash,
            _feature_snapshot_json(
                values=values,
                observed_at=observed_at,
                target_hash=target_hash,
                match_id=(1 << 61) + index,
            ),
        ),
    )
    samples: dict[int, CalibrationSample] = {}
    for horizon in HORIZONS:
        model = deployment.model(horizon)
        calibration = deployment.calibration(horizon)
        prediction = predict_draft(model, values)
        assert prediction.probability is not None
        assert prediction.uncertainty is not None
        probability = calibration.apply(prediction.probability)
        connection.execute(
            """INSERT INTO prospective_draft_landmarks
               (landmark_key, curve_key, horizon_minutes, radiant_probability,
                scaling_edge, synergy_edge, quality, validation_status,
                support, calibration_ref, input_refs_json, uncertainty,
                validation_reason, feature_hash, model_hash, calibration_hash,
                global_calibration_passed, global_gate_ref, model_version,
                model_kind, availability_mode, input_snapshot_hash, created_at,
                raw_radiant_probability, deployment_key, model_input_hash,
                raw_uncertainty)
               VALUES (?, ?, ?, ?, 0.0, 0.0, 1.0, 'failed', 0, ?, '["test"]',
                       ?, 'prospective_evidence_required', ?, ?, ?, 0, ?, ?,
                       'pure_draft', 'prospective', ?, ?, ?, ?, ?, ?)""",
            (
                canonical_hash({"curve": curve_key, "horizon": horizon}),
                curve_key,
                horizon,
                probability,
                f"draft-calibration:{calibration.calibration_hash}",
                prediction.uncertainty,
                model.feature_schema_hash,
                model.model_hash,
                calibration.calibration_hash,
                f"draft-calibration:{calibration.calibration_hash}",
                model.model_version,
                target_hash,
                observed_at.isoformat(),
                prediction.probability,
                deployment.deployment_key,
                prediction.input_snapshot_hash,
                prediction.uncertainty,
            ),
        )
        samples[horizon] = CalibrationSample(
            sample_id=f"{curve_key}:{horizon}",
            probability=prediction.probability,
            outcome=outcome,
            observed_at=observed_at,
            settled_at=settled_at,
            cluster_id=f"raybet:{match_id}",
            event_id="event-1",
        )
    winner = "team_one" if outcome else "team_two"
    raybet_ref = f"raybet:{index}"
    opendota_ref = f"opendota:{index}"
    connection.execute(
        """INSERT INTO map_results
           (raybet_match_id, map_number, dota_match_id, winner_side,
            team_one_kills, team_two_kills, duration_seconds, evidence_ref,
            settled_at)
           VALUES (?, 1, ?, ?, 30, 20, 2400, ?, ?)""",
        (match_id, dota_match_id, winner, opendota_ref, settled_at.isoformat()),
    )
    for source, evidence_ref in (("raybet", raybet_ref), ("opendota", opendota_ref)):
        connection.execute(
            """INSERT INTO settlement_result_evidence
               (raybet_match_id, map_number, dota_match_id, source, status,
                winner_side, evidence_ref, facts_json, observed_at)
               VALUES (?, 1, ?, ?, 'confirmed', ?, ?, '{}', ?)""",
            (
                match_id,
                dota_match_id,
                source,
                winner,
                evidence_ref,
                settled_at.isoformat(),
            ),
        )
    connection.execute(
        """INSERT INTO settlement_reconciliations
           (raybet_match_id, map_number, dota_match_id, raybet_winner_side,
            opendota_winner_side, raybet_evidence_ref, opendota_evidence_ref,
            status, reason, first_observed_at, updated_at)
           VALUES (?, 1, ?, ?, ?, ?, ?, 'confirmed', 'sources_agree', ?, ?)""",
        (
            match_id,
            dota_match_id,
            winner,
            winner,
            raybet_ref,
            opendota_ref,
            settled_at.isoformat(),
            settled_at.isoformat(),
        ),
    )
    connection.execute(
        """INSERT INTO prospective_draft_outcomes
           (curve_key, strict_mapping_id, dota_match_id, radiant_win,
            winner_side, evidence_ref, evidence_hash, settled_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            curve_key,
            mapping_id,
            dota_match_id,
            outcome,
            winner,
            opendota_ref,
            canonical_hash({"curve": curve_key, "outcome": outcome}),
            settled_at.isoformat(),
            settled_at.isoformat(),
        ),
    )
    return samples


def test_frozen_deployment_round_trip_is_complete_and_immutable(
    prepared_database: Path,
) -> None:
    deployment = _deployment(prepared_database)
    with LiveBettingStore(prepared_database) as store:
        assert persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        assert not persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=2),
        )
        loaded = load_latest_frozen_deployment(store.connection)
        assert loaded == deployment
        assert store.connection.execute(
            "SELECT COUNT(*) FROM draft_model_artifacts"
        ).fetchone()[0] == 5
        assert store.connection.execute(
            "SELECT COUNT(*) FROM draft_calibration_artifacts"
        ).fetchone()[0] == 5
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.connection.execute(
                "UPDATE draft_model_artifacts SET model_version='tampered'"
            )


def test_failed_reconstructed_gate_publishes_research_curve_but_no_order(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    monkeypatch.setattr(
        "live_betting.draft_publisher.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    monkeypatch.setattr(
        "live_betting.profiles.draft_curve.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _insert_anchor(store, captured_at=CUTOFF + timedelta(seconds=2))
        first = publish_cycle(
            store.connection,
            deployment=deployment,
            history=(),
            now=CUTOFF + timedelta(seconds=3),
        )
        second = publish_cycle(
            store.connection,
            deployment=deployment,
            history=(),
            now=CUTOFF + timedelta(seconds=4),
        )

        assert (first.inserted, first.unchanged, first.skipped) == (1, 0, 0)
        assert (second.inserted, second.unchanged, second.skipped) == (0, 1, 0)
        curve = store.connection.execute(
            "SELECT * FROM prospective_draft_curves"
        ).fetchone()
        assert curve["deployment_key"] == deployment.deployment_key
        assert curve["radiant_team_side"] == "team_one"
        landmarks = store.connection.execute(
            """SELECT horizon_minutes, validation_status,
                      global_calibration_passed,
                      raw_radiant_probability, radiant_probability,
                      validation_reason
                 FROM prospective_draft_landmarks
                ORDER BY horizon_minutes"""
        ).fetchall()
        assert [int(row[0]) for row in landmarks] == list(HORIZONS)
        assert all(row[1] == "failed" and int(row[2]) == 0 for row in landmarks)
        assert all(row[3] is not None and row[4] is not None for row in landmarks)
        assert all("prospective_evidence_required" in row[5] for row in landmarks)
        assert store.connection.execute(
            "SELECT COUNT(*) FROM shadow_orders"
        ).fetchone()[0] == 0
        loaded_curve = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(seconds=5)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert len(loaded_curve.points) == 5
        assert loaded_curve.at(10 * 60) is None
        assert (
            loaded_curve.unavailable_reason
            == "prospective_draft_calibration_gate_not_passed"
        )

        store.connection.execute(
            "DROP TRIGGER prospective_draft_landmarks_immutable_update"
        )
        store.connection.execute(
            """UPDATE prospective_draft_landmarks
                  SET raw_radiant_probability=raw_radiant_probability + 0.01
                WHERE horizon_minutes=10"""
        )
        corrupted = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(seconds=5)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert corrupted.unavailable_reason == "prospective_draft_artifact_invalid"


def test_conflicted_anchor_is_not_a_publication_candidate(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    monkeypatch.setattr(
        "live_betting.draft_publisher.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _insert_anchor(store, captured_at=CUTOFF + timedelta(seconds=2))
        store.insert_vision_observation(
            VisionObservation(
                "match-1",
                1,
                CUTOFF + timedelta(seconds=3),
                180,
                False,
                (1, 2, 3, 4, 11),
                (6, 7, 8, 9, 10),
                0.99,
                0.99,
                "conflict.jpg",
                "game",
                "team_one",
            )
        )

        report = publish_cycle(
            store.connection,
            deployment=deployment,
            history=(),
            now=CUTOFF + timedelta(seconds=4),
        )

        assert report.candidates == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM prospective_draft_curves"
        ).fetchone()[0] == 0


def test_prospective_outcome_refresh_remains_failed_below_support(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _deployment(prepared_database)

    def one_sample(*args, horizon_minutes: int, **kwargs):
        return (
            CalibrationSample(
                sample_id=f"{'a' * 64}:{horizon_minutes}",
                probability=0.55,
                outcome=1,
                observed_at=CUTOFF + timedelta(minutes=1),
                settled_at=CUTOFF + timedelta(hours=1),
                cluster_id="raybet:match-1",
                event_id="event-1",
            ),
        )

    monkeypatch.setattr(
        "live_betting.draft_publisher._prospective_calibration_samples",
        one_sample,
    )
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            current,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        refreshed = build_prospective_calibration_deployment(
            store.connection,
            current,
        )
        assert refreshed is not None
        assert refreshed.evidence_mode == "prospective"
        assert all(row.support == 1 for row in refreshed.calibrations)
        assert all(not row.passes_live_gate for row in refreshed.calibrations)
        assert persist_frozen_deployment(
            store.connection,
            refreshed,
            created_at=CUTOFF + timedelta(hours=2),
        )
        assert load_latest_frozen_deployment(store.connection) == refreshed


def test_landmark_insert_without_authoritative_artifacts_is_rejected(
    prepared_database: Path,
) -> None:
    with LiveBettingStore(prepared_database) as store:
        with pytest.raises(sqlite3.IntegrityError, match="authority"):
            store.connection.execute(
                """INSERT INTO prospective_draft_curves
                   (curve_key, raybet_match_id, map_number, strict_mapping_id,
                    lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
                    prediction_cutoff, first_usable_at, availability_mode,
                    created_at, radiant_team_side, anchor_draft_hash,
                    anchor_source_frame_ref, anchor_anchored_at, deployment_key,
                    target_snapshot_hash)
                   VALUES (?, 'match-1', 1, 7, ?, '[1,2,3,4,5]',
                           '[6,7,8,9,10]', ?, ?, 'prospective', ?, 'team_one',
                           ?, 'frame.jpg', ?, ?, ?)""",
                (
                    "a" * 64,
                    "b" * 64,
                    CUTOFF.isoformat(),
                    CUTOFF.isoformat(),
                    CUTOFF.isoformat(),
                    "c" * 64,
                    CUTOFF.isoformat(),
                    "d" * 64,
                    "e" * 64,
                ),
            )


def test_authoritative_prospective_artifact_can_enter_decision_gate(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _deployment(prepared_database)
    samples = {horizon: [] for horizon in HORIZONS}
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            current,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _insert_event(store.connection)
        values = {name: 0.0 for name in PURE_FEATURE_SCHEMA}
        for group, edge in enumerate((-2.0, -1.0, 0.0, 1.0, 2.0)):
            values["hero_win_rate_diff"] = edge
            probability = predict_draft(current.model(10), values).probability
            assert probability is not None
            wins = round(probability * 20)
            for offset in range(20):
                index = group * 20 + offset
                inserted = _insert_prospective_sample(
                    store.connection,
                    deployment=current,
                    index=index,
                    hero_edge=edge,
                    outcome=int(offset < wins),
                )
                for horizon, sample in inserted.items():
                    samples[horizon].append(sample)
        store.connection.commit()

        calibrations = tuple(
            build_calibration_artifact(
                current.model(horizon),
                evidence_mode="prospective",
                source_ref="prospective-draft-outcomes-v1",
                fit_samples=(),
                evaluation_samples=samples[horizon],
            )
            for horizon in HORIZONS
        )
        assert all(row.passes_live_gate for row in calibrations)
        fingerprint = draft_dependency_fingerprint(store.connection)
        revision = int(
            store.connection.execute(
                "SELECT dependency_revision FROM draft_lineage_revisions"
            ).fetchone()[0]
        )
        identity = _deployment_identity(
            training_cutoff=current.training_cutoff,
            dependency_fingerprint=fingerprint,
            dependency_revision=revision,
            models=current.models,
            calibrations=calibrations,
            evidence_mode="prospective",
        )
        prospective = FrozenDraftDeployment(
            deployment_key=canonical_hash(identity),
            training_cutoff=current.training_cutoff,
            dependency_fingerprint=fingerprint,
            dependency_revision=revision,
            models=current.models,
            calibrations=calibrations,
        )
        persist_frozen_deployment(
            store.connection,
            prospective,
            created_at=CUTOFF + timedelta(hours=4, minutes=30),
        )
        _insert_anchor(store, captured_at=CUTOFF + timedelta(hours=5))

        def strict(connection, *, raybet_match_id, map_number, **kwargs):
            if raybet_match_id == "match-1":
                return _strict_result()
            row = connection.execute(
                """SELECT mapping_id FROM strict_live_map_mappings
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            ).fetchone()
            mapping = None if row is None else SimpleNamespace(mapping_id=int(row[0]))
            return SimpleNamespace(
                eligible=mapping is not None,
                reason="eligible" if mapping is not None else "missing",
                mapping=mapping,
            )

        monkeypatch.setattr(
            "live_betting.draft_publisher.query_strict_live_eligibility",
            strict,
        )
        monkeypatch.setattr(
            "live_betting.profiles.draft_curve.query_strict_live_eligibility",
            strict,
        )
        anchor = next(
            row
            for row in ready_draft_anchors(store.connection)
            if row.raybet_match_id == "match-1"
        )
        result = publish_anchor_curve(
            store.connection,
            anchor=anchor,
            deployment=prospective,
            history=(),
            published_at=CUTOFF + timedelta(hours=5, seconds=1),
        )
        assert result.status == "inserted"

        curve = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(hours=5, seconds=2)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert curve.unavailable_reason is None
        assert curve.at(10 * 60) is not None
