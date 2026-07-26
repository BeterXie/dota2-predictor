"""Build and verify the ADR-0011 P0 evidence package.

All mutable output is directed to an ignored audit root. Production SQLite is
opened only for an explicit read-only cutoff snapshot; test and rollback
subcommands install a process-level guard that rejects that file identity.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import unquote, urlparse

import rfc8785

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


SCHEMA = "dota2-p0-workspace-evidence-manifest-v1"
SERIALIZATION = "RFC8785/JCS"
BASELINE_COMMIT = "8f6d4cd"
PROPOSED_STRATEGY_VERSION = "comeback-shadow-v5-executable-contract"
OPERATIONAL_ROLES = (
    "execution_owner",
    "independent_verifier",
    "production_db_operator",
    "m4_decision_owner",
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$")
SECRET_REF_PATTERN = re.compile(
    r"(?i)(?:^|[?&;])(?:access[_-]?token|api[_-]?key|authorization|credential|"
    r"password|secret|signature|sig|token)=|x-amz-|x-goog-"
)
CRITICAL_TESTS = (
    "tests/test_raybet_direct_response_audit.py",
    "tests/test_raybet_collector_resilience.py",
    "tests/test_direct_source_isolation.py",
    "tests/test_raybet_stream_scripts.py",
    "tests/test_service_health.py",
    "tests/test_monitoring_dashboard.py",
    "tests/test_successor_fill.py",
    "tests/test_shadow_monitor_safety.py",
    "tests/test_settlement_authority.py",
    "tests/test_postmatch_settlement.py",
    "tests/test_notification_outbox.py",
    "tests/test_live_report.py",
)
BASELINE_COMMON_TESTS = tuple(
    item
    for item in CRITICAL_TESTS
    if item
    not in {
        "tests/test_raybet_collector_resilience.py",
        "tests/test_direct_source_isolation.py",
    }
)
REQUIRED_PACKAGES = (
    "httpx",
    "pandas",
    "numpy",
    "scipy",
    "opencv-python",
    "Pillow",
    "rapidocr-onnxruntime",
    "rfc8785",
    "scikit-learn",
    "xgboost",
    "pyarrow",
    "fastapi",
    "pydantic",
    "uvicorn",
    "pyyaml",
    "psutil",
    "curl_cffi",
    "pytest",
)
SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cfg",
    ".css",
    ".go",
    ".h",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".mjs",
    ".ps1",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
AUTHORITY_NAME_FRAGMENTS = (
    "id",
    "key",
    "at",
    "time",
    "date",
    "sequence",
    "revision",
    "version",
)
TEST_ENVIRONMENT_KEYS = (
    "CI",
    "LANG",
    "LC_ALL",
    "NUMBER_OF_PROCESSORS",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "PYTHONHASHSEED",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "TZ",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return rfc8785.dumps(value)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _secret_safe_artifact_ref(value: object) -> bool:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    if ARTIFACT_REF_PATTERN.fullmatch(value) is None:
        return False
    parsed = urlparse(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    return SECRET_REF_PATTERN.search(value) is None


def _strategy_contract_binding(args: argparse.Namespace) -> dict[str, Any]:
    values = {
        name: getattr(args, name, None)
        for name in (
            "strategy_version",
            "evaluator_hash",
            "policy_hash",
            "serialization_version",
            "evaluator_artifact_ref",
            "policy_artifact_ref",
        )
    }
    issues: list[dict[str, str]] = []

    for name in ("strategy_version", "serialization_version"):
        if not isinstance(values[name], str) or not values[name].strip():
            issues.append(
                {
                    "code": "strategy_contract_identity_missing",
                    "reason": f"{name} is required",
                }
            )
    if values["strategy_version"] not in {None, "", PROPOSED_STRATEGY_VERSION}:
        issues.append(
            {
                "code": "strategy_contract_version_not_proposed",
                "reason": "strategy_version must bind the approved v5 proposal",
            }
        )
    for name in ("evaluator_hash", "policy_hash"):
        value = values[name]
        if value in {None, ""}:
            issues.append(
                {
                    "code": "strategy_contract_hash_missing",
                    "reason": f"{name} is required",
                }
            )
        elif not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
            issues.append(
                {
                    "code": "strategy_contract_hash_invalid",
                    "reason": f"{name} must be 64 lowercase hexadecimal characters",
                }
            )
    safe_refs: dict[str, str | None] = {}
    for name in ("evaluator_artifact_ref", "policy_artifact_ref"):
        value = values[name]
        recorded_status = getattr(args, f"{name}_status", None)
        if value in {None, ""}:
            if recorded_status == "unsafe_redacted":
                issues.append(
                    {
                        "code": "strategy_contract_artifact_ref_unsafe",
                        "reason": f"{name} is not a secret-safe reference",
                    }
                )
                safe_refs[f"{name}_status"] = "unsafe_redacted"
            else:
                issues.append(
                    {
                        "code": "strategy_contract_artifact_ref_missing",
                        "reason": f"{name} is required",
                    }
                )
                safe_refs[f"{name}_status"] = "missing"
            safe_refs[name] = None
        elif not _secret_safe_artifact_ref(value):
            issues.append(
                {
                    "code": "strategy_contract_artifact_ref_unsafe",
                    "reason": f"{name} is not a secret-safe reference",
                }
            )
            safe_refs[name] = None
            safe_refs[f"{name}_status"] = "unsafe_redacted"
        else:
            safe_refs[name] = str(value)
            safe_refs[f"{name}_status"] = "secret_safe"
    return {
        "status": "bound" if not issues else "invalid",
        "proposal_status": "proposal_not_milestone_acceptance",
        "strategy_version": values["strategy_version"],
        "evaluator_hash": values["evaluator_hash"],
        "policy_hash": values["policy_hash"],
        "serialization_version": values["serialization_version"],
        **safe_refs,
        "validation_issues": issues,
    }


def _strategy_contract_evidence(
    strategy_contract: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    available = strategy_contract.get("status") == "bound"

    def binding(artifact: str) -> dict[str, Any]:
        value = {
            "status": "available" if available else "unavailable",
            "binding_source": "#/strategy_contract",
            "artifact_ref_pointer": f"#/strategy_contract/{artifact}_artifact_ref",
            "sha256_pointer": f"#/strategy_contract/{artifact}_hash",
        }
        if not available:
            value["reason"] = "strategy contract is not validly bound"
        return value

    return {
        "canonical_evaluator": binding("evaluator"),
        "policy": binding("policy"),
    }


def _operational_role_bindings(args: argparse.Namespace) -> list[dict[str, Any]]:
    effective_at = getattr(args, "roles_effective_at", None)
    timestamp_valid = _valid_utc_timestamp(effective_at)
    bindings = []
    for role in OPERATIONAL_ROLES:
        account = getattr(args, role, None)
        account_valid = isinstance(account, str) and bool(account.strip())
        issues = []
        if not account_valid:
            issues.append("operational task account is required")
        if not timestamp_valid:
            issues.append("effective_at must be an explicit UTC timestamp")
        bindings.append(
            {
                "role": role,
                "status": "bound" if not issues else "unbound",
                "account": account if account_valid else None,
                "effective_at": effective_at if timestamp_valid else None,
                "identity_kind": "thread_authorized_operational_task_account",
                "binding_basis": "delegation_thread_authorization",
                "person_identity_asserted": False,
                "validation_issues": issues,
            }
        )
    accounts = {item["role"]: item["account"] for item in bindings}
    independent = accounts["independent_verifier"]
    if independent and independent in {
        accounts["execution_owner"],
        accounts["production_db_operator"],
    }:
        entry = next(
            item for item in bindings if item["role"] == "independent_verifier"
        )
        entry["status"] = "invalid"
        entry["validation_issues"].append(
            "independent verifier must use a separate account from execution and production DB operations"
        )
    return bindings


def _page_acceptance_binding(args: argparse.Namespace) -> dict[str, Any]:
    accepted_by = getattr(args, "user_page_acceptance", None)
    effective_at = getattr(args, "page_acceptance_effective_at", None)
    issues = []
    if not isinstance(accepted_by, str) or not accepted_by.strip():
        issues.append("user page acceptance subject is required")
    if not _valid_utc_timestamp(effective_at):
        issues.append("page acceptance effective_at must be an explicit UTC timestamp")
    return {
        "status": "recorded" if not issues else "unbound",
        "accepted_by": accepted_by if isinstance(accepted_by, str) and accepted_by.strip() else None,
        "effective_at": effective_at if _valid_utc_timestamp(effective_at) else None,
        "authority_kind": "user_page_authorization",
        "scope": "operational_task_accounts_only",
        "m1_acceptance": False,
        "milestone_effect": "none",
        "validation_issues": issues,
    }


def file_record(path: Path, *, display_path: str | None = None) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": display_path or str(path.resolve()),
        "bytes": stat.st_size,
        "mtime_ns": str(stat.st_mtime_ns),
        "sha256": sha256_file(path),
    }


def prefix_file_record(
    path: Path,
    *,
    display_path: str | None = None,
) -> dict[str, Any]:
    cutoff_bytes = path.stat().st_size
    digest = hashlib.sha256()
    line_count = 0
    remaining = cutoff_bytes
    with path.open("rb") as stream:
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise OSError(f"file truncated while freezing cutoff: {path}")
            digest.update(chunk)
            line_count += chunk.count(b"\n")
            remaining -= len(chunk)
    return {
        "path": display_path or str(path.resolve()),
        "cutoff_bytes": cutoff_bytes,
        "cutoff_lines": line_count,
        "sha256_through_cutoff": digest.hexdigest(),
    }


def file_identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    return {
        "resolved_path": str(resolved),
        "device": str(stat.st_dev),
        "inode": str(stat.st_ino),
        "bytes": stat.st_size,
        "mtime_ns": str(stat.st_mtime_ns),
    }


def same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError):
        return left.resolve() == right.resolve()


def _sqlite_target(value: object) -> Path | None:
    if not isinstance(value, (str, bytes, os.PathLike)):
        return None
    text = os.fsdecode(value)
    if text == ":memory:" or text.startswith("file::memory:"):
        return None
    if text.startswith("file:"):
        parsed = urlparse(text)
        text = unquote(parsed.path)
        if os.name == "nt" and text.startswith("/") and len(text) > 2:
            text = text[1:]
    return Path(text).resolve()


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")))
        stream.write("\n")


@contextmanager
def production_sqlite_guard(
    production_database: Path,
    audit_log: Path,
) -> Iterator[None]:
    production = production_database.resolve(strict=True)
    original_connect = sqlite3.connect
    original_dbapi_connect = sqlite3.dbapi2.connect

    def guarded(database: object, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        target = _sqlite_target(database)
        blocked = target is not None and same_file(target, production)
        _append_jsonl(
            audit_log,
            {
                "at": utc_now(),
                "database": None if target is None else str(target),
                "production_identity_match": blocked,
            },
        )
        if blocked:
            raise RuntimeError(
                "P0 production path guard rejected a SQLite connection to "
                f"{production}"
            )
        return original_connect(database, *args, **kwargs)

    sqlite3.connect = guarded
    sqlite3.dbapi2.connect = guarded
    try:
        yield
    finally:
        sqlite3.connect = original_connect
        sqlite3.dbapi2.connect = original_dbapi_connect


_SITECUSTOMIZE = r'''"""Generated P0 SQLite production-path guard."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

_PRODUCTION = Path(os.environ["P0_PRODUCTION_DATABASE"]).resolve(strict=True)
_AUDIT = Path(os.environ["P0_SQLITE_AUDIT_LOG"])
_MARKER = Path(os.environ["P0_GUARD_MARKER"])
_ORIGINAL = sqlite3.connect

_identity = _PRODUCTION.stat()
_MARKER.write_text(json.dumps({
    "status": "guard_activated",
    "production_database": str(_PRODUCTION),
    "production_device": _identity.st_dev,
    "production_inode": _identity.st_ino,
}, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

def _target(value):
    if not isinstance(value, (str, bytes, os.PathLike)):
        return None
    text = os.fsdecode(value)
    if text == ":memory:" or text.startswith("file::memory:"):
        return None
    if text.startswith("file:"):
        parsed = urlparse(text)
        text = unquote(parsed.path)
        if os.name == "nt" and text.startswith("/") and len(text) > 2:
            text = text[1:]
    return Path(text).resolve()

def _same(left, right):
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError):
        return left.resolve() == right.resolve()

def connect(database, *args, **kwargs):
    target = _target(database)
    blocked = target is not None and _same(target, _PRODUCTION)
    _AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(),
            "database": None if target is None else str(target),
            "production_identity_match": blocked,
        }, sort_keys=True, separators=(",", ":")) + "\n")
    if blocked:
        raise RuntimeError(
            "P0 production path guard rejected a SQLite connection to "
            + str(_PRODUCTION)
        )
    return _ORIGINAL(database, *args, **kwargs)

connect._p0_production_guard = True
sqlite3.connect = connect
sqlite3.dbapi2.connect = connect
'''


_USERCUSTOMIZE = r'''"""Fallback loader when another sitecustomize shadows the P0 guard."""
import sqlite3
from pathlib import Path

if not getattr(sqlite3.connect, "_p0_production_guard", False):
    source = Path(__file__).with_name("sitecustomize.py")
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"))
'''


_SQLITE_GUARD_PLUGIN = r'''"""Ensure the SQLite guard is active before pytest collection."""
import sqlite3
from pathlib import Path

if not getattr(sqlite3.connect, "_p0_production_guard", False):
    source = Path(__file__).with_name("sitecustomize.py")
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"))
'''


_PYTEST_PLUGIN = r'''"""Generated exact pytest node outcome recorder."""
import json
import os
from pathlib import Path

_REPORTS = {}
_COLLECTION_FAILURES = []

def pytest_runtest_logreport(report):
    _REPORTS.setdefault(report.nodeid, []).append({
        "phase": report.when,
        "outcome": report.outcome,
        "duration_seconds": report.duration,
        "longrepr": str(report.longrepr) if report.failed else None,
    })

def pytest_collectreport(report):
    if report.failed:
        _COLLECTION_FAILURES.append({
            "nodeid": report.nodeid,
            "status": "failed",
            "phases": [{
                "phase": "collection",
                "outcome": "failed",
                "duration_seconds": 0.0,
                "longrepr": str(report.longrepr),
            }],
        })

def pytest_sessionfinish(session, exitstatus):
    records = []
    for nodeid in sorted(_REPORTS):
        phases = _REPORTS[nodeid]
        outcomes = {phase["outcome"] for phase in phases}
        if "failed" in outcomes:
            status = "failed"
        elif "skipped" in outcomes:
            status = "skipped"
        elif any(phase["phase"] == "call" for phase in phases):
            status = "passed"
        else:
            status = "not_run"
        records.append({"nodeid": nodeid, "status": status, "phases": phases})
    records.extend(_COLLECTION_FAILURES)
    value = {
        "schema": "dota2-p0-pytest-node-results-v1",
        "pytest_exit_status": int(exitstatus),
        "nodes": records,
    }
    path = Path(os.environ["P0_NODE_REPORT"])
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
'''


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    text: bool = False,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        check=check,
        capture_output=True,
        text=text,
    )


def _git_bytes(workspace: Path, *args: str) -> bytes:
    return _run(("git", *args), cwd=workspace).stdout


def _write_canonical(path: Path, value: Any) -> dict[str, Any]:
    payload = canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return file_record(path)


def _guard_environment(
    workspace: Path,
    guard_root: Path,
    production_database: Path,
    audit_log: Path,
    node_report: Path,
    marker: Path,
) -> dict[str, str]:
    guard_root.mkdir(parents=True, exist_ok=True)
    (guard_root / "sitecustomize.py").write_text(
        _SITECUSTOMIZE, encoding="utf-8", newline="\n"
    )
    (guard_root / "usercustomize.py").write_text(
        _USERCUSTOMIZE, encoding="utf-8", newline="\n"
    )
    (guard_root / "p0_sqlite_guard_plugin.py").write_text(
        _SQLITE_GUARD_PLUGIN, encoding="utf-8", newline="\n"
    )
    (guard_root / "p0_node_report_plugin.py").write_text(
        _PYTEST_PLUGIN, encoding="utf-8", newline="\n"
    )
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH")
    pythonpath = [str(guard_root), str(workspace)]
    if existing:
        pythonpath.append(existing)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(pythonpath),
            "P0_PRODUCTION_DATABASE": str(production_database.resolve(strict=True)),
            "P0_SQLITE_AUDIT_LOG": str(audit_log.resolve()),
            "P0_NODE_REPORT": str(node_report.resolve()),
            "P0_GUARD_MARKER": str(marker.resolve()),
            "P0_TEST_DATABASE_ROOT": str((guard_root.parent / "pytest-temp").resolve()),
        }
    )
    return environment


def run_tests(
    workspace: Path,
    output_dir: Path,
    production_database: Path,
    test_files: Sequence[str],
    *,
    label: str,
) -> int:
    workspace = workspace.resolve(strict=True)
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"test evidence output must be fresh/empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    def selector_exists(selector: str) -> bool:
        path_text = selector.split("::", 1)[0]
        return bool(path_text) and (workspace / path_text).is_file()

    missing = [item for item in test_files if not selector_exists(item)]
    selected = [item for item in test_files if selector_exists(item)]
    guard_root = output_dir / "guard"
    audit_log = output_dir / "sqlite-connections.jsonl"
    node_report = output_dir / "pytest-nodes.json"
    guard_marker = output_dir / "guard-activated.json"
    junit = output_dir / "pytest-junit.xml"
    stdout_path = output_dir / "pytest.stdout.log"
    stderr_path = output_dir / "pytest.stderr.log"
    test_database_root = output_dir / "pytest-temp"
    command = [
        sys.executable,
        "-m",
        "pytest",
        f"--basetemp={test_database_root}",
        "-p",
        "no:cacheprovider",
        "-p",
        "p0_sqlite_guard_plugin",
        "-p",
        "p0_node_report_plugin",
        f"--junitxml={junit}",
        *selected,
    ]
    environment = _guard_environment(
        workspace,
        guard_root,
        production_database,
        audit_log,
        node_report,
        guard_marker,
    )
    before = file_identity(production_database)
    started = utc_now()
    started_ns = time.time_ns()
    completed = subprocess.run(
        command,
        cwd=workspace,
        env=environment,
        capture_output=True,
    )
    ended_ns = time.time_ns()
    ended = utc_now()
    stdout_path.write_bytes(completed.stdout)
    stderr_path.write_bytes(completed.stderr)
    after = file_identity(production_database)
    guard_activated = False
    if guard_marker.is_file():
        marker_payload = json.loads(guard_marker.read_text(encoding="utf-8"))
        guard_activated = (
            marker_payload.get("status") == "guard_activated"
            and str(marker_payload.get("production_device")) == before["device"]
            and str(marker_payload.get("production_inode")) == before["inode"]
        )
    attempted_production_connections = 0
    connection_count = 0
    if audit_log.exists():
        for line in audit_log.read_text(encoding="utf-8").splitlines():
            connection_count += 1
            if json.loads(line).get("production_identity_match"):
                attempted_production_connections += 1
    if not node_report.exists():
        _write_canonical(
            node_report,
            {
                "schema": "dota2-p0-pytest-node-results-v1",
                "pytest_exit_status": completed.returncode,
                "nodes": [],
                "unavailable_reason": "pytest plugin did not produce a node report",
            },
        )
    nodes = json.loads(node_report.read_text(encoding="utf-8"))["nodes"]
    counts: dict[str, int] = {}
    for node in nodes:
        status = str(node["status"])
        counts[status] = counts.get(status, 0) + 1
    record = {
        "schema": "dota2-p0-command-record-v1",
        "label": label,
        "command": command,
        "cwd": str(workspace),
        "environment_contract": {
            "PYTHONDONTWRITEBYTECODE": "1",
            "pytest_cacheprovider": "disabled",
            "production_database_guard": str(
                production_database.resolve(strict=True)
            ),
            "secrets": "not recorded",
            "inherited_nonsecret": {
                key: {
                    "status": "set" if key in environment else "unset",
                    "value": environment.get(key),
                }
                for key in TEST_ENVIRONMENT_KEYS
            },
        },
        "started_at": started,
        "ended_at": ended,
        "duration_ns": ended_ns - started_ns,
        "exit_status": completed.returncode,
        "requested_test_files": list(test_files),
        "selected_test_files": selected,
        "missing_test_files": missing,
        "node_status_counts": counts,
        "production_connection_audit": {
            "recorded_connections": connection_count,
            "attempted_production_connections": attempted_production_connections,
            "guard_result": (
                "passed"
                if guard_activated and attempted_production_connections == 0
                else "failed"
            ),
            "guard_activated": guard_activated,
            "production_before": before,
            "production_after": after,
            "identity_stable": (
                before["device"] == after["device"]
                and before["inode"] == after["inode"]
            ),
            "mtime_or_size_changes_may_be_active_writer": True,
        },
        "artifacts": {
            "node_results": file_record(node_report),
            "junit": file_record(junit) if junit.exists() else None,
            "stdout": file_record(stdout_path),
            "stderr": file_record(stderr_path),
            "sqlite_connections": (
                file_record(audit_log) if audit_log.exists() else None
            ),
            "production_path_guard": file_record(guard_root / "sitecustomize.py"),
            "production_path_guard_fallback": file_record(
                guard_root / "usercustomize.py"
            ),
            "pytest_sqlite_guard": file_record(
                guard_root / "p0_sqlite_guard_plugin.py"
            ),
            "pytest_node_recorder": file_record(
                guard_root / "p0_node_report_plugin.py"
            ),
            "guard_activation_marker": (
                file_record(guard_marker) if guard_marker.exists() else None
            ),
        },
    }
    _write_canonical(output_dir / "command-record.json", record)
    print(json.dumps(record["node_status_counts"], sort_keys=True))
    if attempted_production_connections or not guard_activated:
        return 2
    return completed.returncode


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _json_scalar(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 9_007_199_254_740_991:
        return {"decimal_integer": str(value)}
    return value


def _authority_classification(table: str) -> str:
    lowered = table.lower()
    rules = (
        (("settlement", "map_results"), "settlement_authority"),
        (("strategy_decision", "shadow_order", "model_quote"), "decision_order"),
        (("notification", "outbox"), "notification"),
        (("vision",), "vision"),
        (("draft", "rosh"), "draft_model"),
        (("odds", "raybet", "direct_response"), "provider_market"),
        (("event_registry", "strict_live_map"), "mapping_event"),
        (("service_health", "collector_runs"), "operations"),
    )
    for fragments, classification in rules:
        if any(fragment in lowered for fragment in fragments):
            return classification
    return "supporting"


def database_cutoffs(path: Path) -> dict[str, Any]:
    before = file_identity(path)
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            if not str(row[0]).startswith("sqlite_")
        ]
        records: list[dict[str, Any]] = []
        for table in tables:
            columns = list(connection.execute(f"PRAGMA table_info({_quoted(table)})"))
            primary_keys = [
                str(row[1])
                for row in sorted(columns, key=lambda row: int(row[5] or 0))
                if int(row[5] or 0) > 0
            ]
            candidate_columns = [
                str(row[1])
                for row in columns
                if any(
                    str(row[1]).lower() == fragment
                    or str(row[1]).lower().endswith("_" + fragment)
                    for fragment in AUTHORITY_NAME_FRAGMENTS
                )
            ]
            cutoff: dict[str, Any] = {}
            row_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {_quoted(table)}"
                ).fetchone()[0]
            )
            try:
                row = connection.execute(
                    f"SELECT MAX(rowid) FROM {_quoted(table)}"
                ).fetchone()
                cutoff["max_rowid"] = _json_scalar(row[0])
            except sqlite3.DatabaseError:
                cutoff["max_rowid"] = None
            for column in candidate_columns:
                row = connection.execute(
                    f"SELECT MAX({_quoted(column)}) FROM {_quoted(table)}"
                ).fetchone()
                cutoff[column] = _json_scalar(row[0])
            record: dict[str, Any] = {
                "table": table,
                "authority_classification": _authority_classification(table),
                "row_count": row_count,
                "primary_key_columns": primary_keys,
                "row_cutoff": cutoff,
                "cutoff_columns": candidate_columns,
            }
            if table == "draft_deployment_bundles":
                order = ", ".join(_quoted(column) for column in primary_keys) or "rowid"
                frozen_rows = []
                for row in connection.execute(
                    f"SELECT * FROM {_quoted(table)} ORDER BY {order}"
                ):
                    values = {key: _json_scalar(row[key]) for key in row.keys()}
                    frozen_rows.append(
                        {
                            "primary_key": {
                                key: values[key] for key in primary_keys
                            },
                            "row_sha256": sha256_bytes(canonical_bytes(values)),
                            "artifact_fields": {
                                key: value
                                for key, value in values.items()
                                if "hash" in key.lower()
                                or "key" in key.lower()
                                or "version" in key.lower()
                                or "ref" in key.lower()
                            },
                        }
                    )
                record["content_addressed_rows"] = frozen_rows
            records.append(record)
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        data_version = connection.execute("PRAGMA data_version").fetchone()[0]
        connection.rollback()
    finally:
        connection.close()
    after = file_identity(path)
    return {
        "access_contract": "sqlite URI mode=ro; PRAGMA query_only=ON; snapshot transaction",
        "captured_at": utc_now(),
        "identity_before": before,
        "identity_after": after,
        "stable_file_identity": (
            before["device"] == after["device"]
            and before["inode"] == after["inode"]
        ),
        "mtime_or_size_changes_may_be_active_writer": True,
        "schema_version": schema_version,
        "data_version_at_connection": data_version,
        "tables": records,
    }


def _tree_records(root: Path, *, append_only: bool = False) -> dict[str, Any]:
    if not root.exists():
        return {
            "status": "unavailable",
            "reason": f"path does not exist: {root}",
            "root": str(root.resolve()),
            "files": [],
        }
    frozen_at = utc_now()
    paths = sorted(path for path in root.rglob("*") if path.is_file())
    if not paths:
        return {
            "status": "unavailable",
            "reason": "root exists but contains no evidence files at cutoff",
            "root": str(root.resolve()),
            "listing_frozen_at": frozen_at,
            "content_contract": (
                "append_only_prefix_cutoff" if append_only else "immutable_object"
            ),
            "file_count": 0,
            "total_bytes_at_cutoff": 0,
            "files": [],
        }
    records = []
    for path in paths:
        display = path.relative_to(root).as_posix()
        records.append(
            prefix_file_record(path, display_path=display)
            if append_only
            else file_record(path, display_path=display)
        )
    return {
        "status": "available",
        "root": str(root.resolve()),
        "listing_frozen_at": frozen_at,
        "content_contract": (
            "append_only_prefix_cutoff" if append_only else "immutable_object"
        ),
        "file_count": len(records),
        "total_bytes_at_cutoff": sum(
            item.get("bytes", item.get("cutoff_bytes", 0)) for item in records
        ),
        "files": records,
    }


def _dependency_versions() -> dict[str, Any]:
    packages: list[dict[str, Any]] = []
    for name in REQUIRED_PACKAGES:
        try:
            version = importlib.metadata.version(name)
            packages.append({"name": name, "status": "available", "version": version})
        except importlib.metadata.PackageNotFoundError:
            packages.append(
                {"name": name, "status": "unavailable", "reason": "not installed"}
            )
    try:
        ffmpeg = subprocess.run(
            ("ffmpeg", "-version"), check=False, capture_output=True, text=True
        )
        first_line = ffmpeg.stdout.splitlines()[0] if ffmpeg.stdout else None
        ffmpeg_record = {
            "status": "available" if ffmpeg.returncode == 0 else "unavailable",
            "version_line": first_line,
            "exit_status": ffmpeg.returncode,
        }
    except FileNotFoundError:
        ffmpeg_record = {"status": "unavailable", "reason": "ffmpeg not on PATH"}
    try:
        import cv2

        opencv = {"status": "available", "version": cv2.__version__}
    except ImportError as error:
        opencv = {"status": "unavailable", "reason": str(error)}
    return {
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
        },
        "platform": platform.platform(),
        "packages": packages,
        "opencv_import": opencv,
        "ffmpeg": ffmpeg_record,
        "installed_distributions": sorted(
            (
                {
                    "name": distribution.metadata.get("Name", distribution.name),
                    "version": distribution.version,
                }
                for distribution in importlib.metadata.distributions()
            ),
            key=lambda item: str(item["name"]).lower(),
        ),
    }


def _classify_untracked(relative: str) -> str | None:
    path = Path(relative)
    parts = path.parts
    if not parts:
        return None
    if parts[0] == "tests":
        return "test"
    if parts[0] == "docs" or (len(parts) == 1 and path.suffix.lower() == ".md"):
        return "formal_doc"
    if parts[0] in {"dogfood-output", "data", "node_modules"}:
        return None
    if path.suffix.lower() in SOURCE_SUFFIXES:
        return "source"
    return None


def workspace_identity(workspace: Path, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    head = _git_bytes(workspace, "rev-parse", "HEAD").decode().strip()
    status_z = _git_bytes(
        workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    )
    status_records = [
        item.decode("utf-8", "surrogateescape")
        for item in status_z.split(b"\0")
        if item
    ]
    staged = _git_bytes(workspace, "diff", "--cached", "--binary", "--no-ext-diff")
    unstaged = _git_bytes(workspace, "diff", "--binary", "--no-ext-diff")
    staged_path = output_dir / "workspace-staged.diff"
    unstaged_path = output_dir / "workspace-unstaged.diff"
    status_path = output_dir / "workspace-status.porcelain-v1-z"
    staged_path.write_bytes(staged)
    unstaged_path.write_bytes(unstaged)
    status_path.write_bytes(status_z)
    untracked_raw = _git_bytes(
        workspace, "ls-files", "--others", "--exclude-standard", "-z"
    )
    untracked_paths = sorted(
        item.decode("utf-8", "surrogateescape")
        for item in untracked_raw.split(b"\0")
        if item
    )
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for relative in untracked_paths:
        category = _classify_untracked(relative)
        path = workspace / relative
        if category is None:
            excluded.append(
                {"path": relative, "reason": "not source/test/formal-doc scope"}
            )
            continue
        if not path.is_file():
            selected.append(
                {"path": relative, "category": category, "status": "unavailable"}
            )
            continue
        selected.append(
            {
                **file_record(path, display_path=relative.replace("\\", "/")),
                "category": category,
                "status": "available",
            }
        )
    return {
        "root": str(workspace.resolve()),
        "head": head,
        "expected_plan_head": BASELINE_COMMIT,
        "head_matches_plan": head.startswith(BASELINE_COMMIT),
        "status": {
            "format": "git status --porcelain=v1 -z --untracked-files=all",
            "records": status_records,
            "artifact": file_record(status_path),
        },
        "tracked_diffs": {
            "staged": file_record(staged_path),
            "unstaged": file_record(unstaged_path),
        },
        "untracked_source_test_formal_docs": selected,
        "untracked_out_of_scope": excluded,
    }


def _load_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        records.append(
            {
                "record": json.loads(path.read_text(encoding="utf-8")),
                "artifact": file_record(path),
            }
        )
    return records


def clean_worktree_identity(worktree: Path) -> dict[str, Any]:
    if not worktree.is_dir():
        return {
            "status": "unavailable",
            "root": str(worktree.resolve()),
            "reason": "clean baseline worktree does not exist",
        }
    head = _git_bytes(worktree, "rev-parse", "HEAD").decode().strip()
    status = _git_bytes(
        worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    )
    return {
        "status": "prepared" if not status and head.startswith(BASELINE_COMMIT) else "invalid",
        "root": str(worktree.resolve()),
        "head": head,
        "expected_head": BASELINE_COMMIT,
        "head_matches": head.startswith(BASELINE_COMMIT),
        "status_porcelain_v1_z_base64": base64.b64encode(status).decode("ascii"),
        "status_sha256": sha256_bytes(status),
        "is_clean": not status,
    }


def _command_node_results(command_entry: dict[str, Any]) -> dict[str, str]:
    artifact = command_entry["record"].get("artifacts", {}).get("node_results")
    if not artifact:
        return {}
    payload = json.loads(Path(artifact["path"]).read_text(encoding="utf-8"))
    return {str(node["nodeid"]): str(node["status"]) for node in payload["nodes"]}


def _recorded_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _select_exact_test_record(
    commands: list[dict[str, Any]],
    *,
    root: Path,
    test_files: Sequence[str],
) -> dict[str, Any] | None:
    expected_files = tuple(test_files)
    expected_root = root.resolve()
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for entry in commands:
        record = entry["record"]
        try:
            record_root = Path(str(record.get("cwd"))).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        started = _recorded_utc(record.get("started_at"))
        ended = _recorded_utc(record.get("ended_at"))
        command = record.get("command")
        if (
            record.get("schema") != "dota2-p0-command-record-v1"
            or record_root != expected_root
            or tuple(record.get("requested_test_files", [])) != expected_files
            or tuple(record.get("selected_test_files", [])) != expected_files
            or started is None
            or ended is None
            or started > ended
            or not isinstance(command, list)
            or command[1:3] != ["-m", "pytest"]
            or tuple(command[-len(expected_files):]) != expected_files
        ):
            continue
        candidates.append((ended, entry))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _command_identity(entry: dict[str, Any]) -> dict[str, Any]:
    record = entry["record"]
    return {
        "label": record.get("label"),
        "cwd": record.get("cwd"),
        "started_at": _recorded_utc(record.get("started_at")).isoformat(),
        "ended_at": _recorded_utc(record.get("ended_at")).isoformat(),
        "command_sha256": sha256_bytes(canonical_bytes(record.get("command"))),
        "record_sha256": entry.get("artifact", {}).get("sha256"),
    }


def _node_status_counts(nodes: dict[str, str]) -> dict[str, int]:
    statuses = sorted(set(nodes.values()))
    return {status: sum(value == status for value in nodes.values()) for status in statuses}


def node_comparison(
    commands: list[dict[str, Any]],
    *,
    workspace_root: Path,
    clean_root: Path,
) -> dict[str, Any]:
    current = _select_exact_test_record(
        commands,
        root=workspace_root,
        test_files=CRITICAL_TESTS,
    )
    clean = _select_exact_test_record(
        commands,
        root=clean_root,
        test_files=BASELINE_COMMON_TESTS,
    )
    if current is None or clean is None:
        return {
            "status": "unavailable",
            "reason": (
                "exact current 12-file and clean 8f6d4cd common 10-file command "
                "records are required for their respective workspace roots"
            ),
        }
    current_nodes = _command_node_results(current)
    clean_nodes = _command_node_results(clean)
    common = sorted(set(current_nodes).intersection(clean_nodes))
    current_record = current["record"]
    clean_record = clean["record"]
    coverage_errors: list[str] = []
    if set(current_record.get("requested_test_files", [])) != set(CRITICAL_TESTS):
        coverage_errors.append("current requested files are not the exact 12-file set")
    if set(current_record.get("selected_test_files", [])) != set(CRITICAL_TESTS):
        coverage_errors.append("current selected files are not the exact 12-file set")
    if set(clean_record.get("requested_test_files", [])) != set(BASELINE_COMMON_TESTS):
        coverage_errors.append("clean requested files are not the exact 10-file common set")
    if set(clean_record.get("selected_test_files", [])) != set(BASELINE_COMMON_TESTS):
        coverage_errors.append("clean selected files are not the exact 10-file common set")
    if set(clean_nodes).difference(current_nodes):
        coverage_errors.append("clean contains nodes absent from current results")
    if len(common) != len(clean_nodes):
        coverage_errors.append("common nodes do not cover all clean nodes")
    current_counts = _node_status_counts(current_nodes)
    clean_counts = _node_status_counts(clean_nodes)
    if current_record.get("node_status_counts") != current_counts:
        coverage_errors.append("current recorded node counts do not match node artifact")
    if clean_record.get("node_status_counts") != clean_counts:
        coverage_errors.append("clean recorded node counts do not match node artifact")
    return {
        "status": "comparable" if common and not coverage_errors else "unavailable",
        "coverage_errors": coverage_errors,
        "current_command_identity": _command_identity(current),
        "clean_command_identity": _command_identity(clean),
        "clean_baseline_commit": BASELINE_COMMIT,
        "current_node_status_counts": current_counts,
        "clean_node_status_counts": clean_counts,
        "current_node_count": len(current_nodes),
        "clean_node_count": len(clean_nodes),
        "common_node_count": len(common),
        "common_nodes": [
            {
                "nodeid": nodeid,
                "current": current_nodes[nodeid],
                "clean": clean_nodes[nodeid],
            }
            for nodeid in common
        ],
        "current_only_nodes": sorted(set(current_nodes).difference(clean_nodes)),
        "clean_only_nodes": sorted(set(clean_nodes).difference(current_nodes)),
        "current_failures": sorted(
            node for node, status in current_nodes.items() if status == "failed"
        ),
        "clean_failures": sorted(
            node for node, status in clean_nodes.items() if status == "failed"
        ),
    }


def _service_report_evidence(
    production_database: Path,
    output_dir: Path,
) -> dict[str, Any]:
    path = production_database.parent / "live_betting" / "service_report.json"
    if not path.is_file():
        return {"status": "unavailable", "reason": f"missing: {path}"}
    try:
        source_bytes = path.read_bytes()
        payload = json.loads(source_bytes)
    except (OSError, json.JSONDecodeError) as error:
        return {"status": "unavailable", "reason": str(error)}

    deployment_keys: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"deployment_key", "draft_deployment_key"} and isinstance(item, str):
                    deployment_keys.add(item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    extracted = {
        "schema": "dota2-p0-service-report-extract-v1",
        "source_path": str(path.resolve()),
        "source_bytes_at_cutoff": len(source_bytes),
        "source_sha256_at_cutoff": sha256_bytes(source_bytes),
        "captured_at": utc_now(),
        "draft_deployment_keys": sorted(deployment_keys),
    }
    extract_path = output_dir / "service-report-extract.json"
    _write_canonical(extract_path, extracted)
    return {
        "status": "available",
        "source_ref": {
            "path": str(path.resolve()),
            "bytes_at_cutoff": len(source_bytes),
            "sha256_at_cutoff": sha256_bytes(source_bytes),
        },
        "extracted_artifact": file_record(extract_path),
        "draft_deployment_keys": sorted(deployment_keys),
        "draft_binding_status": "available" if deployment_keys else "unavailable",
        "draft_binding_reason": None if deployment_keys else "no deployment key in report",
    }


def _derive_blockers(manifest: dict[str, Any]) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []

    def add(code: str, reason: str) -> None:
        blockers.append({"code": code, "reason": reason})

    def detail(value: object) -> str:
        if isinstance(value, (dict, list)):
            return canonical_bytes(value).decode("utf-8")
        return str(value)

    if not manifest["commands"]:
        add("commands_missing", "no exact command records are bound")
    for entry in manifest["commands"]:
        record = entry["record"]
        if record.get("missing_test_files"):
            add("critical_tests_missing", f"{record['label']}: {record['missing_test_files']}")
        counts = record.get("node_status_counts", {})
        if counts.get("skipped") or counts.get("not_run"):
            add("critical_nodes_not_executed", f"{record['label']}: {detail(counts)}")
        if record.get("production_connection_audit", {}).get("guard_result") != "passed":
            add("production_path_guard_failed", str(record.get("label")))
    for package in manifest["dependencies"]["packages"]:
        if package.get("status") != "available":
            add("dependency_unavailable", str(package["name"]))
    for name in ("opencv_import", "ffmpeg"):
        if manifest["dependencies"][name].get("status") != "available":
            add("dependency_unavailable", name)
    for name in ("raw_v2", "vision_jsonl", "vision_frames"):
        if manifest["evidence"][name].get("status") != "available":
            add("required_evidence_unavailable", name)
    for name, evidence in sorted(manifest["evidence"]["providers"].items()):
        if str(evidence.get("status", "")).startswith("unavailable"):
            add("provider_evidence_unavailable", name)
    for name in ("canonical_evaluator", "policy"):
        if manifest["evidence"][name].get("status") != "available":
            add("strategy_contract_unavailable", name)
    if manifest["evidence"]["service_report"].get("draft_binding_status") != "available":
        add("draft_deployment_unbound", "active draft deployment key/hash is unavailable")
    else:
        draft_table = next(
            (
                table
                for table in manifest["production_database"]["tables"]
                if table["table"] == "draft_deployment_bundles"
            ),
            None,
        )
        frozen_keys = {
            str(row.get("primary_key", {}).get("deployment_key"))
            for row in (draft_table or {}).get("content_addressed_rows", [])
        }
        active_keys = set(
            manifest["evidence"]["service_report"].get("draft_deployment_keys", [])
        )
        if not active_keys.issubset(frozen_keys):
            add(
                "draft_deployment_hash_unavailable",
                f"active={sorted(active_keys)} frozen={sorted(frozen_keys)}",
            )
    if manifest["clean_baseline_worktree"].get("status") != "prepared":
        add("clean_worktree_not_prepared", detail(manifest["clean_baseline_worktree"]))
    if manifest["test_node_comparison"].get("status") != "comparable":
        add("test_node_comparison_unavailable", detail(manifest["test_node_comparison"]))
    if manifest["rollback"].get("status") != "fixture_ready":
        add("rollback_fixture_not_ready", str(manifest["rollback"].get("status")))
    for issue in manifest["strategy_contract"]["validation_issues"]:
        add(str(issue["code"]), str(issue["reason"]))
    for role in manifest["operational_roles"]:
        if role.get("status") != "bound":
            add(
                "operational_role_unbound",
                f"{role['role']}: {detail(role.get('validation_issues', []))}",
            )
        if role.get("person_identity_asserted") is not True:
            add(
                "operational_role_person_identity_unasserted",
                f"{role['role']}: a named human identity is required",
            )
    if manifest["page_acceptance"].get("status") != "recorded":
        add(
            "page_acceptance_unbound",
            detail(manifest["page_acceptance"].get("validation_issues", [])),
        )
    add("independent_verification_missing", "independent verifier signature is absent")
    return blockers


def generate_manifest(args: argparse.Namespace) -> int:
    generation_started_at = utc_now()
    workspace = args.workspace.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    production_database = args.production_database.resolve(strict=True)
    legacy_manifest = production_database.parent / "restore-manifest.json"
    commands = _load_records(args.command_record)
    service_report = _service_report_evidence(production_database, output_dir)
    clean_baseline = clean_worktree_identity(args.clean_worktree)
    strategy_contract = _strategy_contract_binding(args)
    strategy_evidence = _strategy_contract_evidence(strategy_contract)
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "serialization": SERIALIZATION,
        "created_at": utc_now(),
        "status": "incomplete",
        "workspace": workspace_identity(workspace, output_dir),
        "dependencies": _dependency_versions(),
        "commands": commands,
        "production_database": database_cutoffs(production_database),
        "clean_baseline_worktree": clean_baseline,
        "test_node_comparison": node_comparison(
            commands,
            workspace_root=workspace,
            clean_root=args.clean_worktree,
        ),
        "critical_test_findings": {
            "gate_owner": "P1/M1",
            "p0_effect": "recorded failures do not block P0 when coverage is complete",
            "current_failure_count": 0,
            "clean_failure_count": 0,
        },
        "strategy_contract": strategy_contract,
        "evidence": {
            "raw_v2": _tree_records(args.raw_root),
            "vision_jsonl": _tree_records(args.vision_jsonl_root, append_only=True),
            "vision_frames": _tree_records(args.vision_frame_root),
            "service_report": service_report,
            "draft_deployment": {
                "status": (
                    "available"
                    if service_report.get("draft_binding_status") == "available"
                    else "unavailable"
                ),
                "authority_table": "draft_deployment_bundles",
                "active_deployment_keys": service_report.get("draft_deployment_keys", []),
                "note": "exact table rows remain frozen by production table cutoffs",
            },
            **strategy_evidence,
            "providers": {
                "raybet": {
                    "status": "available_via_raw_v2_and_database_cutoff",
                    "first_usable_time_source": "database authority cutoffs",
                },
                "opendota": {
                    "status": "unavailable",
                    "reason": "no explicit secret-safe P0 provider artifact root was supplied",
                },
                "hls_ffmpeg": {
                    "status": "unavailable",
                    "reason": "no immutable response/audit hash was bound for P0",
                },
                "stratz": {
                    "status": "unavailable",
                    "reason": "no immutable response/audit hash was bound for P0",
                },
            },
            "legacy_restore_manifest": (
                {
                    **file_record(legacy_manifest),
                    "classification": "legacy_cutover_evidence_only",
                    "adr_0011_root_manifest": False,
                    "reason": (
                        "does not cover current workspace diffs/untracked files, "
                        "commands, current cutoffs, evaluator/policy, or roles"
                    ),
                }
                if legacy_manifest.is_file()
                else {
                    "status": "unavailable",
                    "reason": "restore-manifest.json not present beside production database",
                }
            ),
        },
        "rollback": (
            {
                **json.loads(args.rollback_summary.read_text(encoding="utf-8")),
                "summary_artifact": file_record(args.rollback_summary),
            }
            if args.rollback_summary
            else {
                "status": "unverified",
                "reason": "rollback fixture summary not supplied",
            }
        ),
        "operational_roles": _operational_role_bindings(args),
        "page_acceptance": _page_acceptance_binding(args),
        "p0_blockers": [],
        "p1_blockers": [],
        "manifest_generation": {
            "command": [sys.executable, "-m", "scripts.p0_evidence", *sys.argv[1:]],
            "cwd": str(Path.cwd().resolve()),
            "environment_contract": {
                "secrets": "not recorded",
                "output_is_ignored_audit_root": True,
                "production_database_access": "mode=ro/query_only snapshot only",
            },
            "started_at": generation_started_at,
            "ended_at": utc_now(),
            "exit_status": 0,
        },
    }
    comparison = manifest["test_node_comparison"]
    manifest["critical_test_findings"] = {
        **manifest["critical_test_findings"],
        "current_pass_count": comparison.get("current_node_status_counts", {}).get(
            "passed", 0
        ),
        "current_failure_count": len(comparison.get("current_failures", [])),
        "clean_pass_count": comparison.get("clean_node_status_counts", {}).get(
            "passed", 0
        ),
        "clean_failure_count": len(comparison.get("clean_failures", [])),
        "current_failure_nodes": comparison.get("current_failures", []),
        "clean_failure_nodes": comparison.get("clean_failures", []),
    }
    if comparison.get("current_failures"):
        manifest["p1_blockers"].append(
            {
                "code": "production_critical_tests_failed",
                "reason": f"{len(comparison['current_failures'])} current critical nodes failed",
            }
        )
    manifest["p0_blockers"] = _derive_blockers(manifest)
    manifest["status"] = "ready" if not manifest["p0_blockers"] else "not_ready"
    manifest_path = output_dir / "workspace-evidence-manifest.v1.json"
    manifest_bytes = canonical_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    digest = sha256_bytes(manifest_bytes)
    sidecar = manifest_path.with_suffix(manifest_path.suffix + ".sha256")
    sidecar.write_text(f"{digest}  {manifest_path.name}\n", encoding="ascii")
    print(json.dumps({"manifest": str(manifest_path), "sha256": digest}))
    return 0


def refresh_manifest(source_manifest: Path, output_dir: Path) -> int:
    """Refresh derived P0 evidence without reconnecting to the frozen database."""
    generation_started_at = utc_now()
    source_manifest = source_manifest.resolve(strict=True)
    source_sidecar = source_manifest.with_suffix(source_manifest.suffix + ".sha256")
    source_bytes = source_manifest.read_bytes()
    source_digest = sha256_bytes(source_bytes)
    source = json.loads(
        source_bytes,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant: {value}")
        ),
    )
    if source.get("schema") != SCHEMA:
        raise ValueError("source manifest schema is unsupported")
    if canonical_bytes(source) != source_bytes:
        raise ValueError("source manifest is not canonical RFC8785 serialization")
    if not source_sidecar.is_file():
        raise ValueError("source manifest self-hash sidecar is missing")
    sidecar_tokens = source_sidecar.read_text(encoding="ascii").split()
    if not sidecar_tokens or sidecar_tokens[0] != source_digest:
        raise ValueError("source manifest self-hash mismatch")

    expected_contract = _strategy_contract_binding(
        argparse.Namespace(**source["strategy_contract"])
    )
    if source["strategy_contract"] != expected_contract:
        raise ValueError("source strategy contract is malformed or inconsistent")

    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"manifest refresh output must be fresh/empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = source
    manifest["created_at"] = utc_now()
    manifest["workspace"] = workspace_identity(
        Path(manifest["workspace"]["root"]).resolve(strict=True), output_dir
    )
    manifest["evidence"].update(_strategy_contract_evidence(expected_contract))
    extracted = manifest["evidence"]["service_report"].get("extracted_artifact")
    if extracted is not None:
        error = _verify_file_record(extracted)
        if error:
            raise ValueError(error)
        refreshed_extract = output_dir / "service-report-extract.json"
        shutil.copy2(Path(extracted["path"]), refreshed_extract)
        manifest["evidence"]["service_report"]["extracted_artifact"] = file_record(
            refreshed_extract
        )
    manifest["p0_blockers"] = _derive_blockers(manifest)
    manifest["status"] = "ready" if not manifest["p0_blockers"] else "not_ready"
    manifest["manifest_generation"] = {
        "command": [sys.executable, "-m", "scripts.p0_evidence", *sys.argv[1:]],
        "cwd": str(Path.cwd().resolve()),
        "source_manifest": file_record(source_manifest),
        "environment_contract": {
            "secrets": "not recorded",
            "output_is_ignored_audit_root": True,
            "production_database_access": "none; frozen cutoff inherited from source manifest",
        },
        "started_at": generation_started_at,
        "ended_at": utc_now(),
        "exit_status": 0,
    }
    manifest_path = output_dir / "workspace-evidence-manifest.v1.json"
    manifest_bytes = canonical_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    digest = sha256_bytes(manifest_bytes)
    sidecar = manifest_path.with_suffix(manifest_path.suffix + ".sha256")
    sidecar.write_text(f"{digest}  {manifest_path.name}\n", encoding="ascii")
    print(json.dumps({"manifest": str(manifest_path), "sha256": digest}))
    return 0


def _verify_file_record(record: dict[str, Any], base: Path | None = None) -> str | None:
    path = Path(str(record["path"]))
    if base is not None and not path.is_absolute():
        path = base / path
    if not path.is_file():
        return f"missing file: {path}"
    if path.stat().st_size != int(record["bytes"]):
        return f"size mismatch: {path}"
    if sha256_file(path) != record["sha256"]:
        return f"sha256 mismatch: {path}"
    return None


def _verify_prefix_record(record: dict[str, Any], base: Path) -> str | None:
    path = base / str(record["path"])
    if not path.is_file():
        return f"missing file: {path}"
    cutoff = int(record["cutoff_bytes"])
    if path.stat().st_size < cutoff:
        return f"append-only file truncated below cutoff: {path}"
    digest = hashlib.sha256()
    remaining = cutoff
    with path.open("rb") as stream:
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                return f"append-only file truncated during verify: {path}"
            digest.update(chunk)
            remaining -= len(chunk)
    if digest.hexdigest() != record["sha256_through_cutoff"]:
        return f"prefix sha256 mismatch: {path}"
    return None


def _verify_referenced_artifacts(value: Any, errors: list[str]) -> None:
    if isinstance(value, dict):
        if {
            "root",
            "files",
            "content_contract",
        }.issubset(value) and isinstance(value["files"], list):
            root = Path(str(value["root"]))
            for record in value["files"]:
                error = (
                    _verify_prefix_record(record, root)
                    if value["content_contract"] == "append_only_prefix_cutoff"
                    else _verify_file_record(record, root)
                )
                if error:
                    errors.append(error)
        if {"path", "bytes", "sha256"}.issubset(value):
            artifact_path = Path(str(value["path"]))
            if artifact_path.is_absolute():
                error = _verify_file_record(value)
                if error:
                    errors.append(error)
        for child in value.values():
            _verify_referenced_artifacts(child, errors)
    elif isinstance(value, list):
        for child in value:
            _verify_referenced_artifacts(child, errors)


def _verify_manifest_inner(path: Path) -> int:
    path = path.resolve(strict=True)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    errors: list[str] = []
    raw = path.read_bytes()
    try:
        manifest = json.loads(
            raw,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {value}")
            ),
        )
    except (json.JSONDecodeError, ValueError) as error:
        print(f"invalid JSON: {error}", file=sys.stderr)
        return 1
    if manifest.get("schema") != SCHEMA:
        errors.append("unsupported manifest schema")
    required_sections = {
        "commands",
        "evidence",
        "operational_roles",
        "page_acceptance",
        "p0_blockers",
        "production_database",
        "status",
        "strategy_contract",
        "workspace",
    }
    missing_sections = sorted(required_sections.difference(manifest))
    if missing_sections:
        errors.append(
            "missing required manifest sections: " + ", ".join(missing_sections)
        )
    if canonical_bytes(manifest) != raw:
        errors.append("manifest bytes are not canonical RFC8785 serialization")
    actual = sha256_bytes(raw)
    if not sidecar.is_file():
        errors.append(f"missing self-hash sidecar: {sidecar}")
    else:
        tokens = sidecar.read_text(encoding="ascii").split()
        if not tokens:
            errors.append("manifest self-hash sidecar is empty")
            expected = ""
        else:
            expected = tokens[0]
        if actual != expected:
            errors.append("manifest self-hash mismatch")
    if missing_sections or manifest.get("schema") != SCHEMA:
        result = {
            "manifest": str(path),
            "sha256": actual,
            "integrity_valid": False,
            "p0_ready": False,
            "errors": errors,
        }
        print(json.dumps(result, sort_keys=True))
        return 1
    expected_contract = _strategy_contract_binding(
        argparse.Namespace(**manifest["strategy_contract"])
    )
    if manifest["strategy_contract"] != expected_contract:
        errors.append("strategy contract binding is malformed or inconsistent")
    expected_strategy_evidence = _strategy_contract_evidence(manifest["strategy_contract"])
    for name, expected_evidence in expected_strategy_evidence.items():
        if manifest["evidence"].get(name) != expected_evidence:
            errors.append(f"{name} evidence does not match strategy contract binding")
    role_values = {
        str(item.get("role")): item for item in manifest["operational_roles"]
    }
    if set(role_values) != set(OPERATIONAL_ROLES) or len(role_values) != len(
        manifest["operational_roles"]
    ):
        errors.append("operational role set is malformed")
    else:
        role_namespace = argparse.Namespace(
            roles_effective_at=next(iter(role_values.values())).get("effective_at"),
            **{role: role_values[role].get("account") for role in OPERATIONAL_ROLES},
        )
        if manifest["operational_roles"] != _operational_role_bindings(role_namespace):
            errors.append("operational role bindings are malformed or inconsistent")
    expected_page_acceptance = _page_acceptance_binding(
        argparse.Namespace(
            user_page_acceptance=manifest["page_acceptance"].get("accepted_by"),
            page_acceptance_effective_at=manifest["page_acceptance"].get(
                "effective_at"
            ),
        )
    )
    if manifest["page_acceptance"] != expected_page_acceptance:
        errors.append("page acceptance binding is malformed or inconsistent")
    expected_blockers = _derive_blockers(manifest)
    if manifest["p0_blockers"] != expected_blockers:
        errors.append("P0 blockers do not match manifest bindings")
    expected_status = "ready" if not expected_blockers else "not_ready"
    if manifest["status"] != expected_status:
        errors.append("P0 status does not match derived blockers")
    _verify_referenced_artifacts(manifest, errors)
    workspace = Path(manifest["workspace"]["root"])
    for command in manifest["commands"]:
        error = _verify_file_record(command["artifact"])
        if error:
            errors.append(error)
        for artifact in command["record"].get("artifacts", {}).values():
            if artifact is None:
                continue
            error = _verify_file_record(artifact)
            if error:
                errors.append(error)
    for name in ("raw_v2", "vision_jsonl", "vision_frames"):
        tree = manifest["evidence"][name]
        if tree.get("status") != "available":
            continue
        root = Path(tree["root"])
        for record in tree["files"]:
            error = (
                _verify_prefix_record(record, root)
                if tree.get("content_contract") == "append_only_prefix_cutoff"
                else _verify_file_record(record, root)
            )
            if error:
                errors.append(error)
    identity = manifest["workspace"]
    current_head = _git_bytes(workspace, "rev-parse", "HEAD").decode().strip()
    if current_head != identity["head"]:
        errors.append("workspace HEAD drift")
    status = _git_bytes(
        workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    )
    if sha256_bytes(status) != identity["status"]["artifact"]["sha256"]:
        errors.append("workspace status drift")
    current_diffs = {
        "staged": _git_bytes(workspace, "diff", "--cached", "--binary", "--no-ext-diff"),
        "unstaged": _git_bytes(workspace, "diff", "--binary", "--no-ext-diff"),
    }
    for name, content in current_diffs.items():
        record = identity["tracked_diffs"][name]
        error = _verify_file_record(record)
        if error:
            errors.append(error)
        if sha256_bytes(content) != record["sha256"] or len(content) != record["bytes"]:
            errors.append(f"workspace {name} tracked diff drift")
    error = _verify_file_record(identity["status"]["artifact"])
    if error:
        errors.append(error)
    for record in identity["untracked_source_test_formal_docs"]:
        if record.get("status") != "available":
            errors.append(f"untracked required file unavailable: {record['path']}")
            continue
        error = _verify_file_record(record, workspace)
        if error:
            errors.append(error)
    database = manifest["production_database"]
    current_identity = file_identity(Path(database["identity_before"]["resolved_path"]))
    frozen_identity = database["identity_before"]
    if (
        current_identity["device"] != frozen_identity["device"]
        or current_identity["inode"] != frozen_identity["inode"]
    ):
        errors.append("production database stable identity drift")
    result = {
        "manifest": str(path),
        "sha256": actual,
        "integrity_valid": not errors,
        "p0_status": manifest.get("status", "unknown"),
        "p0_ready": not errors and manifest.get("status") == "ready" and not manifest.get("p0_blockers"),
        "p0_blocker_count": len(manifest.get("p0_blockers", [])),
        "errors": sorted(set(errors)),
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if not errors else 1


def verify_manifest(path: Path) -> int:
    try:
        return _verify_manifest_inner(path)
    except (KeyError, TypeError, ValueError, IndexError, OSError, sqlite3.Error) as error:
        result = {
            "manifest": str(path),
            "integrity_valid": False,
            "p0_ready": False,
            "errors": [f"malformed manifest: {type(error).__name__}: {error}"],
        }
        print(json.dumps(result, sort_keys=True))
        return 1


def build_rollback_fixture(
    output_dir: Path,
    production_database: Path,
    *,
    seed_settlement_review: bool = True,
) -> int:
    output_dir = output_dir.resolve()
    production_database = production_database.resolve(strict=True)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"rollback fixture output must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    fixture_database = output_dir / "rollback-fixture.db"
    if same_file(fixture_database, production_database):
        raise ValueError("rollback fixture path resolves to production database")
    connection_audit = output_dir / "sqlite-connections.jsonl"
    before = file_identity(production_database)
    started = utc_now()
    from live_betting.database_protocol import (
        prepare_database,
        verify_prepared_database,
    )
    from live_betting.markets import normalized_state_hash
    from live_betting.models import OddsSnapshot
    from live_betting.notifications import (
        EVENT_MONITOR_ALERT,
        MONITOR_TEMPLATE_VERSION,
        enqueue,
    )
    from live_betting.report import build_report
    from live_betting.vision_frame_registry import (
        publish_vision_frame_bytes,
        read_registered_vision_frame_bytes,
        relocate_vision_frame_artifacts,
    )
    from tests.test_notification_outbox import NOW, NotificationOutboxTests

    raw_root = output_dir / "raw"
    runtime_raybet_root = output_dir / "live_betting" / "raw-v2"
    vision_root = output_dir / "vision-frames"
    case = NotificationOutboxTests("test_logical_key_and_message_id_are_idempotent")
    with production_sqlite_guard(production_database, connection_audit):
        case.setUp()
        try:
            order = case._order()
            if not case.store.insert_map_order(
                order,
                1,
                strict_mapping_id=case.strict_mapping_id,
                draft_authority=case.draft_authority,
            ):
                raise RuntimeError("current code could not write representative order/attempt")

            non_direct_at = NOW + timedelta(seconds=1)
            non_direct_successor = OddsSnapshot(
                order.raybet_match_id,
                order.odds_id,
                order.signal_odds_group_id,
                non_direct_at,
                order.signal_price * 0.9,
                1,
                order.market,
            )
            case._store_odds_observation(
                source="browser",
                observation_key="non-direct-successor",
                source_event_id=None,
                raybet_match_id=order.raybet_match_id,
                observed_at=non_direct_at,
                normalized_state_hash=normalized_state_hash(
                    [non_direct_successor]
                ),
                snapshots=[non_direct_successor],
            )
            current_watermark = case.store.processed_transport_watermark(
                order.raybet_match_id, as_of=non_direct_at
            )
            if current_watermark != order.signal_transport_at:
                raise RuntimeError(
                    "current writer did not isolate direct transport watermark"
                )
            if case.store.process_pending_successor(
                order, watermark=current_watermark
            ) is not None:
                raise RuntimeError(
                    "current writer consumed a non-direct successor"
                )

            if not enqueue(
                case.store.connection,
                order_key=order.order_key,
                event_type=EVENT_MONITOR_ALERT,
                payload=case.operational_payload(EVENT_MONITOR_ALERT),
                recipient="p0-fixture@example.invalid",
                stats_cutoff_at=NOW,
                created_at=NOW,
                template_version=MONITOR_TEMPLATE_VERSION,
            ):
                raise RuntimeError("current code could not write representative outbox")
            if seed_settlement_review:
                if not case.store.insert_settlement_review(
                    order.order_key,
                    settled_at=NOW.replace(hour=2),
                    evidence_ref=f"p0-fixture-review:{order.order_key}",
                    reason="p0_fixture_authoritative_settlement_not_exercised",
                    actor="p0_fixture_builder",
                ):
                    raise RuntimeError(
                        "current code could not write settlement review shape"
                    )

            source_roots = {
                "raybet": Path(case.store.raw_archive_root),
                "opendota": Path(case.opendota_archive.root),
            }
            for name, source in source_roots.items():
                if source.exists():
                    shutil.copytree(source, raw_root / name)
            if source_roots["raybet"].exists():
                shutil.copytree(source_roots["raybet"], runtime_raybet_root)

            frame_refs = tuple(
                str(row[0])
                for row in case.store.connection.execute(
                    "SELECT frame_ref FROM vision_frame_artifacts ORDER BY frame_ref"
                )
            )
            replacements: dict[str, Path] = {}
            for frame_ref in frame_refs:
                encoded = read_registered_vision_frame_bytes(
                    case.store.connection, frame_ref
                )
                receipt = publish_vision_frame_bytes(vision_root, encoded)
                if receipt.frame_ref != frame_ref:
                    raise RuntimeError("copied vision frame identity changed")
                replacements[frame_ref] = receipt.storage_path
            if replacements:
                if case.store.connection.in_transaction:
                    case.store.connection.commit()
                relocate_vision_frame_artifacts(
                    case.store.connection,
                    replacements,
                    allowed_new_roots=(vision_root,),
                    reason="package self-contained rollback fixture",
                    actor="p0_fixture_builder",
                    relocated_at=NOW,
                )

            destination = sqlite3.connect(fixture_database)
            try:
                case.store.connection.backup(destination)
            finally:
                destination.close()
        finally:
            case.tearDown()

        preparation = prepare_database(
            fixture_database,
            output_dir / "migration-backups",
            odds_raw_root=runtime_raybet_root,
        )
        verified = verify_prepared_database(
            fixture_database,
            odds_raw_root=runtime_raybet_root,
        )
        report_connection = sqlite3.connect(fixture_database)
        try:
            report = build_report(report_connection)
        finally:
            report_connection.close()

    report_path = output_dir / "representative-report.json"
    _write_canonical(report_path, report)
    check = sqlite3.connect(fixture_database)
    try:
        table_counts = {
            table: int(check.execute(f"SELECT COUNT(*) FROM {_quoted(table)}").fetchone()[0])
            for table in (
                "strategy_decisions",
                "shadow_orders",
                "shadow_map_attempts",
                "notification_outbox",
                "settlements",
            )
        }
    finally:
        check.close()
    attempted_production = 0
    for line in connection_audit.read_text(encoding="utf-8").splitlines():
        if json.loads(line).get("production_identity_match"):
            attempted_production += 1
    after = file_identity(production_database)
    summary = {
        "schema": "dota2-p0-rollback-fixture-v1",
        "status": "fixture_ready",
        "rollback_rehearsal_status": "unverified",
        "baseline_commit": BASELINE_COMMIT,
        "created_at": started,
        "command": [
            sys.executable,
            "-m",
            "scripts.p0_evidence",
            "build-rollback-fixture",
            "--output-dir",
            str(output_dir),
            "--production-database",
            str(production_database),
        ],
        "cwd": str(Path.cwd().resolve()),
        "environment_contract": {
            "PYTHONDONTWRITEBYTECODE": os.environ.get("PYTHONDONTWRITEBYTECODE"),
            "production_path_guard": str(production_database),
            "secrets": "not recorded",
        },
        "ended_at": utc_now(),
        "exit_status": 0,
        "database": file_record(fixture_database),
        "report": file_record(report_path),
        "raw_evidence": _tree_records(raw_root),
        "runtime_raybet_evidence": _tree_records(runtime_raybet_root),
        "vision_evidence": _tree_records(vision_root),
        "prepared_schema": {
            "status": "verified",
            "live_schema_version": verified.live_schema_version,
            "intelligence_schema_version": verified.intelligence_schema_version,
            "runtime_schema_version": verified.runtime_schema_version,
            "migration_backup": (
                file_record(preparation.backup)
                if preparation.backup is not None
                else None
            ),
        },
        "representative_table_counts": table_counts,
        "shape_scope": {
            "decision": "eligible current-code fixture",
            "order": "pending paper order",
            "attempt": "pending map attempt",
            "outbox": "monitor notification",
            "settlement": (
                "audited manual-review marker"
                if seed_settlement_review
                else "empty; reserved for cross-version authoritative handoff"
            ),
            "report": "current build_report projection",
            "schema": "current supervisor-prepared contract",
            "vision_frames": "self-contained relocated active artifacts",
            "non_direct_successor": (
                "processed audit observation after the signal; current writer "
                "must ignore it for watermark and fill resolution"
            ),
            "authoritative_filled_settlement": "unverified; remains a P1 concern",
        },
        "production_connection_audit": {
            "attempted_production_connections": attempted_production,
            "guard_result": "passed" if attempted_production == 0 else "failed",
            "artifact": file_record(connection_audit),
            "production_before": before,
            "production_after": after,
            "identity_stable": (
                before["device"] == after["device"]
                and before["inode"] == after["inode"]
            ),
            "mtime_or_size_changes_may_be_active_writer": True,
        },
    }
    _write_canonical(output_dir / "rollback-fixture-summary.json", summary)
    print(json.dumps(summary["representative_table_counts"], sort_keys=True))
    required_counts = {
        key: value
        for key, value in table_counts.items()
        if seed_settlement_review or key != "settlements"
    }
    return 0 if attempted_production == 0 and all(required_counts.values()) else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    test = commands.add_parser("run-tests", help="run guarded pytest and record nodes")
    test.add_argument("--workspace", type=Path, required=True)
    test.add_argument("--output-dir", type=Path, required=True)
    test.add_argument("--production-database", type=Path, required=True)
    test.add_argument("--label", required=True)
    test.add_argument("--test-file", action="append", dest="test_files")

    fixture = commands.add_parser(
        "build-rollback-fixture", help="write isolated representative current shapes"
    )
    fixture.add_argument("--output-dir", type=Path, required=True)
    fixture.add_argument("--production-database", type=Path, required=True)
    fixture.add_argument(
        "--without-settlement-review",
        action="store_true",
        help=(
            "leave settlements empty so a cross-version handoff rehearsal can "
            "exercise current authoritative settlement"
        ),
    )

    generate = commands.add_parser("generate", help="generate canonical P0 manifest")
    generate.add_argument("--workspace", type=Path, required=True)
    generate.add_argument("--output-dir", type=Path, required=True)
    generate.add_argument("--production-database", type=Path, required=True)
    generate.add_argument("--clean-worktree", type=Path, required=True)
    generate.add_argument("--raw-root", type=Path, required=True)
    generate.add_argument("--vision-jsonl-root", type=Path, required=True)
    generate.add_argument("--vision-frame-root", type=Path, required=True)
    generate.add_argument("--command-record", type=Path, action="append", default=[])
    generate.add_argument("--rollback-summary", type=Path)
    generate.add_argument(
        "--strategy-version",
        required=True,
        help="explicit approved v5 proposal strategy version",
    )
    generate.add_argument("--evaluator-hash", required=True, help="64-hex evaluator SHA-256")
    generate.add_argument("--policy-hash", required=True, help="64-hex policy SHA-256")
    generate.add_argument(
        "--serialization-version",
        required=True,
        help="canonical strategy contract serialization version",
    )
    generate.add_argument(
        "--evaluator-artifact-ref", required=True, help="secret-safe evaluator artifact ref"
    )
    generate.add_argument(
        "--policy-artifact-ref", required=True, help="secret-safe policy artifact ref"
    )
    generate.add_argument("--execution-owner", required=True)
    generate.add_argument("--independent-verifier", required=True)
    generate.add_argument("--production-db-operator", required=True)
    generate.add_argument("--m4-decision-owner", required=True)
    generate.add_argument(
        "--roles-effective-at",
        required=True,
        help="UTC effective timestamp for all operational role bindings",
    )
    generate.add_argument(
        "--user-page-acceptance",
        required=True,
        help="user-page authorization subject; this is not M1 acceptance",
    )
    generate.add_argument(
        "--page-acceptance-effective-at",
        required=True,
        help="UTC effective timestamp for user-page authorization",
    )

    refresh = commands.add_parser(
        "refresh", help="refresh derived evidence from a canonical prior manifest"
    )
    refresh.add_argument("--source-manifest", type=Path, required=True)
    refresh.add_argument("--output-dir", type=Path, required=True)

    verify = commands.add_parser("verify", help="verify manifest hashes and identities")
    verify.add_argument("manifest", type=Path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "run-tests":
        return run_tests(
            args.workspace,
            args.output_dir,
            args.production_database,
            args.test_files or CRITICAL_TESTS,
            label=args.label,
        )
    if args.command == "build-rollback-fixture":
        return build_rollback_fixture(
            args.output_dir,
            args.production_database,
            seed_settlement_review=not args.without_settlement_review,
        )
    if args.command == "generate":
        return generate_manifest(args)
    if args.command == "refresh":
        return refresh_manifest(args.source_manifest, args.output_dir)
    if args.command == "verify":
        return verify_manifest(args.manifest)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
