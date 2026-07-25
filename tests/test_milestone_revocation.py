from __future__ import annotations

import json
import hashlib
import sqlite3
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from live_betting.milestone_revocation import (
    MilestoneRevocationIntegrityError,
    MilestoneRevocationConfig,
    append_milestone_revocation,
    canonical_bytes,
    create_pair_baseline_manifest,
    initialize_milestone_revocation_ledger,
    load_milestone_revocation_projection,
)
from tests.milestone_revocation_fixture import (
    _digest,
    milestone_revocation_record as _record,
    signature as _signature,
)


@dataclass
class _LedgerFixture:
    config: MilestoneRevocationConfig

    def __iter__(self):
        yield self.config.root
        yield self.config.database_path
        yield self.config.raw_root


def _append(paths: _LedgerFixture, record: dict[str, object]) -> dict[str, object]:
    config = paths.config
    anchor = append_milestone_revocation(
        config.root,
        record,
        database_path=config.database_path,
        raw_root=config.raw_root,
        expected_anchor=config.expected_anchor,
        pair_manifest=config.pair_manifest,
        expected_pair_manifest_hash=config.expected_pair_manifest_hash,
    )
    paths.config = replace(config, expected_anchor=anchor)
    return anchor


@pytest.fixture
def paired_ledger(tmp_path: Path) -> _LedgerFixture:
    database = tmp_path / "dota2.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE strategy_decisions (decision_key TEXT PRIMARY KEY);
        CREATE TABLE shadow_orders (order_key TEXT PRIMARY KEY);
        CREATE TABLE settlements (order_key TEXT PRIMARY KEY);
        CREATE TABLE shadow_order_decision_lineage (
            order_key TEXT PRIMARY KEY,
            decision_key TEXT NOT NULL
        );
        INSERT INTO strategy_decisions VALUES ('decision-1');
        INSERT INTO shadow_orders VALUES ('order-1');
        INSERT INTO settlements VALUES ('order-1');
        INSERT INTO shadow_order_decision_lineage VALUES ('order-1', 'decision-1');
        """
    )
    connection.commit()
    connection.close()
    raw_root = tmp_path / "raw-v2"
    raw_root.mkdir()
    ledger = tmp_path / "revocations"
    manifest = create_pair_baseline_manifest(
        database, raw_root, p0_baseline_evidence_identity="9" * 64
    )
    manifest_hash = hashlib.sha256(manifest).hexdigest()
    anchor = initialize_milestone_revocation_ledger(
        ledger,
        database_path=database,
        raw_root=raw_root,
        pair_manifest=manifest,
        expected_pair_manifest_hash=manifest_hash,
        p0_baseline_evidence_identity="9" * 64,
    )
    return _LedgerFixture(
        MilestoneRevocationConfig(
            root=ledger,
            database_path=database,
            raw_root=raw_root,
            expected_anchor=anchor,
            pair_manifest=manifest,
            expected_pair_manifest_hash=manifest_hash,
        )
    )


def _projection(paths: _LedgerFixture) -> dict[str, object]:
    return load_milestone_revocation_projection(config=paths.config)


def test_settlement_conflict_appends_without_mutating_passed_m2(
    paired_ledger: _LedgerFixture,
) -> None:
    ledger, database, raw_root = paired_ledger
    index_prefix = (ledger / "index.jsonl").read_bytes()

    _append(paired_ledger, _record())
    projection = _projection(paired_ledger)

    assert (ledger / "index.jsonl").read_bytes().startswith(index_prefix)
    assert projection["ledger_integrity"]["status"] == "verified"
    assert projection["ledger_integrity"]["revocation_record_count"] == 1
    [projected] = projection["records"]
    assert projected["record_id"] == json.loads(
        (ledger / "index.jsonl").read_bytes().splitlines()[1]
    )["object_hash"]
    assert projected["original_record"]["record_id"] == _digest("a")
    assert projected["evaluation_result"] == "passed"
    assert projected["governance_status"] == "revoked"
    assert projected["revocation_status"] == "active"
    assert projection["isolated_keys"] == {
        "decision_keys": ["decision-1"],
        "order_keys": ["order-1"],
        "settlement_keys": ["order-1"],
        "sample_keys": ["sample-1"],
    }
    assert projection["revoked_milestones"] == [
        "M2",
        "M3-C",
        "M3-E",
        "M4-C",
        "M4-E",
    ]
    assert projection["requires_new_cutoff_manifest_report_record"] is True


@pytest.mark.parametrize("conflict_type", ["mapping", "vision", "draft", "source"])
def test_pre_settlement_authority_conflicts_revoke_full_dependency_closure(
    paired_ledger: _LedgerFixture,
    conflict_type: str,
) -> None:
    ledger, database, raw_root = paired_ledger

    _append(paired_ledger, _record(conflict_type))

    assert _projection(paired_ledger)["revoked_milestones"] == [
        "M1",
        "M2",
        "M3-C",
        "M3-E",
        "M4-C",
        "M4-E",
    ]


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("unnamed_verifier", "independent_verifier_not_named"),
        ("same_person", "separation_of_duties_violated"),
        ("missing_timestamp", "independent_verifier_timestamp_missing_or_invalid"),
        ("analysis_author_only", "independent_verifier_approval_missing"),
    ],
)
def test_incomplete_or_non_independent_governance_cannot_activate_revocation(
    paired_ledger: _LedgerFixture,
    case: str,
    reason: str,
) -> None:
    ledger, database, raw_root = paired_ledger
    record = _record()
    governance = record["governance"]
    if case == "unnamed_verifier":
        governance["independent_verifier"]["name"] = ""
    elif case == "same_person":
        governance["independent_verifier"]["name"] = "Owner Li"
        governance["independent_verifier"]["account"] = "owner-li"
    elif case == "missing_timestamp":
        governance["independent_verifier"]["signed_at"] = None
    else:
        governance["approvers"] = [
            _signature("Analyst Wu", "analyst-wu", "m4_analysis_author")
        ]

    _append(paired_ledger, record)
    projection = _projection(paired_ledger)
    [projected] = projection["records"]

    assert projection["status"] == "review_required"
    assert projection["governance_status"] == "active"
    assert projection["revoked_milestones"] == []
    assert projected["revocation_status"] == "review_required"
    assert projected["governance_status"] == "review_required"
    assert reason in projected["review_reasons"]
    assert projection["isolated_keys"]["sample_keys"] == ["sample-1"]


def test_missing_required_binding_is_rejected_before_append(
    paired_ledger: _LedgerFixture,
) -> None:
    ledger, database, raw_root = paired_ledger
    record = _record()
    del record["workspace_evidence"]["report_hash"]

    with pytest.raises(
        MilestoneRevocationIntegrityError,
        match="workspace/evidence binding fields are malformed",
    ):
        _append(paired_ledger, record)

    assert _projection(paired_ledger)["ledger_integrity"]["entry_count"] == 1


def test_duplicate_conflict_is_rejected_without_growing_index(
    paired_ledger: _LedgerFixture,
) -> None:
    ledger, database, raw_root = paired_ledger
    record = _record()
    _append(paired_ledger, record)
    before = (ledger / "index.jsonl").read_bytes()

    with pytest.raises(
        MilestoneRevocationIntegrityError,
        match="duplicate revocation conflict is forbidden",
    ):
        _append(paired_ledger, deepcopy(record))

    assert (ledger / "index.jsonl").read_bytes() == before


def test_same_original_record_id_cannot_append_changed_evaluation_result(
    paired_ledger: _LedgerFixture,
) -> None:
    ledger, database, raw_root = paired_ledger
    _append(paired_ledger, _record())
    before = (ledger / "index.jsonl").read_bytes()
    changed = _record(sample_key="sample-2")
    changed["original_record"]["evaluation_result"] = "failed"

    with pytest.raises(
        MilestoneRevocationIntegrityError,
        match="original milestone record identity conflicts",
    ):
        _append(paired_ledger, changed)

    assert (ledger / "index.jsonl").read_bytes() == before


@pytest.mark.parametrize(
    "tamper",
    ["delete_object", "overwrite_object", "truncate_index", "modify_old_index"],
)
def test_verifier_rejects_deletion_overwrite_truncation_and_old_index_mutation(
    paired_ledger: _LedgerFixture,
    tamper: str,
) -> None:
    ledger, database, raw_root = paired_ledger
    _append(paired_ledger, _record())
    index_path = ledger / "index.jsonl"
    lines = index_path.read_bytes().splitlines(keepends=True)
    second_entry = json.loads(lines[1])
    object_path = ledger / "objects" / f"{second_entry['object_hash']}.json"
    if tamper == "delete_object":
        object_path.unlink()
    elif tamper == "overwrite_object":
        changed = _record()
        changed["disposition"]["reason"] = "silently changed"
        object_path.write_bytes(canonical_bytes(changed))
    elif tamper == "truncate_index":
        index_path.write_bytes(lines[0])
    else:
        first = json.loads(lines[0])
        first["sequence"] = 2
        lines[0] = canonical_bytes(first) + b"\n"
        index_path.write_bytes(b"".join(lines))

    with pytest.raises(MilestoneRevocationIntegrityError):
        _projection(paired_ledger)


def test_configured_ledger_rejects_database_or_raw_pair_substitution(
    paired_ledger: _LedgerFixture, tmp_path: Path
) -> None:
    ledger, database, raw_root = paired_ledger
    other_database = tmp_path / "other.db"
    sqlite3.connect(other_database).close()
    other_raw = tmp_path / "other-raw"
    other_raw.mkdir()

    with pytest.raises(
        MilestoneRevocationIntegrityError,
        match="database/raw pair identity mismatch",
    ):
        load_milestone_revocation_projection(
            ledger,
            database_path=other_database,
            raw_root=raw_root,
            expected_anchor=paired_ledger.config.expected_anchor,
            pair_manifest=paired_ledger.config.pair_manifest,
            expected_pair_manifest_hash=paired_ledger.config.expected_pair_manifest_hash,
        )
    with pytest.raises(
        MilestoneRevocationIntegrityError,
        match="database/raw pair identity mismatch",
    ):
        load_milestone_revocation_projection(
            ledger,
            database_path=database,
            raw_root=other_raw,
            expected_anchor=paired_ledger.config.expected_anchor,
            pair_manifest=paired_ledger.config.pair_manifest,
            expected_pair_manifest_hash=paired_ledger.config.expected_pair_manifest_hash,
        )
