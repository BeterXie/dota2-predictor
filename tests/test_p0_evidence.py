from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from scripts.p0_evidence import (
    BASELINE_COMMON_TESTS,
    CRITICAL_TESTS,
    _classify_untracked,
    _tree_records,
    _verify_prefix_record,
    _write_canonical,
    canonical_bytes,
    database_cutoffs,
    file_identity,
    generate_manifest,
    node_comparison,
    parser,
    production_sqlite_guard,
    refresh_manifest,
    run_tests,
    sha256_bytes,
    verify_manifest,
    workspace_identity,
)


PROPOSED_STRATEGY_VERSION = "comeback-shadow-v5-executable-contract"
EVALUATOR_HASH = "c2d2f741e3b172b1fda1ca161619961e597070388d46d97848391b3f2f91ad24"
POLICY_HASH = "6e0c8a278378ee4c070f5d11204ca23397f54c7b6b703b544adaf105a259d696"
EFFECTIVE_AT = "2026-07-24T09:00:00Z"


def test_canonical_serialization_is_stable() -> None:
    left = canonical_bytes({"z": 1, "a": [True, None, "x"]})
    right = canonical_bytes({"a": [True, None, "x"], "z": 1})
    assert left == right
    assert sha256_bytes(left) == sha256_bytes(right)


def test_production_sqlite_guard_blocks_same_file_identity(tmp_path: Path) -> None:
    production = tmp_path / "production.db"
    sqlite3.connect(production).close()
    alias = tmp_path / "alias.db"
    try:
        alias.hardlink_to(production)
    except OSError:
        pytest.skip("hard links are unavailable")
    audit = tmp_path / "connections.jsonl"

    with production_sqlite_guard(production, audit):
        with pytest.raises(RuntimeError, match="production path guard"):
            sqlite3.connect(alias)
        safe = sqlite3.connect(tmp_path / "fixture.db")
        safe.close()

    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert [record["production_identity_match"] for record in records] == [True, False]


def test_database_cutoffs_use_read_only_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "authority.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE authority (item_id INTEGER, observed_at TEXT)")
    connection.executemany(
        "INSERT INTO authority VALUES (?, ?)",
        ((1, "2026-07-23T00:00:00+00:00"), (2, "2026-07-24T00:00:00+00:00")),
    )
    connection.commit()
    connection.close()
    before = file_identity(database)

    snapshot = database_cutoffs(database)

    authority = next(item for item in snapshot["tables"] if item["table"] == "authority")
    assert authority["row_cutoff"]["max_rowid"] == 2
    assert authority["row_cutoff"]["item_id"] == 2
    assert authority["row_cutoff"]["observed_at"] == "2026-07-24T00:00:00+00:00"
    assert snapshot["access_contract"].startswith("sqlite URI mode=ro")
    assert file_identity(database) == before


@pytest.mark.parametrize(
    ("path", "category"),
    (
        ("tests/fixture.json", "test"),
        ("docs/decision.md", "formal_doc"),
        ("CONTEXT.md", "formal_doc"),
        ("live_betting/new_module.py", "source"),
        ("dogfood-output/report.md", None),
    ),
)
def test_untracked_scope_classification(path: str, category: str | None) -> None:
    assert _classify_untracked(path) == category


def test_verify_rejects_noncanonical_or_tampered_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{ "serialization":"RFC8785/JCS", "schema":"wrong" }', encoding="utf-8")
    manifest.with_suffix(".json.sha256").write_text(
        f"{sha256_bytes(manifest.read_bytes())}  manifest.json\n", encoding="ascii"
    )
    assert verify_manifest(manifest) == 1


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=workspace, check=True, capture_output=True)


def _git_workspace(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "P0 Test")
    _git(root, "config", "user.email", "p0@example.invalid")
    (root / "tracked.py").write_text("BASE = 1\n", encoding="utf-8")
    _git(root, "add", "tracked.py")
    _git(root, "commit", "-qm", "baseline")
    return root


def test_workspace_identity_captures_both_diffs_and_required_untracked(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path / "repo")
    (workspace / "tracked.py").write_text("STAGED = 1\n", encoding="utf-8")
    _git(workspace, "add", "tracked.py")
    (workspace / "tracked.py").write_text("UNSTAGED = 1\n", encoding="utf-8")
    for relative in (
        "tests/fixture.json",
        "docs/formal.md",
        "source.py",
        "dogfood-output/report.md",
    ):
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")

    identity = workspace_identity(workspace, tmp_path / "audit")

    assert identity["root"] == str(workspace.resolve())
    assert identity["tracked_diffs"]["staged"]["bytes"] > 0
    assert identity["tracked_diffs"]["unstaged"]["bytes"] > 0
    selected = {
        item["path"]: item["category"]
        for item in identity["untracked_source_test_formal_docs"]
    }
    assert selected == {
        "docs/formal.md": "formal_doc",
        "source.py": "source",
        "tests/fixture.json": "test",
    }
    assert identity["untracked_out_of_scope"] == [
        {
            "path": "dogfood-output/report.md",
            "reason": "not source/test/formal-doc scope",
        }
    ]


def test_append_only_prefix_allows_suffix_and_rejects_prefix_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vision"
    root.mkdir()
    path = root / "observations.jsonl"
    path.write_bytes(b'{"frame":1}\n')
    record = _tree_records(root, append_only=True)["files"][0]
    path.write_bytes(path.read_bytes() + b'{"frame":2}\n')
    assert _verify_prefix_record(record, root) is None
    path.write_bytes(b'X' + path.read_bytes()[1:])
    assert "prefix sha256 mismatch" in str(_verify_prefix_record(record, root))


def test_empty_evidence_root_is_explicitly_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    evidence = _tree_records(root, append_only=True)
    assert evidence["status"] == "unavailable"
    assert evidence["file_count"] == 0
    assert "no evidence files" in evidence["reason"]


def test_run_tests_activates_subprocess_guard_and_freezes_artifacts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test_safe.py").write_text(
        "import sqlite3\n"
        "def test_safe(tmp_path):\n"
        "    sqlite3.connect(tmp_path / 'safe.db').close()\n",
        encoding="utf-8",
    )
    production = tmp_path / "production.db"
    sqlite3.connect(production).close()
    output = tmp_path / "evidence"

    assert run_tests(
        workspace,
        output,
        production,
        ["test_safe.py"],
        label="current-critical",
    ) == 0
    command = json.loads((output / "command-record.json").read_text())
    audit = command["production_connection_audit"]
    assert audit["guard_activated"] is True
    assert audit["guard_result"] == "passed"
    assert command["node_status_counts"] == {"passed": 1}
    for name in ("production_path_guard", "pytest_node_recorder", "guard_activation_marker"):
        artifact = command["artifacts"][name]
        assert Path(artifact["path"]).is_file()
        assert sha256_bytes(Path(artifact["path"]).read_bytes()) == artifact["sha256"]
    with pytest.raises(ValueError, match="fresh/empty"):
        run_tests(workspace, output, production, ["test_safe.py"], label="rerun")


def test_run_tests_blocks_production_file_uri_in_subprocess(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    production = tmp_path / "production.db"
    sqlite3.connect(production).close()
    (workspace / "test_blocked.py").write_text(
        "import os, sqlite3\n"
        "def test_blocked():\n"
        "    sqlite3.connect('file:' + os.environ['P0_PRODUCTION_DATABASE'] + '?mode=ro', uri=True)\n",
        encoding="utf-8",
    )
    output = tmp_path / "blocked-evidence"

    assert run_tests(
        workspace,
        output,
        production,
        ["test_blocked.py"],
        label="current-critical",
    ) == 2
    command = json.loads((output / "command-record.json").read_text())
    assert command["production_connection_audit"]["attempted_production_connections"] == 1
    assert command["production_connection_audit"]["guard_result"] == "failed"


def test_generated_manifest_round_trip_and_tamper_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _git_workspace(tmp_path / "repo")
    database = tmp_path / "production.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE authority (item_id INTEGER PRIMARY KEY, observed_at TEXT)")
    connection.execute("INSERT INTO authority VALUES (1, '2026-07-24T00:00:00+00:00')")
    connection.commit()
    connection.close()
    roots = []
    for name in ("raw", "vision-jsonl", "vision-frames"):
        root = tmp_path / name
        root.mkdir()
        roots.append(root)
    output = tmp_path / "audit"
    monkeypatch.setenv("STRATZ_API_TOKEN", "must-not-appear-in-manifest")
    args = Namespace(
        workspace=workspace,
        output_dir=output,
        production_database=database,
        clean_worktree=workspace,
        raw_root=roots[0],
        vision_jsonl_root=roots[1],
        vision_frame_root=roots[2],
        command_record=[],
        rollback_summary=None,
        strategy_version=PROPOSED_STRATEGY_VERSION,
        evaluator_hash=EVALUATOR_HASH,
        policy_hash=POLICY_HASH,
        serialization_version="rfc8785-jcs-v1",
        evaluator_artifact_ref="artifact://strategy/v5/evaluator",
        policy_artifact_ref="artifact://strategy/v5/policy",
        execution_owner="/root",
        independent_verifier="/root/p0_verification",
        production_db_operator="/root/p1_triage",
        m4_decision_owner="/root/p0_verification",
        roles_effective_at=EFFECTIVE_AT,
        user_page_acceptance="\u7528\u6237",
        page_acceptance_effective_at=EFFECTIVE_AT,
    )

    assert generate_manifest(args) == 0
    manifest = output / "workspace-evidence-manifest.v1.json"
    assert verify_manifest(manifest) == 0
    assert b"must-not-appear-in-manifest" not in manifest.read_bytes()
    payload = json.loads(manifest.read_bytes())
    assert payload["strategy_contract"] == {
        "status": "bound",
        "proposal_status": "proposal_not_milestone_acceptance",
        "strategy_version": PROPOSED_STRATEGY_VERSION,
        "evaluator_hash": EVALUATOR_HASH,
        "policy_hash": POLICY_HASH,
        "serialization_version": "rfc8785-jcs-v1",
        "evaluator_artifact_ref": "artifact://strategy/v5/evaluator",
        "evaluator_artifact_ref_status": "secret_safe",
        "policy_artifact_ref": "artifact://strategy/v5/policy",
        "policy_artifact_ref_status": "secret_safe",
        "validation_issues": [],
    }
    assert [item["account"] for item in payload["operational_roles"]] == [
        "/root",
        "/root/p0_verification",
        "/root/p1_triage",
        "/root/p0_verification",
    ]
    assert all(
        item["identity_kind"] == "thread_authorized_operational_task_account"
        and item["person_identity_asserted"] is False
        and item["effective_at"] == EFFECTIVE_AT
        for item in payload["operational_roles"]
    )
    assert payload["evidence"]["canonical_evaluator"] == {
        "status": "available",
        "binding_source": "#/strategy_contract",
        "artifact_ref_pointer": "#/strategy_contract/evaluator_artifact_ref",
        "sha256_pointer": "#/strategy_contract/evaluator_hash",
    }
    assert payload["evidence"]["policy"] == {
        "status": "available",
        "binding_source": "#/strategy_contract",
        "artifact_ref_pointer": "#/strategy_contract/policy_artifact_ref",
        "sha256_pointer": "#/strategy_contract/policy_hash",
    }
    blocker_codes = [item["code"] for item in payload["p0_blockers"]]
    assert "strategy_contract_unavailable" not in blocker_codes
    assert blocker_codes.count("operational_role_person_identity_unasserted") == 4
    assert payload["page_acceptance"]["accepted_by"] == "\u7528\u6237"
    assert payload["page_acceptance"]["m1_acceptance"] is False
    assert payload["page_acceptance"]["milestone_effect"] == "none"
    refreshed_output = tmp_path / "audit-v5"
    monkeypatch.setattr(
        "scripts.p0_evidence.database_cutoffs",
        lambda _path: pytest.fail("refresh must not reconnect to the database"),
    )
    assert refresh_manifest(manifest, refreshed_output) == 0
    refreshed_manifest = refreshed_output / "workspace-evidence-manifest.v1.json"
    assert verify_manifest(refreshed_manifest) == 0
    refreshed_payload = json.loads(refreshed_manifest.read_bytes())
    assert refreshed_payload["evidence"]["canonical_evaluator"] == payload["evidence"][
        "canonical_evaluator"
    ]
    assert refreshed_payload["evidence"]["policy"] == payload["evidence"]["policy"]
    assert [
        item["code"]
        for item in refreshed_payload["p0_blockers"]
        if item["code"] == "operational_role_person_identity_unasserted"
    ] == ["operational_role_person_identity_unasserted"] * 4
    assert (
        refreshed_payload["manifest_generation"]["environment_contract"][
            "production_database_access"
        ]
        == "none; frozen cutoff inherited from source manifest"
    )
    original_hash = sha256_bytes(manifest.read_bytes())
    changed = {**payload, "strategy_contract": {**payload["strategy_contract"], "policy_hash": "0" * 64}}
    assert sha256_bytes(canonical_bytes(changed)) != original_hash

    sidecar = manifest.with_suffix(".json.sha256")
    original_sidecar = sidecar.read_bytes()
    sidecar.write_bytes(b"")
    assert verify_manifest(manifest) == 1
    sidecar.write_bytes(original_sidecar)
    original = manifest.read_bytes()
    manifest.write_bytes(original[:-1] + b" ")
    assert verify_manifest(manifest) == 1


def test_generate_missing_hash_and_role_is_not_ready_with_blockers(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path / "repo")
    database = tmp_path / "production.db"
    sqlite3.connect(database).close()
    roots = []
    for name in ("raw", "vision-jsonl", "vision-frames"):
        root = tmp_path / name
        root.mkdir()
        roots.append(root)
    output = tmp_path / "audit"
    args = Namespace(
        workspace=workspace,
        output_dir=output,
        production_database=database,
        clean_worktree=workspace,
        raw_root=roots[0],
        vision_jsonl_root=roots[1],
        vision_frame_root=roots[2],
        command_record=[],
        rollback_summary=None,
        strategy_version=PROPOSED_STRATEGY_VERSION,
        evaluator_hash=None,
        policy_hash=POLICY_HASH,
        serialization_version="rfc8785-jcs-v1",
        evaluator_artifact_ref="workspace:live_betting/strategy_contract.py#registry",
        policy_artifact_ref="artifact://strategy/v5/policy",
        execution_owner="/root",
        independent_verifier=None,
        production_db_operator="/root/p1_triage",
        m4_decision_owner="/root/p0_verification",
        roles_effective_at="2026-07-24T09:00:00+08:00",
        user_page_acceptance="\u7528\u6237",
        page_acceptance_effective_at=None,
    )

    assert generate_manifest(args) == 0
    manifest_path = output / "workspace-evidence-manifest.v1.json"
    manifest = json.loads(manifest_path.read_bytes())
    blocker_codes = {item["code"] for item in manifest["p0_blockers"]}
    assert manifest["status"] == "not_ready"
    assert manifest["strategy_contract"]["evaluator_artifact_ref"] is None
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert "#registry" not in manifest_text
    assert "workspace:live_betting/strategy_contract.py" not in manifest_text
    assert "strategy_contract_hash_missing" in blocker_codes
    assert "strategy_contract_artifact_ref_unsafe" in blocker_codes
    assert manifest["evidence"]["canonical_evaluator"]["status"] == "unavailable"
    assert manifest["evidence"]["policy"]["status"] == "unavailable"
    assert sum(
        item["code"] == "strategy_contract_unavailable"
        for item in manifest["p0_blockers"]
    ) == 2
    assert "operational_role_unbound" in blocker_codes
    assert "operational_role_person_identity_unasserted" in blocker_codes
    assert "page_acceptance_unbound" in blocker_codes
    assert verify_manifest(manifest_path) == 0


def test_verify_rejects_strategy_evidence_tampered_away_from_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _git_workspace(tmp_path / "repo")
    database = tmp_path / "production.db"
    sqlite3.connect(database).close()
    roots = []
    for name in ("raw", "vision-jsonl", "vision-frames"):
        root = tmp_path / name
        root.mkdir()
        roots.append(root)
    output = tmp_path / "audit"
    args = Namespace(
        workspace=workspace,
        output_dir=output,
        production_database=database,
        clean_worktree=workspace,
        raw_root=roots[0],
        vision_jsonl_root=roots[1],
        vision_frame_root=roots[2],
        command_record=[],
        rollback_summary=None,
        strategy_version=PROPOSED_STRATEGY_VERSION,
        evaluator_hash=EVALUATOR_HASH,
        policy_hash=POLICY_HASH,
        serialization_version="rfc8785-jcs-v1",
        evaluator_artifact_ref="artifact://strategy/v5/evaluator",
        policy_artifact_ref="artifact://strategy/v5/policy",
        execution_owner="/root",
        independent_verifier="/root/p0_verification",
        production_db_operator="/root/p1_triage",
        m4_decision_owner="/root/p0_verification",
        roles_effective_at=EFFECTIVE_AT,
        user_page_acceptance="\u7528\u6237",
        page_acceptance_effective_at=EFFECTIVE_AT,
    )
    assert generate_manifest(args) == 0
    manifest_path = output / "workspace-evidence-manifest.v1.json"
    payload = json.loads(manifest_path.read_bytes())
    payload["evidence"]["canonical_evaluator"] = {
        "status": "unavailable",
        "reason": "tampered static template",
    }
    manifest_path.write_bytes(canonical_bytes(payload))
    manifest_path.with_suffix(".json.sha256").write_text(
        f"{sha256_bytes(manifest_path.read_bytes())}  {manifest_path.name}\n",
        encoding="ascii",
    )

    assert verify_manifest(manifest_path) == 1


def _command_entry(
    root: Path,
    evidence_root: Path,
    *,
    label: str,
    test_files: tuple[str, ...],
    ended_at: str,
    nodes: dict[str, str],
) -> dict[str, object]:
    record_root = evidence_root / label
    record_root.mkdir(parents=True)
    node_path = record_root / "pytest-nodes.json"
    node_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {"nodeid": nodeid, "status": status}
                    for nodeid, status in nodes.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    command = ["python", "-m", "pytest", *test_files]
    record = {
        "schema": "dota2-p0-command-record-v1",
        "label": label,
        "cwd": str(root.resolve()),
        "started_at": "2026-07-24T08:00:00Z",
        "ended_at": ended_at,
        "command": command,
        "requested_test_files": list(test_files),
        "selected_test_files": list(test_files),
        "node_status_counts": {
            status: sum(value == status for value in nodes.values())
            for status in sorted(set(nodes.values()))
        },
        "artifacts": {"node_results": {"path": str(node_path)}},
    }
    return {
        "record": record,
        "artifact": {"sha256": sha256_bytes(canonical_bytes(record))},
    }


def test_node_comparison_selects_latest_exact_custom_label_record(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    clean = tmp_path / "clean-8f6d4cd"
    workspace.mkdir()
    clean.mkdir()
    evidence = tmp_path / "evidence"
    clean_nodes = {
        "tests/test_live_report.py::test_common_pass": "passed",
        "tests/test_live_report.py::test_common_fail": "failed",
    }
    old_current_nodes = {**clean_nodes, "tests/test_service_health.py::test_old": "failed"}
    latest_current_nodes = {
        **clean_nodes,
        "tests/test_direct_source_isolation.py::test_current_only": "passed",
    }
    commands = [
        _command_entry(
            workspace,
            evidence,
            label="current-critical",
            test_files=CRITICAL_TESTS,
            ended_at="2026-07-24T08:10:00Z",
            nodes=old_current_nodes,
        ),
        _command_entry(
            clean,
            evidence,
            label="clean-baseline-common",
            test_files=BASELINE_COMMON_TESTS,
            ended_at="2026-07-24T16:20:00+08:00",
            nodes=clean_nodes,
        ),
        _command_entry(
            workspace,
            evidence,
            label="p0-adr0012-12file",
            test_files=CRITICAL_TESTS,
            ended_at="2026-07-24T16:30:00+08:00",
            nodes=latest_current_nodes,
        ),
    ]

    comparison = node_comparison(
        commands,
        workspace_root=workspace,
        clean_root=clean,
    )

    assert comparison["status"] == "comparable"
    assert comparison["current_command_identity"]["label"] == "p0-adr0012-12file"
    assert comparison["current_command_identity"]["ended_at"] == "2026-07-24T08:30:00+00:00"
    assert comparison["current_node_status_counts"] == {"failed": 1, "passed": 2}
    assert comparison["current_failures"] == [
        "tests/test_live_report.py::test_common_fail"
    ]
    assert comparison["clean_baseline_commit"] == "8f6d4cd"


def test_verify_rejects_semantically_tampered_binding(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    value = {
        "schema": "dota2-p0-workspace-evidence-manifest-v1",
        "serialization": "RFC8785/JCS",
        "commands": [],
        "evidence": {},
        "operational_roles": [],
        "page_acceptance": {},
        "p0_blockers": [],
        "production_database": {},
        "status": "ready",
        "workspace": {},
    }
    manifest.write_bytes(canonical_bytes(value))
    manifest.with_suffix(".json.sha256").write_text(
        f"{sha256_bytes(manifest.read_bytes())}  manifest.json\n", encoding="ascii"
    )
    assert verify_manifest(manifest) == 1


def test_generate_parser_requires_explicit_contract_roles_and_timestamps() -> None:
    required = [
        "generate",
        "--workspace", "repo",
        "--output-dir", "audit",
        "--production-database", "production.db",
        "--clean-worktree", "clean",
        "--raw-root", "raw",
        "--vision-jsonl-root", "jsonl",
        "--vision-frame-root", "frames",
        "--strategy-version", PROPOSED_STRATEGY_VERSION,
        "--evaluator-hash", EVALUATOR_HASH,
        "--policy-hash", POLICY_HASH,
        "--serialization-version", "rfc8785-jcs-v1",
        "--evaluator-artifact-ref", "artifact://strategy/v5/evaluator",
        "--policy-artifact-ref", "artifact://strategy/v5/policy",
        "--execution-owner", "/root",
        "--independent-verifier", "/root/p0_verification",
        "--production-db-operator", "/root/p1_triage",
        "--m4-decision-owner", "/root/p0_verification",
        "--roles-effective-at", EFFECTIVE_AT,
        "--user-page-acceptance", "\u7528\u6237",
        "--page-acceptance-effective-at", EFFECTIVE_AT,
    ]
    assert parser().parse_args(required).policy_hash == POLICY_HASH
    missing_policy_hash = required[:]
    index = missing_policy_hash.index("--policy-hash")
    del missing_policy_hash[index:index + 2]
    with pytest.raises(SystemExit):
        parser().parse_args(missing_policy_hash)


def test_write_canonical_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    record = _write_canonical(path, {"b": 2, "a": 1})
    assert path.read_bytes() == b'{"a":1,"b":2}'
    assert record["sha256"] == sha256_bytes(path.read_bytes())
