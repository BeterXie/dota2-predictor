from __future__ import annotations

from copy import deepcopy

from live_betting.milestone_revocation import (
    RECORD_SCHEMA,
    required_revoked_milestones,
)


def _digest(character: str) -> str:
    return character * 64


def signature(
    name: str,
    account: str,
    role: str,
    signed_at: str | None = "2026-07-02T02:00:00+00:00",
) -> dict[str, object]:
    return {
        "name": name,
        "account": account,
        "role": role,
        "signed_at": signed_at,
    }


def milestone_revocation_record(
    conflict_type: str = "settlement",
    *,
    sample_key: str = "sample-1",
) -> dict[str, object]:
    verifier = signature("Verifier Chen", "verifier-chen", "independent_verifier")
    return {
        "schema": RECORD_SCHEMA,
        "original_record": {
            "record_id": _digest("a"),
            "record_type": "acceptance",
            "milestone": "M2",
            "evaluation_result": "passed",
            "recorded_at": "2026-07-01T00:00:00+00:00",
        },
        "workspace_evidence": {
            "workspace_manifest_hash": _digest("b"),
            "evidence_manifest_hash": _digest("c"),
            "cohort_hash": _digest("d"),
            "report_hash": _digest("e"),
            "spec_hash": _digest("f"),
        },
        "conflict": {
            "type": conflict_type,
            "authority_evidence_refs": [f"authority:{conflict_type}:1"],
            "discovered_at": "2026-07-02T00:00:00+00:00",
            "effective_at": "2026-07-02T01:00:00+00:00",
        },
        "affected": {
            "decision_keys": ["decision-1"],
            "order_keys": ["order-1"],
            "settlement_keys": ["order-1"],
            "sample_keys": [sample_key],
            "sample_lineage": [
                {
                    "sample_key": sample_key,
                    "settlement_key": "order-1",
                    "order_key": "order-1",
                    "decision_key": "decision-1",
                }
            ],
        },
        "revoked_milestones": list(required_revoked_milestones(conflict_type)),
        "governance": {
            "initiator": signature(
                "Owner Li", "owner-li", "execution_owner", "2026-07-02T01:30:00+00:00"
            ),
            "independent_verifier": verifier,
            "approvers": [deepcopy(verifier)],
        },
        "disposition": {
            "status": "active",
            "reason": "authoritative conflict confirmed",
            "decided_at": "2026-07-02T02:00:00+00:00",
        },
    }
