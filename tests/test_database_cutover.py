from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import live_betting.database_protocol as database_protocol
from live_betting.database_protocol import (
    prepare_database,
    truncate_wal_checkpoint,
    verify_prepared_database,
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
