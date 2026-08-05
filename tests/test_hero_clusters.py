from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from event_intelligence.hero_clusters import (
    ClusterEvidenceMode,
    assign_hero_cluster,
    load_cluster_resource,
)


UTC = timezone.utc


def _assign(
    hero_id: int,
    role: str | None,
    lane: str | None,
    *,
    cutoff: datetime = datetime(2026, 8, 1, tzinfo=UTC),
    patch: str = "7.41b",
    role_confidence: float = 1.0,
    lane_confidence: float = 1.0,
    mode: ClusterEvidenceMode = ClusterEvidenceMode.PUBLISHED_STATIC,
):
    return assign_hero_cluster(
        load_cluster_resource(),
        hero_id=hero_id,
        expected_role=role,
        expected_lane=lane,
        role_confidence=role_confidence,
        lane_confidence=lane_confidence,
        prediction_cutoff=cutoff,
        patch=patch,
        evidence_mode=mode,
    )


def test_versioned_resource_contains_full_documented_mapping_and_pair_matrices() -> None:
    resource = load_cluster_resource()

    assert resource.resource_version == "noxville-clusters-7.41-v1"
    assert resource.source_hash == (
        "5e7ed32ef62deafd0c8c1d46be74ccde6216a06a445c1142100b7269db6d8e61"
    )
    assert len(resource.mappings) == 87
    assert len(resource.pair_statistics) == 34


def test_assignment_primary_key_includes_role_and_lane() -> None:
    assert _assign(65, "core", "off").cluster_id == "C5"
    assert _assign(65, "support", "off").cluster_id == "C7"
    assert _assign(99, "core", "off").cluster_id == "C3"
    assert _assign(99, "core", "safe").cluster_id == "C4"
    assert _assign(16, "core", "mid").cluster_id == "C4"
    assert _assign(16, "core", "off").cluster_id == "C8"


def test_assignment_backoff_is_explicit_and_low_role_confidence_fails_closed() -> None:
    role_backoff = _assign(90, "support", None, lane_confidence=0.0)
    rank_backoff = _assign(50, None, None, role_confidence=0.0, lane_confidence=0.0)
    low_confidence = _assign(50, "support", "safe", role_confidence=0.69)
    unknown = _assign(999, "core", "safe")

    assert (role_backoff.cluster_id, role_backoff.assignment_source) == (
        "C0",
        "hero_role_backoff",
    )
    assert (rank_backoff.cluster_id, rank_backoff.assignment_source) == (
        "C0",
        "rank_backoff",
    )
    assert low_confidence.cluster_id == "unavailable"
    assert low_confidence.missing_reason == "role_confidence_below_threshold"
    assert unknown.cluster_id == "unavailable"
    assert unknown.missing_reason == "unknown_hero"


def test_published_resource_obeys_first_usable_time_and_patch_scope() -> None:
    before_publication = _assign(
        50,
        "support",
        "safe",
        cutoff=datetime(2026, 7, 23, 10, 47, 30, tzinfo=UTC),
    )
    wrong_patch = _assign(50, "support", "safe", patch="7.42")

    assert before_publication.missing_reason == "cluster_resource_not_published_at_cutoff"
    assert wrong_patch.missing_reason == "cluster_patch_incompatible"


def test_reconstructed_mode_requires_a_strictly_prior_reconstruction() -> None:
    static = load_cluster_resource()
    reconstructed = replace(
        static,
        evidence_mode=ClusterEvidenceMode.RECONSTRUCTED_WALK_FORWARD,
        published_at=None,
        training_cutoff=datetime(2026, 7, 1, tzinfo=UTC),
    )
    target_cutoff = datetime(2026, 7, 2, tzinfo=UTC)
    available = assign_hero_cluster(
        reconstructed,
        hero_id=50,
        expected_role="support",
        expected_lane="safe",
        role_confidence=1.0,
        lane_confidence=1.0,
        prediction_cutoff=target_cutoff,
        patch="7.41",
        evidence_mode=ClusterEvidenceMode.RECONSTRUCTED_WALK_FORWARD,
    )
    noncausal = assign_hero_cluster(
        replace(reconstructed, training_cutoff=target_cutoff),
        hero_id=50,
        expected_role="support",
        expected_lane="safe",
        role_confidence=1.0,
        lane_confidence=1.0,
        prediction_cutoff=target_cutoff,
        patch="7.41",
        evidence_mode=ClusterEvidenceMode.RECONSTRUCTED_WALK_FORWARD,
    )

    assert available.cluster_id == "C0"
    assert noncausal.missing_reason == "cluster_reconstruction_not_prior_to_cutoff"
