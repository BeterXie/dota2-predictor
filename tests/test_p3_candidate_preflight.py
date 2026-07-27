from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "p3_candidate_preflight.py"
SPEC = importlib.util.spec_from_file_location("p3_candidate_preflight", TOOL_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "docs" / "operations" / "betting-readiness-p3-candidate-registry-2026-07-27.json"
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "docs" / "operations" / "betting-readiness-p3-candidate-template-2026-07-27.json"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def ready_candidate():
    candidate = load(TEMPLATE_PATH)
    candidate.update({
        "candidate_id": "redacted-candidate-001",
        "registered_at_utc": "2026-07-27T00:00:00Z",
        "scheduled_start_utc": "2026-07-28T12:00:00Z",
        "blockers": [],
    })
    candidate["event"].update({
        "tier1_approved": True,
        "strict_event_registry_covered": True,
        "strict_event_id": "redacted-event-001",
        "series_id": "redacted-series-001",
        "map_number": 1,
    })
    candidate["identity"].update({
        "raybet_match_id": "redacted-raybet-match-001",
        "raybet_match_identity_ready": True,
        "raybet_odds_identity_ready": True,
        "exact_match_team_map_mapping": True,
        "team_identity_refs": ["redacted-team-a", "redacted-team-b"],
    })
    candidate["draft"].update({
        "deployment_key": "a" * 64,
        "loadable": True,
        "lineage_complete": True,
    })
    for key in candidate["model_inputs"]:
        candidate["model_inputs"][key] = True
    candidate["provider"].update({
        "hls_exact_match_refresh_ready": True,
        "signed_url_persisted": False,
    })
    candidate["layout"].update({
        "runtime_capability_healthy": True,
        "positive_live_marker_evidence_sha256": "b" * 64,
        "negative_replay_highlights_evidence_sha256": "c" * 64,
        "fixed_real_frame_evidence_sha256": "d" * 64,
    })
    candidate["operations"].update({
        "paper_only": True,
        "browser_companion_configured": False,
        "operations_approved": True,
        "single_managed_writer_plan": True,
        "database_raw_pair_frozen": True,
    })
    candidate["approval"].update({
        "p3_candidate_approved": True,
        "p4_live_canary_approved": False,
    })
    return candidate


def test_empty_template_is_not_ready():
    result = MODULE.validate(load(TEMPLATE_PATH), load(REGISTRY_PATH))
    assert result["status"] == "p3_preparation_incomplete"
    assert "candidate_id_missing" in result["blockers"]
    assert result["authorization"]["p4_live_canary"] is False


def test_complete_m1_candidate_is_ready_for_review_only():
    result = MODULE.validate(ready_candidate(), load(REGISTRY_PATH))
    assert result["status"] == "ready_for_p3_exit_review"
    assert result["blockers"] == []
    assert result["authorization"]["p3_exit_review"] is True
    assert result["authorization"]["p4_live_canary"] is False


def test_m2_target_fails_while_registry_is_blocked():
    candidate = ready_candidate()
    candidate["target_mode"] = "m1_and_m2"
    result = MODULE.validate(candidate, load(REGISTRY_PATH))
    assert "m2_target_forbidden_while_m2_blocked" in result["blockers"]


def test_strategy_drift_fails_closed():
    candidate = ready_candidate()
    candidate["strategy"] = copy.deepcopy(candidate["strategy"])
    candidate["strategy"]["policy_hash"] = "0" * 64
    result = MODULE.validate(candidate, load(REGISTRY_PATH))
    assert "canonical_strategy_identity_mismatch" in result["blockers"]


def test_p4_approval_cannot_be_smuggled_into_p3():
    candidate = ready_candidate()
    candidate["approval"]["p4_live_canary_approved"] = True
    result = MODULE.validate(candidate, load(REGISTRY_PATH))
    assert "p4_approval_must_remain_false_in_p3_preflight" in result["blockers"]
