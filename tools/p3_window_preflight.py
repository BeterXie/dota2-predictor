#!/usr/bin/env python3
"""Fail-closed validator for a P3 monitored Tier-1 window.

A valid watch window is not a P3 candidate. This tool performs no network
access, opens no database, and starts no service. It verifies that public event
facts are recorded without smuggling unverified provider/runtime claims into
P3 readiness or starting the ADR-0013 monitoring clock early.
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
EXPECTED_REGISTRY_SCHEMA = "dota2-p3-candidate-registry-v1"
EXPECTED_WINDOW_SCHEMA = "dota2-p3-monitored-window-v1"
EXPECTED_LAYOUT = "STANDARD_DOTA_HUD"


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


def canonical_sha256(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def readiness_blockers(window: dict[str, Any]) -> list[str]:
    blockers: list[str] = []

    def require_true(path: str, reason: str) -> None:
        if get(window, path) is not True:
            blockers.append(reason)

    require_true("event.strict_event_registry_covered", "strict_event_registry_not_covered")
    require_true("event.opendota_league_id_confirmed", "opendota_league_id_unconfirmed")
    require_true("schedule.exact_series_schedule_published", "exact_series_schedule_unpublished")
    require_true("schedule.exact_map_schedule_ready", "exact_map_schedule_unpublished")
    if (
        get(window, "schedule.publication_conflict_detected") is True
        and get(window, "schedule.official_schedule_reconciled") is not True
    ):
        blockers.append("official_schedule_conflict_unreconciled")
    require_true("identity.raybet_match_identity_ready", "raybet_match_identity_not_ready")
    require_true("identity.raybet_odds_identity_ready", "raybet_odds_identity_not_ready")
    require_true("identity.exact_match_team_map_mapping", "exact_match_team_map_mapping_missing")

    if not is_hex64(get(window, "draft.deployment_key")):
        blockers.append("draft_deployment_key_missing")
    require_true("draft.deployment_ready", "draft_deployment_not_ready")
    require_true("draft.loadable", "draft_deployment_not_loadable")
    require_true("draft.lineage_complete", "draft_lineage_incomplete")

    require_true("model_inputs.rosh_lineup_score_ready", "rosh_lineup_score_not_ready")
    require_true("model_inputs.team_profiles_ready", "team_profiles_not_ready")
    require_true("model_inputs.player_profiles_ready", "player_profiles_not_ready")
    require_true("model_inputs.model_refs_ready", "model_refs_not_ready")

    require_true("provider.broadcast_channel_announced", "broadcast_channel_unannounced")
    require_true("provider.hls_exact_match_refresh_ready", "hls_exact_match_refresh_not_ready")

    if get(window, "layout.preferred_layout_id") != EXPECTED_LAYOUT:
        blockers.append("preferred_layout_mismatch")
    require_true("layout.runtime_capability_healthy", "standard_dota_hud_runtime_not_healthy")
    for path, reason in [
        ("layout.positive_live_marker_evidence_sha256", "positive_live_marker_evidence_invalid"),
        ("layout.negative_replay_highlights_evidence_sha256", "negative_layout_evidence_invalid"),
        ("layout.fixed_real_frame_evidence_sha256", "fixed_real_frame_evidence_invalid"),
    ]:
        if not is_hex64(get(window, path)):
            blockers.append(reason)

    require_true("operations.operations_approved", "operations_candidate_approval_missing")
    require_true("operations.single_managed_writer_plan", "single_managed_writer_plan_missing")
    require_true("operations.database_raw_pair_frozen", "database_raw_pair_not_frozen")
    require_true("approval.p3_candidate_approved", "p3_candidate_not_approved")

    for path, reason in [
        ("event.strict_event_id", "strict_event_id_missing"),
        ("schedule.series_id", "series_id_missing"),
        ("schedule.map_number", "map_number_missing"),
        ("schedule.scheduled_start_utc", "scheduled_start_missing"),
        ("identity.raybet_match_id", "raybet_match_id_missing"),
    ]:
        if get(window, path) in (None, "", []):
            blockers.append(reason)

    return sorted(set(blockers))


def validate(window: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    integrity_blockers: list[str] = []
    window_sha = canonical_sha256(window)

    if window.get("schema") != EXPECTED_WINDOW_SCHEMA:
        integrity_blockers.append("window_schema_invalid")
    if registry.get("schema") != EXPECTED_REGISTRY_SCHEMA:
        integrity_blockers.append("registry_schema_invalid")
    if window.get("status") != "registered_watch_window_not_candidate":
        integrity_blockers.append("window_status_must_remain_watch_only")
    if window.get("target_mode") != "m1_only":
        integrity_blockers.append("watch_window_target_mode_must_be_m1_only")

    if get(window, "event.tier1_watch_evidence") is not True:
        integrity_blockers.append("tier1_watch_evidence_missing")
    prize_pool = get(window, "event.prize_pool_usd")
    if not isinstance(prize_pool, int) or isinstance(prize_pool, bool) or prize_pool < 1_000_000:
        integrity_blockers.append("tier1_prize_pool_threshold_not_proven")
    urls = window.get("source_evidence")
    if not isinstance(urls, list) or len(urls) < 2:
        integrity_blockers.append("source_evidence_incomplete")

    if get(window, "provider.signed_url_persisted") is not False:
        integrity_blockers.append("signed_url_persistence_must_be_false")
    if get(window, "operations.paper_only") is not True:
        integrity_blockers.append("paper_only_not_enforced")
    if get(window, "approval.p4_live_canary_approved") is not False:
        integrity_blockers.append("p4_approval_must_remain_false")
    if get(window, "monitoring_clock.backfill_allowed") is not False:
        integrity_blockers.append("monitoring_clock_backfill_must_be_false")

    schedule_conflict = get(window, "schedule.publication_conflict_detected") is True
    schedule_reconciled = get(window, "schedule.official_schedule_reconciled") is True
    if schedule_conflict and not schedule_reconciled:
        observed = get(window, "schedule.observed_opening_series")
        if not isinstance(observed, list) or not observed:
            integrity_blockers.append("schedule_conflict_evidence_missing")
        if get(window, "schedule.exact_series_schedule_published") is True:
            integrity_blockers.append("exact_schedule_claimed_while_conflict_unreconciled")

    computed_readiness = readiness_blockers(window)
    declared = window.get("blockers")
    if declared != computed_readiness:
        integrity_blockers.append("declared_readiness_blockers_mismatch")

    clock_prerequisite_paths = (
        "event.strict_event_registry_covered",
        "event.opendota_league_id_confirmed",
        "schedule.exact_series_schedule_published",
        "schedule.exact_map_schedule_ready",
        "identity.raybet_match_identity_ready",
        "identity.raybet_odds_identity_ready",
        "identity.exact_match_team_map_mapping",
        "draft.deployment_ready",
        "draft.loadable",
        "draft.lineage_complete",
        "model_inputs.rosh_lineup_score_ready",
        "model_inputs.team_profiles_ready",
        "model_inputs.player_profiles_ready",
        "model_inputs.model_refs_ready",
        "layout.runtime_capability_healthy",
    )
    clock_prerequisites_ready = all(get(window, path) is True for path in clock_prerequisite_paths)
    clock_eligible = get(window, "monitoring_clock.eligible_to_start")
    clock_started = get(window, "monitoring_clock.started_at_utc")
    if clock_eligible is True and not clock_prerequisites_ready:
        integrity_blockers.append("monitoring_clock_eligibility_claimed_too_early")
    if clock_started not in (None, "") and not clock_prerequisites_ready:
        integrity_blockers.append("monitoring_clock_started_too_early")

    matching_entries = [
        entry
        for entry in registry.get("watch_windows", [])
        if isinstance(entry, dict) and entry.get("window_id") == window.get("window_id")
    ]
    if len(matching_entries) != 1:
        integrity_blockers.append("registry_watch_window_entry_missing_or_duplicate")
    else:
        entry = matching_entries[0]
        if entry.get("window_sha256") != window_sha:
            integrity_blockers.append("registry_watch_window_hash_mismatch")
        if entry.get("status") != window.get("status"):
            integrity_blockers.append("registry_watch_window_status_mismatch")
        if entry.get("candidate_ready") is not False:
            integrity_blockers.append("registry_must_not_mark_watch_window_candidate_ready")
        if entry.get("monitoring_clock_eligible") is not False:
            integrity_blockers.append("registry_must_not_start_clock_for_blocked_window")

    if get(registry, "gates.p3_exit") != "not_achieved":
        integrity_blockers.append("registry_p3_exit_must_remain_not_achieved")
    if get(registry, "gates.p4_live_canary") != "no_go":
        integrity_blockers.append("registry_p4_must_remain_no_go")

    integrity_blockers = sorted(set(integrity_blockers))
    integrity_status = "valid_registered_watch_window" if not integrity_blockers else "invalid_watch_window"
    candidate_status = "ready" if not computed_readiness else "blocked"

    return {
        "schema": "dota2-p3-window-preflight-result-v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "integrity_status": integrity_status,
        "candidate_readiness": candidate_status,
        "window_id": window.get("window_id"),
        "window_sha256": window_sha,
        "integrity_blockers": integrity_blockers,
        "readiness_blockers": computed_readiness,
        "monitoring_clock": {
            "prerequisites_ready": clock_prerequisites_ready,
            "authorized_to_start": clock_prerequisites_ready and not integrity_blockers,
            "started_at_utc": clock_started,
        },
        "authorization": {
            "continue_monitoring": integrity_status == "valid_registered_watch_window",
            "p3_exit_review": candidate_status == "ready" and not integrity_blockers,
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
    parser.add_argument("--window", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(load_object(args.window), load_object(args.registry))
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["integrity_status"] == "valid_registered_watch_window" else 2


if __name__ == "__main__":
    raise SystemExit(main())
