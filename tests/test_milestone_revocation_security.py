from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from live_betting.milestone_revocation import (
    MilestoneRevocationConfig,
    MilestoneRevocationIntegrityError,
    append_milestone_revocation,
    create_pair_baseline_manifest,
    initialize_milestone_revocation_ledger,
    load_milestone_revocation_projection,
)
from tests.milestone_revocation_fixture import milestone_revocation_record


P0_ID = "9" * 64


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fixture(tmp_path: Path) -> tuple[MilestoneRevocationConfig, dict[str, object]]:
    database = tmp_path / "pair.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE baseline_rows (value TEXT NOT NULL)")
    connection.execute("INSERT INTO baseline_rows VALUES ('frozen')")
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
    (raw_root / "history.jsonl").write_bytes(b'{"old":1}\n')
    (raw_root / "nested").mkdir()
    (raw_root / "nested" / "history.jsonl").write_bytes(b'{"nested":1}\n')
    manifest = create_pair_baseline_manifest(
        database, raw_root, p0_baseline_evidence_identity=P0_ID
    )
    manifest_hash = _hash(manifest)
    ledger = tmp_path / "ledger"
    anchor = initialize_milestone_revocation_ledger(
        ledger,
        database_path=database,
        raw_root=raw_root,
        pair_manifest=manifest,
        expected_pair_manifest_hash=manifest_hash,
        p0_baseline_evidence_identity=P0_ID,
    )
    return (
        MilestoneRevocationConfig(
            root=ledger,
            database_path=database,
            raw_root=raw_root,
            expected_anchor=anchor,
            pair_manifest=manifest,
            expected_pair_manifest_hash=manifest_hash,
        ),
        anchor,
    )


def _replace_anchor(
    config: MilestoneRevocationConfig, anchor: dict[str, object]
) -> MilestoneRevocationConfig:
    return MilestoneRevocationConfig(
        root=config.root,
        database_path=config.database_path,
        raw_root=config.raw_root,
        expected_anchor=anchor,
        pair_manifest=config.pair_manifest,
        expected_pair_manifest_hash=config.expected_pair_manifest_hash,
    )


def _append(config: MilestoneRevocationConfig, record: dict[str, object], **kwargs: object):
    return append_milestone_revocation(
        config.root,
        record,
        database_path=config.database_path,
        raw_root=config.raw_root,
        expected_anchor=config.expected_anchor,
        pair_manifest=config.pair_manifest,
        expected_pair_manifest_hash=config.expected_pair_manifest_hash,
        **kwargs,
    )


def _concurrent_append_worker(
    root: str,
    database: str,
    raw_root: str,
    anchor: dict[str, object],
    manifest: bytes,
    manifest_hash: str,
    sample: str,
    output: multiprocessing.Queue,
) -> None:
    record = milestone_revocation_record(sample_key=sample)
    record["conflict"]["authority_evidence_refs"] = [f"authority:{sample}"]
    try:
        new_anchor = append_milestone_revocation(
            Path(root),
            record,
            database_path=Path(database),
            raw_root=Path(raw_root),
            expected_anchor=anchor,
            pair_manifest=manifest,
            expected_pair_manifest_hash=manifest_hash,
        )
    except Exception as error:  # pragma: no cover - asserted in the parent
        output.put(("error", type(error).__name__, str(error)))
    else:
        output.put(("ok", new_anchor))


def test_configured_load_requires_external_anchor_and_pair_baseline(tmp_path: Path) -> None:
    config, _ = _fixture(tmp_path)
    with pytest.raises(MilestoneRevocationIntegrityError, match="external anchor"):
        load_milestone_revocation_projection(
            config.root,
            database_path=config.database_path,
            raw_root=config.raw_root,
        )


def test_independent_anchor_and_pair_files_require_their_expected_hashes(
    tmp_path: Path,
) -> None:
    config, anchor = _fixture(tmp_path)
    anchor_file = tmp_path / "external-anchor.json"
    anchor_file.write_bytes(
        json.dumps(anchor, sort_keys=True, separators=(",", ":")).encode("ascii")
    )
    pair_file = tmp_path / "external-pair.json"
    pair_file.write_bytes(config.pair_manifest)
    file_config = MilestoneRevocationConfig(
        root=config.root,
        database_path=config.database_path,
        raw_root=config.raw_root,
        expected_anchor=anchor_file,
        expected_anchor_hash=_hash(anchor_file.read_bytes()),
        pair_manifest=pair_file,
        expected_pair_manifest_hash=_hash(pair_file.read_bytes()),
    )
    assert load_milestone_revocation_projection(config=file_config)["status"] == "active"

    duplicate_anchor = tmp_path / "duplicate-anchor.json"
    duplicate_anchor.write_bytes(
        b'{"schema":"duplicate",' + anchor_file.read_bytes()[1:]
    )
    with pytest.raises(MilestoneRevocationIntegrityError, match="JSON is invalid"):
        load_milestone_revocation_projection(
            config=MilestoneRevocationConfig(
                root=config.root,
                database_path=config.database_path,
                raw_root=config.raw_root,
                expected_anchor=duplicate_anchor,
                expected_anchor_hash=_hash(duplicate_anchor.read_bytes()),
                pair_manifest=pair_file,
                expected_pair_manifest_hash=_hash(pair_file.read_bytes()),
            )
        )


@pytest.mark.parametrize("target_name", ["index", "lock", "object", "seal", "journal"])
def test_every_mutable_ledger_file_rejects_external_hardlinks(
    tmp_path: Path, target_name: str
) -> None:
    config, _ = _fixture(tmp_path)
    if target_name == "journal":
        with pytest.raises(RuntimeError):
            _append(
                config,
                milestone_revocation_record(),
                _crash_at="object_only",
            )
        target = config.root / "transaction.json"
    elif target_name == "index":
        target = config.root / "index.jsonl"
    elif target_name == "lock":
        target = config.root / "ledger.lock"
    elif target_name == "object":
        target = next((config.root / "objects").iterdir())
    else:
        target = next((config.root / "seals").iterdir())
    external = tmp_path / f"external-{target_name}"
    external.write_bytes(target.read_bytes())
    target.unlink()
    os.link(external, target)
    with pytest.raises(MilestoneRevocationIntegrityError, match="hard link"):
        load_milestone_revocation_projection(config=config)


@pytest.mark.parametrize("target_name", ["root", "objects", "index"])
def test_ledger_root_directory_and_file_symlinks_are_rejected(
    tmp_path: Path, target_name: str
) -> None:
    config, _ = _fixture(tmp_path)
    try:
        if target_name == "root":
            actual = tmp_path / "actual-ledger"
            config.root.rename(actual)
            os.symlink(actual, config.root, target_is_directory=True)
        elif target_name == "objects":
            target = config.root / "objects"
            actual = tmp_path / "actual-objects"
            target.rename(actual)
            os.symlink(actual, target, target_is_directory=True)
        else:
            target = config.root / "index.jsonl"
            actual = tmp_path / "actual-index"
            actual.write_bytes(target.read_bytes())
            target.unlink()
            os.symlink(actual, target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    with pytest.raises(MilestoneRevocationIntegrityError, match="symlink|unsafe"):
        load_milestone_revocation_projection(config=config)


@pytest.mark.parametrize("crash_at", ["object_only", "seal_only", "index_only"])
def test_crashed_append_recovers_to_old_external_anchor(
    tmp_path: Path, crash_at: str
) -> None:
    config, anchor = _fixture(tmp_path)
    with pytest.raises(RuntimeError, match="simulated append crash"):
        _append(config, milestone_revocation_record(), _crash_at=crash_at)

    projection = load_milestone_revocation_projection(config=config)
    assert projection["ledger_integrity"]["entry_count"] == 1
    assert projection["ledger_integrity"]["head_entry_hash"] == anchor["head_entry_hash"]
    assert not (config.root / "transaction.json").exists()


def test_stale_anchor_append_is_rejected_and_two_process_writers_do_not_corrupt(
    tmp_path: Path,
) -> None:
    config, old_anchor = _fixture(tmp_path)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_append_worker,
            args=(
                str(config.root),
                str(config.database_path),
                str(config.raw_root),
                old_anchor,
                config.pair_manifest,
                config.expected_pair_manifest_hash,
                f"sample-{index}",
                output,
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    results = [output.get(timeout=5) for _ in processes]
    successes = [item for item in results if item[0] == "ok"]
    failures = [item for item in results if item[0] == "error"]
    assert len(successes) == 1
    assert len(failures) == 1
    assert "stale external anchor" in failures[0][2]
    new_anchor = successes[0][1]
    projection = load_milestone_revocation_projection(
        config=_replace_anchor(config, new_anchor)
    )
    assert projection["ledger_integrity"]["entry_count"] == 2


def test_suffix_append_is_allowed_but_same_inode_database_and_raw_prefix_tamper_fail(
    tmp_path: Path,
) -> None:
    config, _ = _fixture(tmp_path)
    connection = sqlite3.connect(config.database_path)
    connection.execute("INSERT INTO baseline_rows VALUES ('suffix')")
    connection.commit()
    connection.close()
    raw_file = config.raw_root / "history.jsonl"
    with raw_file.open("ab") as stream:
        stream.write(b'{"new":2}\n')
    assert load_milestone_revocation_projection(config=config)["status"] == "active"

    raw_file.write_bytes(b'{"evil":1}\n{"new":2}\n')
    with pytest.raises(MilestoneRevocationIntegrityError, match="raw baseline"):
        load_milestone_revocation_projection(config=config)

    raw_file.write_bytes(b'{"old":1}\n{"new":2}\n')
    other = tmp_path / "other.db"
    replacement = sqlite3.connect(other)
    replacement.execute("CREATE TABLE baseline_rows (value TEXT NOT NULL)")
    replacement.execute("INSERT INTO baseline_rows VALUES ('replaced')")
    replacement.commit()
    replacement.close()
    config.database_path.write_bytes(other.read_bytes())
    with pytest.raises(MilestoneRevocationIntegrityError, match="database baseline"):
        load_milestone_revocation_projection(config=config)


def test_old_self_consistent_copy_and_last_entry_deletion_are_rejected(tmp_path: Path) -> None:
    config, old_anchor = _fixture(tmp_path)
    old_copy = tmp_path / "old-copy"
    shutil.copytree(config.root, old_copy)
    new_anchor = _append(config, milestone_revocation_record())
    new_config = _replace_anchor(config, new_anchor)
    load_milestone_revocation_projection(config=new_config)

    shutil.rmtree(config.root)
    shutil.copytree(old_copy, config.root)
    with pytest.raises(MilestoneRevocationIntegrityError, match="external anchor"):
        load_milestone_revocation_projection(config=new_config)

    shutil.rmtree(config.root)
    shutil.copytree(old_copy, config.root)
    restored = _replace_anchor(config, old_anchor)
    next_anchor = _append(restored, milestone_revocation_record())
    finalized = _replace_anchor(restored, next_anchor)
    load_milestone_revocation_projection(config=finalized)
    lines = (config.root / "index.jsonl").read_bytes().splitlines(keepends=True)
    last = json.loads(lines[-1])
    (config.root / "index.jsonl").write_bytes(b"".join(lines[:-1]))
    (config.root / "objects" / f"{last['object_hash']}.json").unlink()
    for seal in (config.root / "seals").glob(f"{len(lines):020d}-*.json"):
        seal.unlink()
    with pytest.raises(MilestoneRevocationIntegrityError, match="external anchor"):
        load_milestone_revocation_projection(config=finalized)


def test_strict_json_hardlinks_and_unreferenced_files_fail_closed(tmp_path: Path) -> None:
    config, _ = _fixture(tmp_path)
    external = tmp_path / "external-index"
    external.write_bytes((config.root / "index.jsonl").read_bytes())
    (config.root / "index.jsonl").unlink()
    os.link(external, config.root / "index.jsonl")
    with pytest.raises(MilestoneRevocationIntegrityError, match="hard link"):
        load_milestone_revocation_projection(config=config)

    (config.root / "index.jsonl").unlink()
    shutil.copyfile(external, config.root / "index.jsonl")
    unreferenced = {"unexpected": float("nan")}
    (config.root / "objects" / ("f" * 64 + ".json")).write_text(
        json.dumps(unreferenced), encoding="utf-8"
    )
    with pytest.raises(MilestoneRevocationIntegrityError):
        load_milestone_revocation_projection(config=config)


def test_raw_baseline_rejects_intermediate_symlink(
    tmp_path: Path,
) -> None:
    config, _ = _fixture(tmp_path)
    nested = config.raw_root / "nested"
    external_nested = tmp_path / "external-nested"
    nested.rename(external_nested)
    try:
        os.symlink(external_nested, nested, target_is_directory=True)
    except OSError as error:
        external_nested.rename(nested)
        pytest.skip(f"symlink creation unavailable: {error}")
    with pytest.raises(MilestoneRevocationIntegrityError, match="symlink|escapes"):
        load_milestone_revocation_projection(config=config)


def test_raw_baseline_rejects_file_hardlink(tmp_path: Path) -> None:
    config, _ = _fixture(tmp_path)
    raw_file = config.raw_root / "history.jsonl"
    external_file = tmp_path / "external-history.jsonl"
    external_file.write_bytes(raw_file.read_bytes())
    raw_file.unlink()
    os.link(external_file, raw_file)
    with pytest.raises(MilestoneRevocationIntegrityError, match="hard link"):
        load_milestone_revocation_projection(config=config)

def test_governance_timestamps_before_record_or_discovery_require_review(tmp_path: Path) -> None:
    config, _ = _fixture(tmp_path)
    record = milestone_revocation_record()
    record["governance"]["initiator"]["signed_at"] = "2026-06-30T00:00:00+00:00"
    record["governance"]["independent_verifier"]["signed_at"] = (
        "2026-07-01T12:00:00+00:00"
    )
    record["governance"]["approvers"][0]["signed_at"] = "2026-07-01T12:00:00+00:00"
    record["disposition"]["decided_at"] = "2026-07-01T12:00:00+00:00"
    anchor = _append(config, record)
    projection = load_milestone_revocation_projection(
        config=_replace_anchor(config, anchor)
    )
    [projected] = projection["records"]
    assert projected["revocation_status"] == "review_required"
    assert "initiator_signature_precedes_original_record" in projected["review_reasons"]
    assert "disposition_precedes_conflict_discovery" in projected["review_reasons"]
    assert projection["revoked_milestones"] == []


@pytest.mark.parametrize(
    ("field", "timestamp", "reason"),
    (
        (
            "initiator",
            "2026-07-02T00:30:00+00:00",
            "initiator_signature_precedes_effective_at",
        ),
        (
            "independent_verifier",
            "2026-07-02T03:00:00+00:00",
            "independent_verifier_signature_follows_disposition",
        ),
        (
            "approver",
            "2026-07-02T03:00:00+00:00",
            "approver_signature_follows_disposition",
        ),
    ),
)
def test_governance_signatures_must_follow_effective_time_and_precede_disposition(
    tmp_path: Path,
    field: str,
    timestamp: str,
    reason: str,
) -> None:
    config, _ = _fixture(tmp_path)
    record = milestone_revocation_record()
    if field == "approver":
        record["governance"]["approvers"][0]["signed_at"] = timestamp
    else:
        record["governance"][field]["signed_at"] = timestamp
    anchor = _append(config, record)

    projection = load_milestone_revocation_projection(
        config=_replace_anchor(config, anchor)
    )

    assert projection["status"] == "review_required"
    assert reason in projection["records"][0]["review_reasons"]
    assert projection["revoked_milestones"] == []


@pytest.mark.parametrize(
    "lineage_case",
    ("settlement_order_mismatch", "missing_decision", "wrong_persisted_decision"),
)
def test_sample_lineage_must_match_persisted_settlement_order_and_decision(
    tmp_path: Path,
    lineage_case: str,
) -> None:
    config, _ = _fixture(tmp_path)
    record = milestone_revocation_record()
    lineage = record["affected"]["sample_lineage"][0]
    if lineage_case == "settlement_order_mismatch":
        lineage["settlement_key"] = "different-order"
        expected = "settlement/order lineage identity mismatch"
    elif lineage_case == "missing_decision":
        lineage["decision_key"] = "missing-decision"
        expected = "decision lineage is not persisted"
    else:
        connection = sqlite3.connect(config.database_path)
        connection.execute("INSERT INTO strategy_decisions VALUES ('decision-2')")
        connection.commit()
        connection.close()
        lineage["decision_key"] = "decision-2"
        expected = "does not match persisted lineage"

    with pytest.raises(MilestoneRevocationIntegrityError, match=expected):
        _append(config, record)


def test_sample_lineage_is_explicit_and_not_treated_as_a_decision_key(tmp_path: Path) -> None:
    config, _ = _fixture(tmp_path)
    record = milestone_revocation_record(sample_key="sample-not-a-decision")
    record["affected"]["decision_keys"] = []
    record["affected"]["order_keys"] = []
    record["affected"]["settlement_keys"] = []
    record["affected"]["sample_lineage"] = [
        {
            "sample_key": "sample-not-a-decision",
            "settlement_key": "order-1",
            "order_key": "order-1",
            "decision_key": "decision-1",
        }
    ]
    anchor = _append(config, record)
    projection = load_milestone_revocation_projection(
        config=_replace_anchor(config, anchor)
    )
    assert projection["isolated_keys"]["decision_keys"] == ["decision-1"]
    assert "sample-not-a-decision" not in projection["isolated_keys"]["decision_keys"]
