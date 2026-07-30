"""Disabled legacy milestone revocation projection.

The former ledger bound approvals to one SQLite file identity. PostgreSQL has
no equivalent local-file authority, and the application has never configured
this optional ledger. Runtime consumers retain the existing not-configured
projection while legacy file-ledger operations fail explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import rfc8785


PAIR_BASELINE_SCHEMA = "dota2-milestone-revocation-pair-baseline-v1"
RECORD_SCHEMA = "dota2-milestone-revocation-record-v1"
_AFFECTED_FIELDS = (
    "decision_keys",
    "order_keys",
    "settlement_keys",
    "sample_keys",
)
_REVOKED_BY_CONFLICT = {
    "mapping": ("M1", "M2", "M3-C", "M3-E", "M4-C", "M4-E"),
    "vision": ("M1", "M2", "M3-C", "M3-E", "M4-C", "M4-E"),
    "draft": ("M1", "M2", "M3-C", "M3-E", "M4-C", "M4-E"),
    "source": ("M1", "M2", "M3-C", "M3-E", "M4-C", "M4-E"),
    "settlement": ("M3-C", "M3-E", "M4-C", "M4-E"),
}


@dataclass(frozen=True)
class MilestoneRevocationConfig:
    """Legacy configuration retained only for explicit rejection."""

    root: Path
    database_path: Path
    raw_root: Path
    expected_anchor: Mapping[str, object] | Path
    pair_manifest: bytes | Mapping[str, object] | Path
    expected_pair_manifest_hash: str
    expected_anchor_hash: str | None = None


class MilestoneRevocationIntegrityError(RuntimeError):
    """Raised when a removed SQLite-bound ledger is configured."""


def canonical_bytes(value: Any) -> bytes:
    return rfc8785.dumps(value)


def required_revoked_milestones(conflict_type: str) -> tuple[str, ...]:
    try:
        return _REVOKED_BY_CONFLICT[conflict_type]
    except KeyError as error:
        raise ValueError(f"unsupported revocation conflict type: {conflict_type}") from error


def not_configured_milestone_revocation_projection() -> dict[str, object]:
    return {
        "status": "not_configured",
        "governance_status": "active",
        "ledger_integrity": {"status": "not_configured"},
        "pair_identity": None,
        "records": [],
        "isolated_keys": {field: [] for field in _AFFECTED_FIELDS},
        "revoked_milestones": [],
        "review_required_milestones": [],
        "requires_new_cutoff_manifest_report_record": False,
    }


def load_milestone_revocation_projection(
    root: Path | None = None,
    *,
    database_path: Path | None = None,
    raw_root: Path | None = None,
    connection: Any | None = None,
    expected_anchor: Mapping[str, object] | Path | None = None,
    expected_anchor_hash: str | None = None,
    pair_manifest: bytes | Mapping[str, object] | Path | None = None,
    expected_pair_manifest_hash: str | None = None,
    config: MilestoneRevocationConfig | None = None,
) -> dict[str, object]:
    del connection
    configured = any(
        value is not None
        for value in (
            root,
            database_path,
            raw_root,
            expected_anchor,
            expected_anchor_hash,
            pair_manifest,
            expected_pair_manifest_hash,
            config,
        )
    )
    if configured:
        raise MilestoneRevocationIntegrityError(
            "SQLite-bound milestone revocation ledgers were retired by the "
            "PostgreSQL migration"
        )
    return not_configured_milestone_revocation_projection()


def _removed_operation(*_args: object, **_kwargs: object) -> dict[str, object]:
    raise MilestoneRevocationIntegrityError(
        "SQLite-bound milestone revocation ledgers were retired by the "
        "PostgreSQL migration"
    )


create_pair_baseline_manifest = _removed_operation
initialize_milestone_revocation_ledger = _removed_operation
append_milestone_revocation = _removed_operation


__all__ = [
    "MilestoneRevocationIntegrityError",
    "MilestoneRevocationConfig",
    "PAIR_BASELINE_SCHEMA",
    "RECORD_SCHEMA",
    "append_milestone_revocation",
    "canonical_bytes",
    "create_pair_baseline_manifest",
    "initialize_milestone_revocation_ledger",
    "load_milestone_revocation_projection",
    "not_configured_milestone_revocation_projection",
    "required_revoked_milestones",
]
