from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "p3_window_preflight.py"
SPEC = importlib.util.spec_from_file_location("p3_window_preflight", TOOL_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

WINDOW_PATH = ROOT / "docs" / "operations" / "betting-readiness-p3-ti2026-window-2026-07-27.json"
REGISTRY_PATH = ROOT / "docs" / "operations" / "betting-readiness-p3-candidate-registry-2026-07-27.json"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_ti2026_window_is_valid_but_not_a_candidate():
    result = MODULE.validate(load(WINDOW_PATH), load(REGISTRY_PATH))
    assert result["integrity_status"] == "valid_registered_watch_window"
    assert result["candidate_readiness"] == "blocked"
    assert result["authorization"]["continue_monitoring"] is True
    assert result["authorization"]["p3_exit_review"] is False
    assert result["authorization"]["p4_live_canary"] is False
    assert "raybet_match_identity_not_ready" in result["readiness_blockers"]


def test_monitoring_clock_cannot_start_early():
    window = load(WINDOW_PATH)
    window["monitoring_clock"]["eligible_to_start"] = True
    result = MODULE.validate(window, load(REGISTRY_PATH))
    assert "monitoring_clock_eligibility_claimed_too_early" in result["integrity_blockers"]


def test_p4_approval_cannot_be_smuggled_into_watch_window():
    window = load(WINDOW_PATH)
    window["approval"]["p4_live_canary_approved"] = True
    result = MODULE.validate(window, load(REGISTRY_PATH))
    assert "p4_approval_must_remain_false" in result["integrity_blockers"]


def test_registry_hash_mismatch_fails_closed():
    registry = load(REGISTRY_PATH)
    registry["watch_windows"][0]["window_sha256"] = "0" * 64
    result = MODULE.validate(load(WINDOW_PATH), registry)
    assert "registry_watch_window_hash_mismatch" in result["integrity_blockers"]


def test_declared_blockers_must_match_computed_readiness():
    window = copy.deepcopy(load(WINDOW_PATH))
    window["blockers"] = []
    result = MODULE.validate(window, load(REGISTRY_PATH))
    assert "declared_readiness_blockers_mismatch" in result["integrity_blockers"]
