from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from event_intelligence.backtest import (
    BACKTEST_VERSION,
    DRAFT_VALIDATION_VERSION,
)
from event_intelligence.deployment import DEPLOYMENT_VERSION
from event_intelligence.draft_features import (
    DRAFT_FEATURE_ARTIFACT_VERSION,
    FEATURE_VERSION,
    LEGACY_DRAFT_FEATURE_ARTIFACT_VERSION,
    AvailabilityMode,
)
from event_intelligence.draft_model import (
    FEATURE_SCHEMA_VERSION,
    LEGACY_MODEL_ARTIFACT_VERSION,
    MODEL_ARTIFACT_VERSION,
    MODEL_VERSION,
)
from event_intelligence.roles import (
    ASSIGNMENT_VERSION,
    PROSPECTIVE_ASSIGNMENT_VERSION,
    RECONSTRUCTED_ASSIGNMENT_VERSION,
)
from event_intelligence.draft_residual_features import (
    DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
    DRAFT_RESIDUAL_FEATURE_VERSION,
    DRAFT_RESIDUAL_MODEL_SCHEMA,
    DRAFT_RESIDUAL_PURE_SCHEMA,
    SHRINKAGE_STRENGTH,
)
from event_intelligence.prematch_features import (
    PREMATCH_CLUSTER_MODEL_SCHEMA,
    PREMATCH_FEATURE_SCHEMA_HASHES,
    PREMATCH_FEATURE_SCHEMAS,
    PREMATCH_FEATURE_VERSION,
    PREMATCH_MODEL_KINDS,
)
from event_intelligence.prematch_backtest import PREMATCH_BACKTEST_VERSION
from event_intelligence.prematch_calibration import (
    PREMATCH_CALIBRATION_ARTIFACT_SCHEMA,
    PREMATCH_CALIBRATION_VERSION,
)
from event_intelligence.prematch_model import (
    PREMATCH_MODEL_ARTIFACT_VERSION,
    PREMATCH_MODEL_VERSION,
)
from event_intelligence.prematch_storage import PREMATCH_VALIDATION_VERSION
from event_intelligence.rosh_features import (
    ROSH_UNAVAILABLE_AUTHORITY_SCHEMA,
    ROSH_FEATURE_SCHEMA,
    ROSH_FEATURE_VERSION,
    ROSH_MODEL_SCHEMA,
    ROSH_MODEL_SCHEMA_HASH,
)
from event_intelligence.team_rating import TEAM_RATING_VERSION
from event_intelligence.team_rating_artifacts import TEAM_RATING_ARTIFACT_VERSION
from event_intelligence.team_rating_backtest import (
    TEAM_RATING_BACKTEST_VERSION,
    TEAM_RATING_PARAMETER_GRID,
)
from live_betting.official_rosh_shadow_strategy import CANDIDATE_SCHEMA
from live_betting.rosh_evidence import EVIDENCE_SCHEMA
from live_betting.strategy_contract import (
    OFFICIAL_ROSH_DIRECTION_STRATEGY_VERSION,
    build_official_rosh_strategy_contract,
)
from prematch.stratz_official_profile import (
    ACTIVE_PROFILE_ID,
    SCORER_SOURCE_HASH,
    SERIALIZATION_VERSION,
    V2_FORMULA_VERSION,
    V2_PROFILE_ID,
    get_profile,
)


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_ROSH_SCORER = ROOT / "prematch" / "stratz_official_score.py"


def test_existing_draft_versions_remain_frozen() -> None:
    assert FEATURE_VERSION == "draft-features-v3"
    assert DRAFT_FEATURE_ARTIFACT_VERSION == "draft-feature-artifact-v2"
    assert LEGACY_DRAFT_FEATURE_ARTIFACT_VERSION == "draft-feature-artifact-v1"
    assert MODEL_VERSION == "draft-logistic-l2-v1"
    assert MODEL_ARTIFACT_VERSION == "draft-model-artifact-v2"
    assert LEGACY_MODEL_ARTIFACT_VERSION == "draft-model-artifact-v1"
    assert FEATURE_SCHEMA_VERSION == "draft-feature-schema-v1"
    assert BACKTEST_VERSION == "strict-draft-walk-forward-v1"
    assert DRAFT_VALIDATION_VERSION == "draft-input-lineage-v4"
    assert DEPLOYMENT_VERSION == "frozen-pure-draft-deployment-v2"


def test_team_rating_walk_forward_versions_and_grid_remain_frozen() -> None:
    assert TEAM_RATING_VERSION == "team-rating-elo-v1"
    assert TEAM_RATING_ARTIFACT_VERSION == "team-rating-artifact-v1"
    assert TEAM_RATING_BACKTEST_VERSION == "team-rating-walk-forward-v1"
    assert len(TEAM_RATING_PARAMETER_GRID) == 144


def test_draft_residual_version_schema_and_shrinkage_remain_frozen() -> None:
    assert DRAFT_RESIDUAL_FEATURE_VERSION == "draft-residual-features-v1"
    assert DRAFT_RESIDUAL_PURE_SCHEMA == (
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
    assert len(DRAFT_RESIDUAL_MODEL_SCHEMA) == 40
    assert len(DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH) == 64
    assert SHRINKAGE_STRENGTH == 10.0


def test_official_rosh_feature_and_model_schemas_remain_frozen() -> None:
    assert ROSH_FEATURE_VERSION == "official-rosh-features-v1"
    assert ROSH_FEATURE_SCHEMA == (
        "relative_advantage",
        "score_20",
        "score_30",
        "score_40",
        "score_50",
        "slope_20_40",
        "slope_30_50",
        "curve_min",
        "curve_max",
        "curve_range",
        "direction_flip_count",
        "position_min_support",
        "synergy_min_support",
        "rank_fallback_ratio",
        "coverage",
    )
    assert len(ROSH_MODEL_SCHEMA) == 30
    assert len(ROSH_MODEL_SCHEMA_HASH) == 64


def test_prematch_model_versions_kinds_and_schemas_remain_frozen() -> None:
    assert PREMATCH_FEATURE_VERSION == "prematch-features-v1"
    assert PREMATCH_MODEL_VERSION == "prematch-offset-logistic-l2-v1"
    assert PREMATCH_MODEL_ARTIFACT_VERSION == "prematch-model-artifact-v1"
    assert PREMATCH_MODEL_KINDS == (
        "team_only",
        "team_plus_draft",
        "team_plus_rosh",
        "team_plus_draft_rosh",
        "team_plus_draft_rosh_clusters",
    )
    assert PREMATCH_FEATURE_SCHEMAS["team_only"] == ()
    assert PREMATCH_FEATURE_SCHEMAS["team_plus_draft"] == (DRAFT_RESIDUAL_MODEL_SCHEMA)
    assert PREMATCH_FEATURE_SCHEMAS["team_plus_rosh"] == ROSH_MODEL_SCHEMA
    assert PREMATCH_FEATURE_SCHEMAS["team_plus_draft_rosh"] == (
        DRAFT_RESIDUAL_MODEL_SCHEMA + ROSH_MODEL_SCHEMA
    )
    assert PREMATCH_FEATURE_SCHEMAS["team_plus_draft_rosh_clusters"] == (
        DRAFT_RESIDUAL_MODEL_SCHEMA
        + ROSH_MODEL_SCHEMA
        + PREMATCH_CLUSTER_MODEL_SCHEMA
    )
    assert dict(PREMATCH_FEATURE_SCHEMA_HASHES) == {
        "team_only": (
            "7110eb0dcd7bd9e60f3d392e2abe6b20eaed9c9df4a7aa0ce8aec5923144c69f"
        ),
        "team_plus_draft": (
            "1230a670c93c1b794cab50bded1490a2385f74a302b751f494b70a2b23a68a48"
        ),
        "team_plus_rosh": (
            "93400f9c773f90740760767382e9be33897e3ae0f25c30828ce65d35d802209d"
        ),
        "team_plus_draft_rosh": (
            "581e42787406d3ce5b758d990e5db71d71e9c92e9d19baf85c70fdad85d9dedc"
        ),
        "team_plus_draft_rosh_clusters": (
            "51d4a5f530ff760e19ff700ccb0bbff50bc8238c1d4d9fd3701c4ea247c42295"
        ),
    }


def test_prematch_validation_versions_remain_frozen() -> None:
    assert PREMATCH_BACKTEST_VERSION == "prematch-walk-forward-v1"
    assert PREMATCH_CALIBRATION_VERSION == "prematch-platt-v1"
    assert PREMATCH_CALIBRATION_ARTIFACT_SCHEMA == (
        "prematch-calibration-artifact/v1"
    )
    assert PREMATCH_VALIDATION_VERSION == "prematch-input-lineage-v1"
    assert ROSH_UNAVAILABLE_AUTHORITY_SCHEMA == (
        "official-rosh-feature-unavailable-authority/v1"
    )


def test_official_rosh_scorer_identity_remains_frozen() -> None:
    profile = get_profile()

    assert ACTIVE_PROFILE_ID == V2_PROFILE_ID == "stratz-rosh-web-2026-07-28-v2"
    assert profile.formula_version == V2_FORMULA_VERSION
    assert V2_FORMULA_VERSION == "stratz-official-rosh/2026-07-28-v2"
    assert profile.serialization_version == SERIALIZATION_VERSION
    assert SERIALIZATION_VERSION == "rfc8785-jcs/v1"
    assert SCORER_SOURCE_HASH == (
        "c0f0ec77aa90468c4f741e133dac4a013ef8236ec6be3342a169adfbbe4d837c"
    )
    assert hashlib.sha256(OFFICIAL_ROSH_SCORER.read_bytes()).hexdigest() == (
        SCORER_SOURCE_HASH
    )


def test_reconstructed_and_prospective_contracts_remain_disjoint() -> None:
    assert {mode.value for mode in AvailabilityMode} == {
        "reconstructed_walk_forward",
        "prospective",
    }
    assert len(AvailabilityMode) == 2
    assert ASSIGNMENT_VERSION == "role-assignment-v1"
    assert RECONSTRUCTED_ASSIGNMENT_VERSION == (
        "role-assignment-v1-reconstructed-walk-forward"
    )
    assert PROSPECTIVE_ASSIGNMENT_VERSION == "role-assignment-v1-prospective"
    assert RECONSTRUCTED_ASSIGNMENT_VERSION != PROSPECTIVE_ASSIGNMENT_VERSION


def test_official_rosh_shadow_remains_non_executable_direction_evidence() -> None:
    contract = build_official_rosh_strategy_contract()
    policy = contract.policy_artifact

    assert EVIDENCE_SCHEMA == "rosh-direction-evidence/v1"
    assert CANDIDATE_SCHEMA == "official-rosh-shadow-candidate/v1"
    assert OFFICIAL_ROSH_DIRECTION_STRATEGY_VERSION == (
        "comeback-shadow-v6-official-rosh-direction"
    )
    assert policy["probability"]["rosh_score_to_probability"] is False
    assert policy["probability"]["calibrated_probability_when_unavailable"] is None
    assert policy["stake"]["rosh_magnitude_used"] is False
    assert policy["stake"]["stake_multiplier_when_calibration_unavailable"] is None
    assert policy["execution"]["paper_order_creation"] is False
    assert policy["execution"]["real_money_execution"] is False


def _legacy_scorer_imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(
                alias.name
                for alias in node.names
                if alias.name == "prematch.scorer"
                or alias.name.startswith("prematch.scorer.")
            )
        elif isinstance(node, ast.ImportFrom):
            if node.module == "prematch.scorer" or (
                node.module == "prematch"
                and any(alias.name == "scorer" for alias in node.names)
            ):
                imports.append(
                    node.module
                    if node.module == "prematch.scorer"
                    else "prematch.scorer"
                )
    return tuple(imports)


def test_event_intelligence_never_imports_legacy_prematch_scorer() -> None:
    offenders = {}
    for path in sorted((ROOT / "event_intelligence").glob("*.py")):
        imports = _legacy_scorer_imports(path)
        if imports:
            offenders[str(path.relative_to(ROOT))] = imports

    assert offenders == {}
