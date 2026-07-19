from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import live_betting.database_protocol as database_protocol
import scripts.database_cutover as database_cutover
from live_betting.database_protocol import (
    prepare_database,
    truncate_wal_checkpoint,
    verify_prepared_database,
)
from live_betting.service_coordination import (
    DatabaseFileIdentity,
    ProcessIdentity,
    WriterScanResult,
    database_global_authority_lock_paths,
)
from scripts.database_cutover import main
from scripts.run_dota_shadow_service import SingleInstanceLock


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wal_database(path: Path) -> sqlite3.Connection:
    writer = sqlite3.connect(path)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("CREATE TABLE facts (value INTEGER NOT NULL)")
    writer.execute("INSERT INTO facts VALUES (1)")
    writer.commit()
    wal = Path(f"{path}-wal")
    assert wal.stat().st_size > 0
    return writer


def test_truncate_checkpoint_requires_zero_triplet_and_empty_wal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "checkpoint.db"
    writer = _wal_database(database)
    try:
        result = truncate_wal_checkpoint(database)
    finally:
        writer.close()

    assert (result.busy, result.log, result.checkpoint) == (0, 0, 0)
    assert result.wal_bytes == 0
    assert result.safe


def test_truncate_checkpoint_rejects_an_active_writer(tmp_path: Path) -> None:
    database = tmp_path / "busy.db"
    writer = _wal_database(database)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO facts VALUES (2)")
    try:
        result = truncate_wal_checkpoint(database)
    finally:
        writer.rollback()
        writer.close()

    assert result.busy != 0
    assert not result.safe


def test_checkpoint_cli_succeeds_only_for_zero_triplet_and_empty_wal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "cli-checkpoint.db"
    writer = _wal_database(database)
    try:
        exit_code = main(["checkpoint", "--database", str(database)])
    finally:
        writer.close()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert (payload["busy"], payload["log"], payload["checkpoint"]) == (0, 0, 0)
    assert payload["wal_bytes"] == 0


def test_checkpoint_cli_returns_unsafe_for_an_active_writer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "cli-busy.db"
    writer = _wal_database(database)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO facts VALUES (2)")
    try:
        exit_code = main(["checkpoint", "--database", str(database)])
    finally:
        writer.rollback()
        writer.close()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == "unsafe"
    assert payload["busy"] != 0


def test_checkpoint_cli_rejects_a_running_supervisor_lock(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "locked.db"
    connection = _wal_database(database)
    lock = database.with_suffix(".service.lock")
    try:
        with SingleInstanceLock(lock):
            exit_code = main(["checkpoint", "--database", str(database)])
    finally:
        connection.close()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == "error"
    assert payload["busy"] is None
    assert payload["log"] is None
    assert payload["checkpoint"] is None
    assert "service lock is already held" in payload["error"]


def test_checkpoint_custom_lock_follows_canonical_authority_order(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "ordered.db"
    sqlite3.connect(database).close()
    custom = tmp_path / "custom.lock"
    events: list[tuple[str, Path]] = []

    class RecordingLock:
        def __init__(self, path: Path) -> None:
            self.path = path

        def __enter__(self) -> "RecordingLock":
            events.append(("enter", self.path))
            return self

        def __exit__(self, *_: object) -> None:
            events.append(("exit", self.path))

    monkeypatch.setattr(database_cutover, "SingleInstanceLock", RecordingLock)
    monkeypatch.setattr(
        database_cutover,
        "scan_managed_writers",
        lambda _, **__: WriterScanResult((), ()),
    )
    monkeypatch.setattr(
        database_cutover,
        "truncate_wal_checkpoint",
        lambda path: SimpleNamespace(
            database=path,
            busy=0,
            log=0,
            checkpoint=0,
            wal_bytes=0,
            safe=True,
        ),
    )

    assert main([
        "checkpoint",
        "--database",
        str(database),
        "--lock",
        str(custom),
    ]) == 0
    capsys.readouterr()

    standard = database.with_suffix(".service.lock").resolve()
    web = database.with_suffix(".web.lock").resolve()
    global_service, global_web = database_global_authority_lock_paths(database)
    assert events == [
        ("enter", global_service),
        ("enter", global_web),
        ("enter", standard),
        ("enter", web),
        ("enter", custom.resolve()),
        ("exit", custom.resolve()),
        ("exit", web),
        ("exit", standard),
        ("exit", global_web),
        ("exit", global_service),
    ]


def test_checkpoint_custom_lock_cannot_bypass_standard_or_leak_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "double-lock.db"
    sqlite3.connect(database).close()
    standard = database.with_suffix(".service.lock")
    custom = tmp_path / "custom.lock"

    with SingleInstanceLock(standard):
        assert main([
            "checkpoint",
            "--database",
            str(database),
            "--lock",
            str(custom),
        ]) == 1
    assert "already held" in json.loads(capsys.readouterr().out)["error"]

    with SingleInstanceLock(custom):
        assert main([
            "checkpoint",
            "--database",
            str(database),
            "--lock",
            str(custom),
        ]) == 1
    capsys.readouterr()
    with SingleInstanceLock(standard):
        pass


def test_checkpoint_same_custom_and_standard_lock_is_acquired_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "same-lock.db"
    sqlite3.connect(database).close()
    standard = database.with_suffix(".service.lock").resolve()
    web = database.with_suffix(".web.lock").resolve()
    global_service, global_web = database_global_authority_lock_paths(database)
    entered: list[Path] = []

    class RecordingLock:
        def __init__(self, path: Path) -> None:
            self.path = path

        def __enter__(self) -> "RecordingLock":
            entered.append(self.path)
            return self

        def __exit__(self, *_: object) -> None:
            pass

    monkeypatch.setattr(database_cutover, "SingleInstanceLock", RecordingLock)
    monkeypatch.setattr(
        database_cutover,
        "scan_managed_writers",
        lambda _, **__: WriterScanResult((), ()),
    )
    monkeypatch.setattr(
        database_cutover,
        "truncate_wal_checkpoint",
        lambda path: SimpleNamespace(
            database=path,
            busy=0,
            log=0,
            checkpoint=0,
            wal_bytes=0,
            safe=True,
        ),
    )

    assert main([
        "checkpoint",
        "--database",
        str(database),
        "--lock",
        str(standard),
    ]) == 0
    capsys.readouterr()
    assert entered == [global_service, global_web, standard, web]


def test_checkpoint_rejects_a_running_web_lock(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "web-locked.db"
    connection = _wal_database(database)
    try:
        with SingleInstanceLock(database.with_suffix(".web.lock")):
            exit_code = main(["checkpoint", "--database", str(database)])
    finally:
        connection.close()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == "error"
    assert "service lock is already held" in payload["error"]


def test_checkpoint_rejects_hardlink_and_rechecks_after_operation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "hardlink.db"
    alias = tmp_path / "hardlink-alias.db"
    sqlite3.connect(database).close()
    truncate = Mock()
    monkeypatch.setattr(database_cutover, "truncate_wal_checkpoint", truncate)

    os.link(database, alias)
    assert main(["checkpoint", "--database", str(database)]) == 1
    assert "exactly one hard link" in json.loads(capsys.readouterr().out)["error"]
    truncate.assert_not_called()
    alias.unlink()

    monkeypatch.setattr(
        database_cutover,
        "scan_managed_writers",
        lambda _, **__: WriterScanResult((), ()),
    )

    def create_alias(path: Path) -> SimpleNamespace:
        os.link(path, alias)
        return SimpleNamespace(
            database=path,
            busy=0,
            log=0,
            checkpoint=0,
            wal_bytes=0,
            safe=True,
        )

    monkeypatch.setattr(
        database_cutover,
        "truncate_wal_checkpoint",
        create_alias,
    )
    assert main(["checkpoint", "--database", str(database)]) == 1
    assert "exactly one hard link" in json.loads(capsys.readouterr().out)["error"]


def test_checkpoint_writer_gate_runs_before_and_after_sqlite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "writer-gate.db"
    sqlite3.connect(database).close()
    truncate = Mock()
    monkeypatch.setattr(database_cutover, "truncate_wal_checkpoint", truncate)
    monkeypatch.setattr(
        database_cutover,
        "scan_managed_writers",
        lambda _, **__: WriterScanResult((ProcessIdentity(9001, 10.0),), ()),
    )

    assert main(["checkpoint", "--database", str(database)]) == 1
    assert "managed writers still target" in json.loads(
        capsys.readouterr().out
    )["error"]
    truncate.assert_not_called()

    scans = iter((
        WriterScanResult((), ()),
        WriterScanResult((ProcessIdentity(9002, 11.0),), ()),
    ))
    monkeypatch.setattr(
        database_cutover,
        "scan_managed_writers",
        lambda _, **__: next(scans),
    )
    monkeypatch.setattr(
        database_cutover,
        "truncate_wal_checkpoint",
        lambda path: SimpleNamespace(
            database=path,
            busy=0,
            log=0,
            checkpoint=0,
            wal_bytes=0,
            safe=True,
        ),
    )
    assert main(["checkpoint", "--database", str(database)]) == 1
    assert "managed writers still target" in json.loads(
        capsys.readouterr().out
    )["error"]


@pytest.mark.parametrize(
    ("command", "operation_name"),
    [
        ("checkpoint", "truncate_wal_checkpoint"),
        ("verify-prepared", "verify_prepared_database"),
    ],
)
def test_cutover_rechecks_identity_after_initial_writer_scan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    operation_name: str,
) -> None:
    database = tmp_path / "scan-replaced.db"
    displaced = tmp_path / "scan-replaced-original.db"
    replacement = tmp_path / "scan-replacement.db"
    sqlite3.connect(database).close()
    sqlite3.connect(replacement).close()
    replacement_performed = False

    def replace_during_scan(_: Path, **__: object) -> WriterScanResult:
        nonlocal replacement_performed
        if replacement_performed:
            raise AssertionError("writer scan repeated after authority loss")
        os.replace(database, displaced)
        os.replace(replacement, database)
        replacement_performed = True
        return WriterScanResult((), ())

    sqlite_operation = Mock()
    monkeypatch.setattr(
        database_cutover,
        "scan_managed_writers",
        replace_during_scan,
    )
    monkeypatch.setattr(database_cutover, operation_name, sqlite_operation)

    assert main([command, "--database", str(database)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "database file identity changed" in payload["error"]
    assert replacement_performed
    sqlite_operation.assert_not_called()


def test_checkpoint_rechecks_identity_after_failing_final_writer_scan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "final-gates.db"
    sqlite3.connect(database).close()
    identity = DatabaseFileIdentity(database.resolve(), 1, 2)
    events: list[str] = []
    initial_scan_completed = False

    def require_identity(*_: object, **__: object) -> DatabaseFileIdentity:
        events.append("identity")
        return identity

    def scan(_: Path, **__: object) -> WriterScanResult:
        nonlocal initial_scan_completed
        events.append("scan")
        if initial_scan_completed:
            raise OSError("scan unavailable")
        initial_scan_completed = True
        return WriterScanResult((), ())

    monkeypatch.setattr(
        database_cutover,
        "require_unique_database_file",
        require_identity,
    )
    monkeypatch.setattr(database_cutover, "scan_managed_writers", scan)
    monkeypatch.setattr(
        database_cutover,
        "truncate_wal_checkpoint",
        lambda path: SimpleNamespace(
            database=path,
            busy=0,
            log=0,
            checkpoint=0,
            wal_bytes=0,
            safe=True,
        ),
    )

    assert main(["checkpoint", "--database", str(database)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "writer_scan:OSError:scan unavailable" in payload["error"]
    final_scan = max(index for index, event in enumerate(events) if event == "scan")
    assert "identity" in events[final_scan + 1 :]


def test_verify_prepared_rejects_hardlinked_database_before_verifier(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "verify-hardlink.db"
    alias = tmp_path / "verify-hardlink-alias.db"
    sqlite3.connect(database).close()
    os.link(database, alias)
    verifier = Mock()
    monkeypatch.setattr(database_cutover, "verify_prepared_database", verifier)

    assert main(["verify-prepared", "--database", str(database)]) == 1
    assert "exactly one hard link" in json.loads(capsys.readouterr().out)["error"]
    verifier.assert_not_called()


def test_verify_prepared_cli_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "prepared.db"
    prepare_database(database, tmp_path / "schema-backups")
    before = _hash(database)

    exit_code = main(
        [
            "verify-prepared",
            "--database",
            str(database),
            "--odds-raw-root",
            str(tmp_path / "missing-but-empty-raw-v2"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["open_mode"] == "ro"
    assert payload["query_only"] is True
    assert payload["runtime_schema_version"] == 1
    assert _hash(database) == before


def test_prepared_verifier_enforces_query_only_on_a_mode_ro_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "mode-ro.db"
    prepare_database(database, tmp_path / "schema-backups")
    observed: dict[str, bool] = {}

    def verify_authority(connection: sqlite3.Connection, _: Path) -> None:
        observed["query_only"] = bool(
            connection.execute("PRAGMA query_only").fetchone()[0]
        )
        connection.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden_write (value INTEGER)")
        observed["mode_ro"] = True

    monkeypatch.setattr(
        database_protocol,
        "verify_odds_response_authority",
        verify_authority,
    )

    verify_prepared_database(database)

    assert observed == {"query_only": True, "mode_ro": True}
