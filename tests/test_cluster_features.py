from __future__ import annotations

from datetime import datetime, timezone

from event_intelligence.cluster_features import (
    CLUSTER_FEATURE_SCHEMA,
    CLUSTER_MODEL_FEATURE_SCHEMA,
    ClusterFeatureTarget,
    ClusterPlayer,
    build_cluster_feature_snapshot,
    project_cluster_features,
)
from event_intelligence.hero_clusters import ClusterEvidenceMode, load_cluster_resource


UTC = timezone.utc


def _player(hero_id: int, role: str, lane: str) -> ClusterPlayer:
    return ClusterPlayer(hero_id, role, lane, 1.0, 1.0)


def _target(
    *, cutoff: datetime = datetime(2026, 8, 1, tzinfo=UTC),
    mode: ClusterEvidenceMode = ClusterEvidenceMode.PUBLISHED_STATIC,
) -> ClusterFeatureTarget:
    return ClusterFeatureTarget(
        match_id=42,
        prediction_cutoff=cutoff,
        patch="7.41",
        evidence_mode=mode,
        radiant=(
            _player(70, "core", "safe"),
            _player(106, "core", "mid"),
            _player(96, "core", "off"),
            _player(50, "support", "safe"),
            _player(100, "support", "off"),
        ),
        dire=(
            _player(73, "core", "safe"),
            _player(39, "core", "mid"),
            _player(78, "core", "off"),
            _player(87, "support", "safe"),
            _player(123, "support", "off"),
        ),
    )


def test_snapshot_has_complete_schema_and_estimation_metadata() -> None:
    snapshot = build_cluster_feature_snapshot(_target(), load_cluster_resource())

    assert tuple(row.name for row in snapshot.features) == CLUSTER_FEATURE_SCHEMA
    assert snapshot.mapping_coverage == 1.0
    assert snapshot.feature("radiant_cluster_count_C1").value == 1.0
    assert snapshot.feature("dire_cluster_count_C1").value == 0.0
    assert snapshot.feature("cluster_count_diff_C1").value == 1.0
    assert all(row.effective_support >= 0 for row in snapshot.features)
    assert all(0.0 <= row.coverage <= 1.0 for row in snapshot.features)


def test_pair_values_are_shrunk_and_unpublished_relationships_back_off() -> None:
    snapshot = build_cluster_feature_snapshot(_target(), load_cluster_resource())
    support = snapshot.feature("support_cluster_pair_interaction")
    cross = snapshot.feature("cross_team_cluster_edge")

    assert support.value is not None
    assert support.support > 0
    assert support.missing_reason is None
    assert cross.value == 0.0
    assert cross.coverage == 0.0
    assert cross.missing_reason == "cross_team_cluster_statistics_unavailable"


def test_model_projection_keeps_value_support_coverage_and_missing_flag() -> None:
    snapshot = build_cluster_feature_snapshot(_target(), load_cluster_resource())
    projected = project_cluster_features(snapshot)

    assert tuple(projected) == CLUSTER_MODEL_FEATURE_SCHEMA
    assert projected["support_cluster_pair_interaction"] is not None
    assert projected["support_cluster_pair_interaction__log1p_support"] > 0
    assert projected["support_cluster_pair_interaction__coverage"] == 1.0
    assert projected["cross_team_cluster_edge__missing"] == 1.0


def test_static_resource_cannot_leak_into_reconstructed_walk_forward() -> None:
    snapshot = build_cluster_feature_snapshot(
        _target(mode=ClusterEvidenceMode.RECONSTRUCTED_WALK_FORWARD),
        load_cluster_resource(),
    )

    assert snapshot.mapping_coverage == 0.0
    assert snapshot.missing_reason == "cluster_evidence_unavailable"
    assert snapshot.feature("support_cluster_pair_interaction").value is None
    assert snapshot.feature("missing_cluster_count").value == 10.0


def test_prepublication_target_is_explicitly_unavailable_not_zero_filled() -> None:
    snapshot = build_cluster_feature_snapshot(
        _target(cutoff=datetime(2026, 7, 23, 10, 47, 30, tzinfo=UTC)),
        load_cluster_resource(),
    )

    assert snapshot.feature("core_pair_mean").value is None
    assert snapshot.feature("core_pair_mean").missing_reason == (
        "cluster_pair_assignments_unavailable"
    )
    assert snapshot.feature("radiant_cluster_count_C0").missing_reason == (
        "partial_cluster_assignment"
    )
