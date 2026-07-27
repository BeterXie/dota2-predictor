#!/usr/bin/env python3
"""Fail-closed P3 candidate JSON validator.

This tool performs no network access, opens no SQLite database, and starts no
service. A successful result authorizes only P3 exit review, never P4.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HEX64 = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_COMMIT = "2237d5f120ded13eb4e393a0c6a4251b096085df"
EXPECTED_TREE = "84ecdfa62c5e465860495c231623d7ccef939619"
EXPECTED_STRATEGY = {
    "strategy_version": "comeback-shadow-v5-executable-contract",
    "evaluator_version": "comeback-shadow-canonical-evaluator-v2",
    "evaluator_hash": "c2d2f741e3b172b1fda1ca161619961e597070388d46d97848391b3f2f91ad24",
    "policy_hash": "6e0c8a278378ee4c070f5d11204ca23397f54c7b6b703b544adaf105a259d696",
    "serialization_version": "rfc8785-jcs-v1",
    "source_path": "live_betting/strategy_contract.py",
}


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def get(value: dict[str, Any], dotted: str) -> Any:
    cur: Any = value
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def is_hex64(value: Any) -> bool:
    return isinstance(value, str) and bool(HEX64.fullmatch(value))


def validate(candidate: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []

    def require_true(path: str, reason: str) -> None:
        if get(candidate, path) is not True:
            blockers.append(reason)

    if candidate.get("schema") != "dota2-p3-candidate-v1":
        blockers.append("candidate_schema_invalid")
    if registry.get("schema") != "dota2-p3-candidate-registry-v1":
        blockers.append("registry_schema_invalid")

    frozen = registry.get("frozen_identity", {})
    if frozen.get("current_commit") != EXPECTED_COMMIT:
        blockers.append("registry_current_commit_mismatch")
    if frozen.get("current_tree") != EXPECTED_TREE:
        blockers.append("registry_current_tree_mismatch")
    if registry.get("p2_dependency", {}).get("m1_path_readiness") != "ready":
        blockers.append("p2_m1_path_not_ready")

    target_mode = candidate.get("target_mode")
    if target_mode not in {"m1_only", "m1_and_m2"}:
        blockers.append("target_mode_invalid")
    if target_mode == "m1_and_m2" and registry.get("p2_dependency", {}).get("m2_readiness") != "ready":
        blockers.append("m2_target_forbidden_while_m2_blocked")

    require_true("event.tier1_approved", "tier1_event_not_approved")
    require_true("event.strict_event_registry_covered", "strict_event_registry_not_covered")
    require_true("identity.raybet_match_identity_ready", "raybet_match_identity_not_ready")
    require_true("identity.raybet_odds_identity_ready", "raybet_odds_identity_not_ready")
    require_true("identity.exact_match_team_map_mapping", "exact_match_team_map_mapping_missing")

    deployment_key = get(candidate, "draft.deployment_key")
    if not is_hex64(deployment_key):
        blockers.append("draft_deployment_key_invalid")
    require_true("draft.loadable", "draft_deployment_not_loadable")
    require_true("draft.lineage_complete", "draft_lineage_incomplete")

    require_true("model_inputs.rosh_lineup_score_ready", "rosh_lineup_score_not_ready")
    require_true("model_inputs.team_profiles_ready", "team_profiles_not_ready")
    require_true("model_inputs.player_profiles_ready", "player_profiles_not_ready")
    require_true("model_inputs.model_refs_ready", "model_refs_not_ready")

    require_true("provider.hls_exact_match_refresh_ready", "hls_exact_match_refresh_not_ready")
    if get(candidate, "provider.signed_url_persisted") is not False:
        blockers.append("signed_url_persistence_must_be_false")

    if get(candidate, "layout.layout_id") != "STANDARD_DOTA_HUD":
        blockers.append("unsupported_layout_without_expansion_authorization")
    require_true("layout.runtime_capability_healthy", "standard_dota_hud_runtime_not_healthy")
    for field, reason in [
        ("layout.positive_live_marker_evidence_sha256", "positive_live_marker_evidence_invalid"),
        ("layout.negative_replay_highlights_evidence_sha256", "negative_layout_evidence_invalid"),
        ("layout.fixed_real_frame_evidence_sha256", "fixed_real_frame_evidence_invalid"),
    ]:
        if not is_hex64(get(candidate, field)):
            blockers.append(reason)

    if candidate.get("strategy") != EXPECTED_STRATEGY:
        blockers.append("canonical_strategy_identity_mismatch")

    require_true("operations.paper_only", "paper_only_not_enforced")
    if get(candidate, "operations.browser_companion_configured") is not False:
        blockers.append("browser_companion_must_be_disabled")
    require_true("operations.operations_approved", "operations_candidate_approval_missing")
    require_true("operations.single_managed_writer_plan", "single_managed_writer_plan_missing")
    require_true("operations.database_raw_pair_frozen", "database_raw_pair_not_frozen")
    require_true("approval.p3_candidate_approved", "p3_candidate_not_approved")
    if get(candidate, "approval.p4_live_canary_approved") is not False:
        blockers.append("p4_approval_must_remain_false_in_p3_preflight")

    # Exact identifiers must exist; values may be redacted but cannot be absent.
    for path, reason in [
        ("candidate_id", "candidate_id_missing"),
        ("scheduled_start_utc", "scheduled_start_missing"),
        ("event.strict_event_id", "strict_event_id_missing"),
        ("event.series_id", "series_id_missing"),
        ("event.map_number", "map_number_missing"),
        ("identity.raybet_match_id", "raybet_match_id_missing"),
    ]:
        if get(candidate, path) in (None, "", []):
            blockers.append(reason)

    # Stable order and no duplicates makes results reproducible.
    blockers = sorted(set(blockers))
    status = "ready_for_p3_exit_review" if not blockers else "p3_preparation_incomplete"
    canonical = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema": "dota2-p3-candidate-preflight-result-v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        "candidate_sha256": hashlib.sha256(canonical).hexdigest(),
        "target_mode": target_mode,
        "blockers": blockers,
        "authorization": {
            "p3_exit_review": status == "ready_for_p3_exit_review",
            "p4_live_canary": False,
            "production_deployment": False,
            "production_database_mutation": False,
        },
        "safety": {
            "network_access_performed": False,
            "database_connections_performed": 0,
            "service_processes_started": 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(load_object(args.candidate), load_object(args.registry))
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["status"] == "ready_for_p3_exit_review" else 2


if __name__ == "__main__":
    raise SystemExit(main())
