from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import live_betting.database_protocol as database_protocol
import live_betting.storage as live_storage
from event_intelligence.backtest import (
    draft_lineage_tracking_is_current,
    draft_lineage_trigger_names,
)
from event_intelligence.storage import (
    CURRENT_SCHEMA_VERSION as INTELLIGENCE_VERSION,
    IntelligenceStorage,
)
from live_betting.database_protocol import (
    check_schema_versions,
    immutable_checkpoint_reader,
    online_backup,
    prepare_database,
    restore_database_backup,
    vacuum_into_immutable_checkpoint,
    verify_prepared_database,
)
from live_betting.service_coordination import SingleInstanceLock
from live_betting.storage import CURRENT_SCHEMA_VERSION as LIVE_VERSION
from live_betting.storage import LiveBettingStore
from live_betting.vision import VisionObservation
from shared.sqlite import connect, execute_script


def _database_dump(database: Path) -> tuple[str, ...]:
    connection = sqlite3.connect(database)
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()


def _file_state(path: Path) -> tuple[int, int, str] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_lineage_tracking(connection: sqlite3.Connection) -> None:
    expected = draft_lineage_trigger_names()
    assert len(expected) == 42
    installed = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
        if str(row[0]).startswith("draft_lineage_")
    }
    assert installed == expected
    assert draft_lineage_tracking_is_current(connection)


def _rewrite_table_definition(
    database: Path,
    table: str,
    old: str,
    new: str,
) -> None:
    connection = connect(database)
    try:
        table_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        assert table_row is not None and table_row[0] is not None
        table_sql = str(table_row[0])
        assert table_sql.count(old) == 1
        dependent_sql = [
            str(row[0])
            for row in connection.execute(
                """SELECT sql FROM sqlite_master
                     WHERE tbl_name=? AND type IN ('index', 'trigger')
                       AND sql IS NOT NULL
                     ORDER BY type, name""",
                (table,),
            )
        ]
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(f'DROP TABLE "{table}"')
        connection.execute(table_sql.replace(old, new))
        for statement in dependent_sql:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def test_execute_script_does_not_commit_the_callers_transaction() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("BEGIN EXCLUSIVE")
        execute_script(
            connection,
            "CREATE TABLE first (value TEXT); CREATE TABLE second (value TEXT);",
        )
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
        ).fetchone() is None
    finally:
        connection.close()


def test_immutable_checkpoint_reader_preserves_quiescent_sidecars(
    tmp_path: Path,
) -> None:
    database = tmp_path / "immutable.db"
    writer = connect(database)
    try:
        writer.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        writer.execute("INSERT INTO marker VALUES ('stable')")
        writer.commit()
    finally:
        writer.close()
    shm = Path(f"{database}-shm")
    shm.write_bytes(b"quiescent")
    before = database_protocol.sqlite_sidecar_state(database)
    lock_path = tmp_path / "immutable.lock"

    with SingleInstanceLock(lock_path):
        with immutable_checkpoint_reader(
            database,
            label="test database",
            required_locks=(lock_path,),
        ) as reader:
            assert reader.execute("SELECT value FROM marker").fetchone()[0] == "stable"

    assert database_protocol.sqlite_sidecar_state(database) == before


def test_immutable_checkpoint_reader_rejects_transactional_sidecars(
    tmp_path: Path,
) -> None:
    database = tmp_path / "immutable.db"
    writer = connect(database)
    writer.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    writer.commit()
    writer.close()
    Path(f"{database}-wal").write_bytes(b"active")
    lock_path = tmp_path / "immutable.lock"

    with SingleInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="non-empty transactional sidecars"):
            with immutable_checkpoint_reader(
                database,
                label="test database",
                required_locks=(lock_path,),
            ):
                pytest.fail("transactional database was opened as immutable")


def test_immutable_checkpoint_reader_rejects_sidecar_change(
    tmp_path: Path,
) -> None:
    database = tmp_path / "immutable.db"
    writer = connect(database)
    writer.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    writer.commit()
    writer.close()
    shm = Path(f"{database}-shm")
    shm.write_bytes(b"before")
    lock_path = tmp_path / "immutable.lock"

    with SingleInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="sidecars changed during immutable read"):
            with immutable_checkpoint_reader(
                database,
                label="test database",
                required_locks=(lock_path,),
            ):
                shm.write_bytes(b"after!")


def test_immutable_checkpoint_reader_rejects_same_size_database_change(
    tmp_path: Path,
) -> None:
    database = tmp_path / "immutable.db"
    writer = connect(database)
    writer.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    writer.execute("INSERT INTO marker VALUES ('before')")
    writer.commit()
    writer.close()
    lock_path = tmp_path / "immutable.lock"

    with SingleInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="database changed during immutable read"):
            with immutable_checkpoint_reader(
                database,
                label="test database",
                required_locks=(lock_path,),
            ):
                with database.open("r+b") as handle:
                    handle.seek(-1, 2)
                    original = handle.read(1)
                    handle.seek(-1, 2)
                    handle.write(b"0" if original != b"0" else b"1")
                    handle.flush()
                    os.fsync(handle.fileno())


def test_immutable_checkpoint_reader_preserves_body_base_exception(
    tmp_path: Path,
) -> None:
    database = tmp_path / "immutable.db"
    writer = connect(database)
    writer.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    writer.commit()
    writer.close()
    shm = Path(f"{database}-shm")
    shm.write_bytes(b"before")
    lock_path = tmp_path / "immutable.lock"

    with SingleInstanceLock(lock_path):
        with pytest.raises(KeyboardInterrupt, match="body") as raised:
            with immutable_checkpoint_reader(
                database,
                label="test database",
                required_locks=(lock_path,),
            ):
                shm.write_bytes(b"after!")
                raise KeyboardInterrupt("body")

    assert any(
        "sidecars changed during immutable read" in note
        for note in raised.value.__notes__
    )


def test_vacuum_into_immutable_checkpoint_preserves_source_sidecars(
    tmp_path: Path,
) -> None:
    database = tmp_path / "source.db"
    output = tmp_path / "vacuumed.db"
    writer = connect(database)
    writer.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    writer.execute("INSERT INTO marker VALUES ('stable')")
    writer.commit()
    writer.close()
    shm = Path(f"{database}-shm")
    shm.write_bytes(b"quiescent")
    before = database_protocol.sqlite_sidecar_state(database)
    checks: list[str] = []
    lock_path = tmp_path / "immutable.lock"

    with SingleInstanceLock(lock_path):
        vacuum_into_immutable_checkpoint(
            database,
            output,
            label="test vacuum source",
            required_locks=(lock_path,),
            authority_check=lambda: checks.append("checked"),
        )

    reader = connect(output, read_only=True)
    try:
        assert reader.execute("SELECT value FROM marker").fetchone()[0] == "stable"
    finally:
        reader.close()
    assert checks == ["checked", "checked"]
    assert database_protocol.sqlite_sidecar_state(database) == before


def test_shared_connection_policy_applies_to_read_write_and_read_only(
    tmp_path: Path,
) -> None:
    database = tmp_path / "policy.db"
    writer = connect(database)
    try:
        writer.execute("CREATE TABLE marker (value TEXT)")
        writer.commit()
        assert writer.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert writer.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        writer.close()

    reader = connect(database, read_only=True)
    try:
        assert reader.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO marker VALUES ('forbidden')")
    finally:
        reader.close()


def test_online_backup_includes_committed_wal_with_writer_still_open(
    tmp_path: Path,
) -> None:
    database = tmp_path / "wal.db"
    backup_path = tmp_path / "wal-backup.db"
    writer = connect(database, wal=True)
    try:
        writer.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        writer.execute("INSERT INTO marker VALUES ('committed-in-wal')")
        writer.commit()

        online_backup(database, backup_path)
    finally:
        writer.close()

    backup = connect(backup_path, read_only=True)
    try:
        assert backup.execute("SELECT value FROM marker").fetchone()[0] == (
            "committed-in-wal"
        )
    finally:
        backup.close()


def test_online_backup_rejects_a_destination_without_enough_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "source.db"
    backup_path = tmp_path / "backups" / "source.db"
    connection = connect(database)
    connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    connection.execute("INSERT INTO marker VALUES ('preserved')")
    connection.commit()
    connection.close()

    monkeypatch.setattr(
        database_protocol.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=0),
    )

    with pytest.raises(RuntimeError, match="insufficient free space"):
        online_backup(database, backup_path)

    assert not backup_path.exists()


def test_prepare_database_backs_up_existing_data_before_migration(
    tmp_path: Path,
) -> None:
    database = tmp_path / "service.db"
    connection = connect(database, wal=True)
    connection.execute("CREATE TABLE operator_marker (value TEXT NOT NULL)")
    connection.execute("INSERT INTO operator_marker VALUES ('preserve-me')")
    connection.commit()
    connection.close()

    result = prepare_database(
        database,
        tmp_path / "backups",
        now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
    )

    assert result.backup is not None and result.backup.is_file()
    backup = connect(result.backup, read_only=True)
    try:
        assert backup.execute("SELECT value FROM operator_marker").fetchone()[0] == (
            "preserve-me"
        )
        assert (
            backup.execute(
                "SELECT 1 FROM sqlite_master WHERE name='live_schema_version'"
            ).fetchone()
            is None
        )
    finally:
        backup.close()
    assert check_schema_versions(database) == (LIVE_VERSION, INTELLIGENCE_VERSION)


def test_prepare_holds_exclusive_lock_before_taking_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "locked-service.db"
    connection = connect(database, wal=True)
    connection.execute("CREATE TABLE operator_marker (value TEXT NOT NULL)")
    connection.commit()
    connection.close()
    original_backup = database_protocol._backup_connection

    def assert_locked(source: sqlite3.Connection, destination: Path) -> None:
        assert source.execute("PRAGMA locking_mode").fetchone()[0] == "exclusive"
        contender = connect(database)
        try:
            contender.execute("PRAGMA busy_timeout=1")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("INSERT INTO operator_marker VALUES ('racing')")
        finally:
            contender.close()
        original_backup(source, destination)

    monkeypatch.setattr(database_protocol, "_backup_connection", assert_locked)

    result = prepare_database(database, tmp_path / "backups")

    assert result.backup is not None


def test_prepare_database_initializes_a_fresh_database_without_fake_backup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fresh.db"

    result = prepare_database(database, tmp_path / "backups")

    assert result.backup is None
    assert result.live_schema_version == LIVE_VERSION
    assert result.intelligence_schema_version == INTELLIGENCE_VERSION
    assert not (tmp_path / "backups").exists()


def test_locked_prepare_fast_path_is_read_only_and_creates_no_repeat_backup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "current.db"
    backup_dir = tmp_path / "backups"
    first = prepare_database(
        database,
        backup_dir,
        now=datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc),
        supervisor_process_lock_held=True,
    )
    before = {
        path: _file_state(path)
        for path in (database, Path(f"{database}-wal"))
    }

    second = prepare_database(
        database,
        backup_dir,
        now=datetime(2026, 7, 17, 8, 1, tzinfo=timezone.utc),
        supervisor_process_lock_held=True,
    )
    third = prepare_database(
        database,
        backup_dir,
        now=datetime(2026, 7, 17, 8, 2, tzinfo=timezone.utc),
        supervisor_process_lock_held=True,
    )

    assert first.backup is None
    assert second.backup is None
    assert third.backup is None
    assert {
        path: _file_state(path)
        for path in (database, Path(f"{database}-wal"))
    } == before
    assert not backup_dir.exists()


def test_locked_prepare_backs_up_and_repairs_current_contract_drift(
    tmp_path: Path,
) -> None:
    database = tmp_path / "drifted-current.db"
    backup_dir = tmp_path / "backups"
    prepare_database(
        database,
        backup_dir,
        supervisor_process_lock_held=True,
    )
    connection = connect(database)
    try:
        connection.execute(
            "DROP TRIGGER raw_source_artifact_relocations_immutable_update"
        )
        connection.commit()
    finally:
        connection.close()

    repaired = prepare_database(
        database,
        backup_dir,
        now=datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc),
        supervisor_process_lock_held=True,
    )

    assert repaired.backup is not None and repaired.backup.is_file()
    assert len(list(backup_dir.glob("*.db"))) == 1
    verify_prepared_database(database)
    verification = connect(database, read_only=True)
    try:
        assert verification.execute(
            """SELECT 1 FROM sqlite_master WHERE type='trigger'
                AND name='raw_source_artifact_relocations_immutable_update'"""
        ).fetchone() is not None
    finally:
        verification.close()
    verification = connect(database, read_only=True)
    try:
        assert (
            verification.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='draft_lineage_revisions'"
            ).fetchone()
            is not None
        )
        _assert_lineage_tracking(verification)
    finally:
        verification.close()


@pytest.mark.parametrize(
    "phase",
    ("live", "intelligence", "fetch", "draft_authority"),
)
def test_prepare_database_rolls_back_every_schema_phase_without_backup_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    database = tmp_path / f"atomic-{phase}.db"
    original = connect(database, wal=True)
    try:
        original.execute("CREATE TABLE operator_marker (value TEXT NOT NULL)")
        original.execute("INSERT INTO operator_marker VALUES ('preserved')")
        original.commit()
    finally:
        original.close()
    before = _database_dump(database)

    if phase == "live":
        initialize = database_protocol.LiveBettingStore.init_schema

        def fail_live(
            store: LiveBettingStore,
            *,
            external_transaction: bool = False,
        ) -> None:
            initialize(store, external_transaction=external_transaction)
            assert external_transaction and store.connection.in_transaction
            raise RuntimeError("fault after live schema")

        monkeypatch.setattr(
            database_protocol.LiveBettingStore,
            "init_schema",
            fail_live,
        )
    elif phase == "intelligence":
        initialize = database_protocol.IntelligenceStorage.init_schema

        def fail_intelligence(
            storage: IntelligenceStorage,
            *,
            seed_events: bool = True,
            external_transaction: bool = False,
        ) -> None:
            initialize(
                storage,
                seed_events=seed_events,
                external_transaction=external_transaction,
            )
            assert external_transaction and storage.connection.in_transaction
            raise RuntimeError("fault after intelligence schema")

        monkeypatch.setattr(
            database_protocol.IntelligenceStorage,
            "init_schema",
            fail_intelligence,
        )
    elif phase == "fetch":
        initialize = database_protocol.Database.init_db

        def fail_fetch(
            storage: database_protocol.Database,
            *,
            external_transaction: bool = False,
        ) -> None:
            initialize(storage, external_transaction=external_transaction)
            connection = storage.connect()
            assert external_transaction and connection.in_transaction
            raise RuntimeError("fault after fetch schema")

        monkeypatch.setattr(database_protocol.Database, "init_db", fail_fetch)
    else:
        initialize = database_protocol.init_draft_authority_revision_schema

        def fail_draft_authority(connection: sqlite3.Connection) -> None:
            initialize(connection)
            assert connection.in_transaction
            raise RuntimeError("fault after draft authority schema")

        monkeypatch.setattr(
            database_protocol,
            "init_draft_authority_revision_schema",
            fail_draft_authority,
        )

    restore_attempted = False

    def skip_restore(_backup: Path, _database: Path) -> None:
        nonlocal restore_attempted
        restore_attempted = True

    monkeypatch.setattr(database_protocol, "_restore_online_backup", skip_restore)

    with pytest.raises(RuntimeError, match=f"fault after {phase.replace('_', ' ')} schema"):
        prepare_database(database, tmp_path / "backups")

    assert restore_attempted
    assert _database_dump(database) == before


def test_prepare_database_rolls_back_partial_lineage_trigger_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "atomic-lineage.db"
    original = connect(database, wal=True)
    try:
        original.execute("CREATE TABLE operator_marker (value TEXT NOT NULL)")
        original.execute("INSERT INTO operator_marker VALUES ('preserved')")
        original.commit()
    finally:
        original.close()
    before = _database_dump(database)
    install = database_protocol.ensure_draft_lineage_tracking
    create_count = 0

    def fail_during_trigger_install(connection: sqlite3.Connection) -> None:
        def authorizer(
            action: int,
            _arg1: str | None,
            _arg2: str | None,
            _database_name: str | None,
            _trigger_name: str | None,
        ) -> int:
            nonlocal create_count
            if action == sqlite3.SQLITE_CREATE_TRIGGER:
                create_count += 1
                if create_count == 5:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        try:
            install(connection)
        finally:
            connection.set_authorizer(None)

    monkeypatch.setattr(
        database_protocol,
        "ensure_draft_lineage_tracking",
        fail_during_trigger_install,
    )
    monkeypatch.setattr(
        database_protocol,
        "_restore_online_backup",
        lambda _backup, _database: None,
    )

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        prepare_database(database, tmp_path / "backups")

    assert create_count == 5
    assert _database_dump(database) == before


def test_prepare_database_holds_exclusive_transaction_through_schema_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "exclusive-transaction.db"
    original = connect(database, wal=True)
    original.execute("CREATE TABLE operator_marker (value TEXT NOT NULL)")
    original.commit()
    original.close()
    initialize = database_protocol.IntelligenceStorage.init_schema
    observed = False

    def assert_exclusive(
        storage: IntelligenceStorage,
        *,
        seed_events: bool = True,
        external_transaction: bool = False,
    ) -> None:
        nonlocal observed
        observed = True
        assert external_transaction
        assert storage.connection.in_transaction
        assert storage.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert storage.connection.execute("PRAGMA locking_mode").fetchone()[0] == (
            "exclusive"
        )
        contender = connect(database)
        try:
            contender.execute("PRAGMA busy_timeout=1")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("INSERT INTO operator_marker VALUES ('racing')")
        finally:
            contender.close()
        initialize(
            storage,
            seed_events=seed_events,
            external_transaction=external_transaction,
        )

    monkeypatch.setattr(
        database_protocol.IntelligenceStorage,
        "init_schema",
        assert_exclusive,
    )

    prepare_database(database, tmp_path / "backups")

    assert observed


def test_prepare_database_installs_lineage_tracking_during_v5_upgrade(
    tmp_path: Path,
) -> None:
    database = tmp_path / "lineage-v5.db"
    legacy = connect(database, wal=True)
    try:
        legacy.execute(
            """CREATE TABLE live_schema_version (
                   version INTEGER PRIMARY KEY,
                   applied_at TEXT NOT NULL
               )"""
        )
        legacy.execute("INSERT INTO live_schema_version VALUES (5, 'v5')")
        legacy.commit()
    finally:
        legacy.close()

    prepare_database(database, tmp_path / "backups")

    upgraded = connect(database)
    try:
        _assert_lineage_tracking(upgraded)
        before = int(
            upgraded.execute(
                "SELECT dependency_revision FROM draft_lineage_revisions WHERE singleton=1"
            ).fetchone()[0]
        )
        event_id = str(
            upgraded.execute(
                "SELECT event_id FROM event_registry ORDER BY event_id LIMIT 1"
            ).fetchone()[0]
        )
        upgraded.execute(
            "UPDATE event_registry SET canonical_name=canonical_name || ' revised' "
            "WHERE event_id=?",
            (event_id,),
        )
        after = int(
            upgraded.execute(
                "SELECT dependency_revision FROM draft_lineage_revisions WHERE singleton=1"
            ).fetchone()[0]
        )
        assert after == before + 1
        upgraded.rollback()
    finally:
        upgraded.close()


def test_verify_prepared_database_rejects_missing_lineage_trigger(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing-lineage-trigger.db"
    prepare_database(database, tmp_path / "backups")
    missing = sorted(draft_lineage_trigger_names())[0]
    tampered = connect(database)
    try:
        tampered.execute(f'DROP TRIGGER "{missing}"')
        tampered.commit()
        assert not draft_lineage_tracking_is_current(tampered)
    finally:
        tampered.close()

    with pytest.raises(RuntimeError, match=f"missing triggers: {missing}"):
        verify_prepared_database(database)


def test_prepare_database_rebuilds_v5_draft_authority_triggers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live-v5-draft-authority.db"
    legacy = connect(database)
    try:
        legacy.executescript(
            """
            CREATE TABLE live_schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO live_schema_version VALUES (5, 'v5');
            CREATE TABLE draft_deployment_bundles (
                deployment_key TEXT PRIMARY KEY CHECK (length(deployment_key)=64),
                model_hashes_json TEXT NOT NULL CHECK (json_valid(model_hashes_json)),
                calibration_hashes_json TEXT NOT NULL
                    CHECK (json_valid(calibration_hashes_json)),
                training_cutoff TEXT NOT NULL,
                dependency_fingerprint TEXT NOT NULL
                    CHECK (length(dependency_fingerprint)=64),
                dependency_revision INTEGER NOT NULL
                    CHECK (dependency_revision >= 1),
                evidence_mode TEXT NOT NULL CHECK (
                    evidence_mode IN ('reconstructed_walk_forward', 'prospective')
                ),
                created_at TEXT NOT NULL
            );
            CREATE TABLE prospective_draft_curves (
                curve_key TEXT PRIMARY KEY CHECK (length(curve_key)=64),
                raybet_match_id TEXT NOT NULL,
                map_number INTEGER NOT NULL CHECK (map_number > 0),
                strict_mapping_id INTEGER NOT NULL CHECK (strict_mapping_id > 0),
                lineup_hash TEXT NOT NULL CHECK (length(lineup_hash)=64),
                radiant_hero_ids_json TEXT NOT NULL,
                dire_hero_ids_json TEXT NOT NULL,
                prediction_cutoff TEXT NOT NULL,
                first_usable_at TEXT NOT NULL,
                availability_mode TEXT NOT NULL
                    CHECK (availability_mode='prospective'),
                created_at TEXT NOT NULL,
                radiant_team_side TEXT CHECK (
                    radiant_team_side IS NULL
                    OR radiant_team_side IN ('team_one', 'team_two')
                ),
                anchor_draft_hash TEXT CHECK (
                    anchor_draft_hash IS NULL OR length(anchor_draft_hash)=64
                ),
                anchor_source_frame_ref TEXT,
                anchor_anchored_at TEXT,
                deployment_key TEXT CHECK (
                    deployment_key IS NULL OR length(deployment_key)=64
                ),
                target_snapshot_hash TEXT CHECK (
                    target_snapshot_hash IS NULL OR length(target_snapshot_hash)=64
                ),
                feature_snapshot_json TEXT CHECK (
                    feature_snapshot_json IS NULL
                    OR json_valid(feature_snapshot_json)
                ),
                UNIQUE (
                    raybet_match_id, map_number, strict_mapping_id, lineup_hash,
                    first_usable_at, curve_key
                )
            );
            CREATE TRIGGER prospective_draft_curve_authority_insert
            BEFORE INSERT ON prospective_draft_curves
            WHEN NEW.radiant_team_side IS NULL
              OR NEW.anchor_draft_hash IS NULL
              OR NEW.anchor_source_frame_ref IS NULL
              OR NEW.anchor_source_frame_ref=''
              OR NEW.anchor_anchored_at IS NULL
              OR NEW.deployment_key IS NULL
              OR NEW.target_snapshot_hash IS NULL
              OR NEW.feature_snapshot_json IS NULL
              OR NOT EXISTS (
                  SELECT 1 FROM draft_deployment_bundles AS deployment
                   WHERE deployment.deployment_key=NEW.deployment_key
              )
            BEGIN
                SELECT RAISE(
                    ABORT, 'prospective draft curve authority is required'
                );
            END;
            CREATE TABLE prospective_draft_outcomes (
                curve_key TEXT PRIMARY KEY
                    REFERENCES prospective_draft_curves(curve_key),
                strict_mapping_id INTEGER NOT NULL CHECK (strict_mapping_id > 0),
                dota_match_id INTEGER NOT NULL,
                radiant_win INTEGER NOT NULL CHECK (radiant_win IN (0, 1)),
                winner_side TEXT NOT NULL
                    CHECK (winner_side IN ('team_one', 'team_two')),
                evidence_ref TEXT NOT NULL,
                evidence_hash TEXT NOT NULL CHECK (length(evidence_hash)=64),
                settled_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        legacy.commit()
    finally:
        legacy.close()

    prepare_database(database, tmp_path / "migration-backups")

    migrated = connect(database)
    try:
        trigger_sql = {
            str(row[0]): str(row[1])
            for row in migrated.execute(
                """SELECT name, sql FROM sqlite_master
                     WHERE type='trigger' AND name IN (
                         'prospective_draft_curve_authority_insert',
                         'prospective_draft_outcome_authority_insert'
                     )"""
            )
        }
        assert "feature_dependency_fingerprint" in trigger_sql[
            "prospective_draft_curve_authority_insert"
        ]
        assert "feature_dependency_revision" in trigger_sql[
            "prospective_draft_curve_authority_insert"
        ]
        assert "first_usable_at" in trigger_sql[
            "prospective_draft_outcome_authority_insert"
        ]

        deployment_key = "d" * 64
        with pytest.raises(
            sqlite3.IntegrityError,
            match="draft deployment bundle authority is required",
        ):
            migrated.execute(
                """INSERT INTO draft_deployment_bundles
                   (deployment_key, model_hashes_json, calibration_hashes_json,
                    training_cutoff, dependency_fingerprint, dependency_revision,
                    evidence_mode, created_at)
                   VALUES (?, '{}', '{}', ?, ?, 1, 'prospective', ?)""",
                (
                    deployment_key,
                    "2026-07-17T10:00:00+00:00",
                    "a" * 64,
                    "2026-07-17T10:00:00+00:00",
                ),
            )
        model_hashes = {
            str(horizon): hashlib.sha256(
                f"migration-model:{horizon}".encode()
            ).hexdigest()
            for horizon in (10, 20, 30, 40, 50)
        }
        calibration_hashes = {
            str(horizon): hashlib.sha256(
                f"migration-calibration:{horizon}".encode()
            ).hexdigest()
            for horizon in (10, 20, 30, 40, 50)
        }
        model_json = json.dumps(
            {
                "artifact_version": "draft-model-artifact-v2",
                "support": 0,
                "training_corpus": [],
            },
            sort_keys=True,
        )
        for horizon in (10, 20, 30, 40, 50):
            migrated.execute(
                """INSERT INTO draft_model_artifacts
                   (model_hash, model_version, model_kind, horizon_minutes,
                    training_cutoff, feature_schema_hash, training_input_hash,
                    artifact_json, created_at)
                   VALUES (?, 'migration-model-v2', 'pure_draft', ?, ?, ?, ?, ?, ?)""",
                (
                    model_hashes[str(horizon)],
                    horizon,
                    "2026-07-17T10:00:00+00:00",
                    "f" * 64,
                    "i" * 64,
                    model_json,
                    "2026-07-17T10:00:00+00:00",
                ),
            )
            migrated.execute(
                """INSERT INTO draft_calibration_artifacts
                   (calibration_hash, model_hash, calibration_version,
                    horizon_minutes, evidence_mode, support, artifact_json,
                    created_at)
                   VALUES (?, ?, 'migration-calibration-v1', ?, 'prospective',
                           0, '{}', ?)""",
                (
                    calibration_hashes[str(horizon)],
                    model_hashes[str(horizon)],
                    horizon,
                    "2026-07-17T10:00:00+00:00",
                ),
            )
        migrated.execute(
            """INSERT INTO draft_deployment_bundles
               (deployment_key, model_hashes_json, calibration_hashes_json,
                training_cutoff, dependency_fingerprint, dependency_revision,
                evidence_mode, created_at)
               VALUES (?, ?, ?, ?, ?, 1, 'prospective', ?)""",
            (
                deployment_key,
                json.dumps(model_hashes, sort_keys=True),
                json.dumps(calibration_hashes, sort_keys=True),
                "2026-07-17T10:00:00+00:00",
                "a" * 64,
                "2026-07-17T10:00:00+00:00",
            ),
        )
        legacy_curve_values = (
            "c" * 64,
            "match-v5",
            1,
            7,
            "l" * 64,
            "[1,2,3,4,5]",
            "[6,7,8,9,10]",
            "2026-07-17T10:01:00+00:00",
            "2026-07-17T10:01:00+00:00",
            "2026-07-17T10:01:00+00:00",
            "team_one",
            "h" * 64,
            "frame-ref",
            "2026-07-17T10:00:59+00:00",
            deployment_key,
            "t" * 64,
            "{}",
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="prospective draft curve authority is required",
        ):
            migrated.execute(
                """INSERT INTO prospective_draft_curves
                   (curve_key, raybet_match_id, map_number, strict_mapping_id,
                    lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
                    prediction_cutoff, first_usable_at, availability_mode,
                    created_at, radiant_team_side, anchor_draft_hash,
                    anchor_source_frame_ref, anchor_anchored_at, deployment_key,
                    target_snapshot_hash, feature_snapshot_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prospective', ?, ?, ?,
                           ?, ?, ?, ?, ?)""",
                legacy_curve_values,
            )

        migrated.execute(
            """INSERT INTO prospective_draft_curves
               (curve_key, raybet_match_id, map_number, strict_mapping_id,
                lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
                prediction_cutoff, first_usable_at, availability_mode,
                created_at, radiant_team_side, anchor_draft_hash,
                anchor_source_frame_ref, anchor_anchored_at, deployment_key,
                anchor_team_side_source_frame_ref,
                anchor_team_side_anchored_at,
                target_snapshot_hash, feature_snapshot_json,
                feature_dependency_fingerprint, feature_dependency_revision)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prospective', ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                *legacy_curve_values[:15],
                "team-side-frame-ref",
                "2026-07-17T10:00:58+00:00",
                *legacy_curve_values[15:],
                "f" * 64,
            ),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="prospective draft outcome authority is required",
        ):
            migrated.execute(
                """INSERT INTO prospective_draft_outcomes
                   (curve_key, strict_mapping_id, dota_match_id, radiant_win,
                    winner_side, evidence_ref, evidence_hash, settled_at,
                    created_at)
                   VALUES (?, 7, 12345, 1, 'team_one', 'result-ref', ?, ?, ?)""",
                (
                    "c" * 64,
                    "e" * 64,
                    "2026-07-17T11:00:00+00:00",
                    "2026-07-17T11:00:01+00:00",
                ),
            )
    finally:
        migrated.close()


def test_prepare_database_migrates_v5_postmatch_mapping_authority_additively(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live-v5-postmatch-mapping.db"
    legacy = connect(database)
    try:
        legacy.executescript(
            """
            CREATE TABLE live_schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO live_schema_version VALUES (5, 'v5');
            CREATE TABLE settlement_reconciliations (
                raybet_match_id TEXT NOT NULL,
                map_number INTEGER NOT NULL CHECK (map_number > 0),
                dota_match_id INTEGER NOT NULL,
                raybet_winner_side TEXT
                    CHECK (raybet_winner_side IN ('team_one', 'team_two')),
                opendota_winner_side TEXT NOT NULL
                    CHECK (opendota_winner_side IN ('team_one', 'team_two')),
                raybet_evidence_ref TEXT NOT NULL,
                opendota_evidence_ref TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('pending', 'confirmed', 'manual_review')),
                reason TEXT NOT NULL,
                first_observed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (raybet_match_id, map_number)
            );
            CREATE INDEX idx_settlement_reconciliations_dota_match
                ON settlement_reconciliations(dota_match_id);
            CREATE TABLE map_results (
                raybet_match_id TEXT NOT NULL,
                map_number INTEGER NOT NULL,
                dota_match_id INTEGER NOT NULL UNIQUE,
                winner_side TEXT NOT NULL,
                team_one_kills INTEGER,
                team_two_kills INTEGER,
                duration_seconds INTEGER,
                evidence_ref TEXT NOT NULL,
                settled_at TEXT NOT NULL,
                PRIMARY KEY (raybet_match_id, map_number)
            );
            INSERT INTO settlement_reconciliations
                (raybet_match_id, map_number, dota_match_id,
                 raybet_winner_side, opendota_winner_side,
                 raybet_evidence_ref, opendota_evidence_ref, status, reason,
                 first_observed_at, updated_at)
            VALUES ('legacy-match', 1, 9001, 'team_one', 'team_one',
                    'raybet:legacy', 'opendota:9001', 'confirmed',
                    'sources_consistent', '2026-07-16T10:00:00+00:00',
                    '2026-07-16T10:00:00+00:00');
            INSERT INTO map_results
                (raybet_match_id, map_number, dota_match_id, winner_side,
                 team_one_kills, team_two_kills, duration_seconds, evidence_ref,
                 settled_at)
            VALUES ('legacy-match', 1, 9001, 'team_one', 30, 20, 2400,
                    'settlement-reconciliation:legacy-match:map:1',
                    '2026-07-16T10:00:00+00:00');
            """
        )
        legacy.commit()
    finally:
        legacy.close()

    first = prepare_database(
        database,
        tmp_path / "migration-backups",
        now=datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
    )
    second = prepare_database(
        database,
        tmp_path / "migration-backups",
        now=datetime(2026, 7, 17, 10, 1, tzinfo=timezone.utc),
    )

    assert first.live_schema_version == LIVE_VERSION
    assert second.live_schema_version == LIVE_VERSION
    assert first.backup is not None and first.backup.is_file()
    assert second.backup is not None and second.backup.is_file()

    migrated = connect(database)
    try:
        assert "strict_mapping_id" in {
            str(row[1])
            for row in migrated.execute("PRAGMA table_info(settlement_reconciliations)")
        }
        assert "strict_mapping_id" in {
            str(row[1]) for row in migrated.execute("PRAGMA table_info(map_results)")
        }
        assert tuple(
            migrated.execute(
                """SELECT raybet_match_id, map_number, dota_match_id, status,
                          reason, strict_mapping_id
                     FROM settlement_reconciliations
                    WHERE raybet_match_id='legacy-match' AND map_number=1"""
            ).fetchone()
        ) == (
            "legacy-match",
            1,
            9001,
                "manual_review",
                "legacy_source_authority_missing",
            None,
        )
        assert tuple(
            migrated.execute(
                """SELECT raybet_match_id, map_number, dota_match_id, winner_side,
                          strict_mapping_id
                     FROM map_results
                    WHERE raybet_match_id='legacy-match' AND map_number=1"""
            ).fetchone()
        ) == ("legacy-match", 1, 9001, "team_one", None)
        assert [
            int(row[0])
            for row in migrated.execute(
                "SELECT version FROM live_schema_version ORDER BY version"
            )
        ] == [5, LIVE_VERSION]

        with pytest.raises(
            sqlite3.IntegrityError,
            match="settlement reconciliation mapping authority is required",
        ):
            migrated.execute(
                """INSERT INTO settlement_reconciliations
                   (raybet_match_id, map_number, strict_mapping_id, dota_match_id,
                    raybet_winner_side, opendota_winner_side,
                    raybet_evidence_ref, opendota_evidence_ref, status, reason,
                    first_observed_at, updated_at)
                   VALUES ('new-match', 1, NULL, 9002, 'team_one', 'team_one',
                           'raybet:new', 'opendota:9002', 'manual_review',
                           'missing_source_authority', '2026-07-17T11:00:00+00:00',
                           '2026-07-17T11:00:00+00:00')"""
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="map result mapping authority is required",
        ):
            migrated.execute(
                """INSERT INTO map_results
                   (raybet_match_id, map_number, strict_mapping_id, dota_match_id,
                    winner_side, team_one_kills, team_two_kills,
                    duration_seconds, evidence_ref, settled_at)
                   VALUES ('new-match', 1, NULL, 9002, 'team_one', 30, 20,
                           2400, 'settlement-reconciliation:new-match:map:1',
                           '2026-07-17T11:00:00+00:00')"""
            )

        for statement in (
            """UPDATE settlement_reconciliations
                  SET raybet_match_id='rebound-match'
                WHERE raybet_match_id='legacy-match' AND map_number=1""",
            """UPDATE settlement_reconciliations
                  SET map_number=2
                WHERE raybet_match_id='legacy-match' AND map_number=1""",
            """UPDATE settlement_reconciliations
                  SET strict_mapping_id=1
                WHERE raybet_match_id='legacy-match' AND map_number=1""",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                migrated.execute(statement)

        with pytest.raises(sqlite3.IntegrityError, match="map results are immutable"):
            migrated.execute(
                """UPDATE map_results SET winner_side='team_two'
                    WHERE raybet_match_id='legacy-match' AND map_number=1"""
            )
        with pytest.raises(sqlite3.IntegrityError, match="map results are immutable"):
            migrated.execute(
                """DELETE FROM map_results
                    WHERE raybet_match_id='legacy-match' AND map_number=1"""
            )
    finally:
        migrated.close()


def test_verify_prepared_database_is_read_only_and_creates_no_backup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "prepared.db"
    prepare_database(database, tmp_path / "migration-backups")
    backup_dir = tmp_path / "routine-backups"

    result = verify_prepared_database(database)

    assert result.backup is None
    assert result.live_schema_version == LIVE_VERSION
    assert result.intelligence_schema_version == INTELLIGENCE_VERSION
    assert not backup_dir.exists()


@pytest.mark.parametrize(
    "table",
    (
        "raybet_matches",
        "provider_matches",
        "match_links",
        "matches",
        "match_players",
        "player_map_facts",
        "model_quotes",
        "odds_response_outcomes",
        "strict_live_map_mapping_audit",
    ),
)
def test_verify_prepared_database_rejects_missing_core_table(
    tmp_path: Path,
    table: str,
) -> None:
    database = tmp_path / "drifted.db"
    prepare_database(database, tmp_path / "migration-backups")
    connection = connect(database)
    connection.execute(f'DROP TABLE "{table}"')
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="schema contract failed: missing tables"):
        verify_prepared_database(database)


def test_verify_prepared_database_rejects_incomplete_table_contract(
    tmp_path: Path,
) -> None:
    database = tmp_path / "incomplete.db"
    prepare_database(database, tmp_path / "migration-backups")
    connection = connect(database)
    connection.executescript(
        """DROP TABLE raybet_matches;
           CREATE TABLE raybet_matches (raybet_match_id TEXT PRIMARY KEY);"""
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="raybet_matches missing columns"):
        verify_prepared_database(database)


@pytest.mark.parametrize(
    ("table", "old", "new"),
    (
        (
            "browser_events",
            "payload_json TEXT NOT NULL",
            "payload_json TEXT",
        ),
        (
            "live_frames",
            "sequence TEXT NOT NULL DEFAULT ''",
            "sequence TEXT NOT NULL DEFAULT 'drifted'",
        ),
    ),
)
def test_verify_prepared_database_rejects_column_constraint_drift(
    tmp_path: Path,
    table: str,
    old: str,
    new: str,
) -> None:
    database = tmp_path / f"column-contract-{table}.db"
    prepare_database(database, tmp_path / "migration-backups")
    _rewrite_table_definition(database, table, old, new)

    with pytest.raises(RuntimeError, match="column constraint mismatch"):
        verify_prepared_database(database)


@pytest.mark.parametrize(
    ("table", "old", "new"),
    (
        (
            "odds_transport_observations",
            "CHECK (source IN ('direct', 'browser'))",
            "CHECK (source IN ('direct', 'browser', 'drifted'))",
        ),
        (
            "strict_live_map_mappings",
            "CHECK (map_number <= raybet_best_of)",
            "CHECK (map_number < raybet_best_of)",
        ),
    ),
)
def test_verify_prepared_database_rejects_check_constraint_drift(
    tmp_path: Path,
    table: str,
    old: str,
    new: str,
) -> None:
    database = tmp_path / f"check-contract-{table}.db"
    prepare_database(database, tmp_path / "migration-backups")
    _rewrite_table_definition(database, table, old, new)

    with pytest.raises(RuntimeError, match="check constraint mismatch"):
        verify_prepared_database(database)


def test_prepare_database_repairs_v9_shadow_order_stake_constraint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live-v9-shadow-order-stake.db"
    prepare_database(database, tmp_path / "initial-backups")
    connection = connect(database, read_only=True)
    try:
        expected_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_xinfo(shadow_orders)")
        }
    finally:
        connection.close()
    assert len(expected_columns) == 61
    _rewrite_table_definition(
        database,
        "shadow_orders",
        "stake REAL NOT NULL CHECK (stake>0.0 AND stake<=1.0)",
        "stake REAL NOT NULL",
    )
    connection = connect(database)
    try:
        trigger_names = [
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                     WHERE type='trigger' AND tbl_name='shadow_orders'"""
            )
        ]
        for name in trigger_names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, odds_id, market_key, signaled_at,
                model_probability, market_probability, signal_price,
                signal_transport_key, signal_transport_at, expires_at,
                signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status)
               VALUES ('preserved-order', 'match-1', 'odds-1',
                       'winner|map_1|team_one|', '2026-07-22T00:00:00+00:00',
                       0.6, 0.5, 2.0, 'transport-1',
                       '2026-07-22T00:00:00+00:00',
                       '2026-07-22T00:00:15+00:00', 'group-1', 'team_one',
                       1, 0.5, 'pending')"""
        )
        connection.execute(
            """CREATE TABLE application_order_refs (
                   ref_key TEXT PRIMARY KEY,
                   order_key TEXT NOT NULL REFERENCES shadow_orders(order_key)
               )"""
        )
        connection.execute(
            "INSERT INTO application_order_refs VALUES ('ref-1', 'preserved-order')"
        )
        connection.execute("DELETE FROM live_schema_version")
        connection.execute("INSERT INTO live_schema_version VALUES (9, 'v9')")
        connection.commit()
    finally:
        connection.close()

    result = prepare_database(database, tmp_path / "migration-backups")

    assert result.live_schema_version == LIVE_VERSION
    verify_prepared_database(database)
    connection = connect(database, read_only=True)
    try:
        checks = database_protocol._schema_contract(connection).checks["shadow_orders"]
        assert "stake > 0.0 and stake <= 1.0" in checks
        actual_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_xinfo(shadow_orders)")
        }
        assert actual_columns == expected_columns
        assert connection.execute(
            """SELECT 1 FROM sqlite_master
                 WHERE type='trigger'
                   AND name='strict_live_shadow_impact_after_insert'"""
        ).fetchone() is not None
        assert tuple(
            connection.execute(
                "SELECT order_key, stake FROM shadow_orders WHERE order_key=?",
                ("preserved-order",),
            ).fetchone()
        ) == ("preserved-order", 0.5)
        assert tuple(
            connection.execute(
                "SELECT ref_key, order_key FROM application_order_refs"
            ).fetchone()
        ) == ("ref-1", "preserved-order")
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        connection.close()


def test_live_store_direct_init_rejects_shadow_order_stake_rebuild_before_ddl(
    tmp_path: Path,
) -> None:
    database = tmp_path / "direct-init-shadow-order-stake.db"
    prepare_database(database, tmp_path / "initial-backups")
    _rewrite_table_definition(
        database,
        "shadow_orders",
        "stake REAL NOT NULL CHECK (stake>0.0 AND stake<=1.0)",
        "stake REAL NOT NULL",
    )
    connection = connect(database)
    try:
        trigger_names = [
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                     WHERE type='trigger' AND tbl_name='shadow_orders'"""
            )
        ]
        for name in trigger_names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, odds_id, market_key, signaled_at,
                model_probability, market_probability, signal_price,
                signal_transport_key, signal_transport_at, expires_at,
                signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status)
               VALUES ('preserved-order', 'match-1', 'odds-1',
                       'winner|map_1|team_one|', '2026-07-22T00:00:00+00:00',
                       0.6, 0.5, 2.0, 'transport-1',
                       '2026-07-22T00:00:00+00:00',
                       '2026-07-22T00:00:15+00:00', 'group-1', 'team_one',
                       1, 0.5, 'pending')"""
        )
        connection.execute(
            """CREATE TABLE application_order_refs (
                   ref_key TEXT PRIMARY KEY,
                   order_key TEXT NOT NULL REFERENCES shadow_orders(order_key)
               )"""
        )
        connection.execute(
            "INSERT INTO application_order_refs VALUES ('ref-1', 'preserved-order')"
        )
        connection.commit()
    finally:
        connection.close()
    before = _database_dump(database)

    with LiveBettingStore(database) as store:
        with pytest.raises(RuntimeError, match="migrated through prepare_database"):
            store.init_schema()

    assert _database_dump(database) == before
    connection = connect(database, read_only=True)
    try:
        assert tuple(
            connection.execute(
                "SELECT ref_key, order_key FROM application_order_refs"
            ).fetchone()
        ) == ("ref-1", "preserved-order")
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        connection.close()


def test_prepare_database_rejects_v9_shadow_order_invalid_stake(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live-v9-shadow-order-invalid-stake.db"
    prepare_database(database, tmp_path / "initial-backups")
    _rewrite_table_definition(
        database,
        "shadow_orders",
        "stake REAL NOT NULL CHECK (stake>0.0 AND stake<=1.0)",
        "stake REAL NOT NULL",
    )
    connection = connect(database)
    try:
        trigger_names = [
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                     WHERE type='trigger' AND tbl_name='shadow_orders'"""
            )
        ]
        for name in trigger_names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, odds_id, market_key, signaled_at,
                model_probability, market_probability, signal_price,
                signal_transport_key, signal_transport_at, expires_at,
                signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status)
               VALUES ('invalid-stake', 'match-1', 'odds-1',
                       'winner|map_1|team_one|', '2026-07-22T00:00:00+00:00',
                       0.6, 0.5, 2.0, 'transport-1',
                       '2026-07-22T00:00:00+00:00',
                       '2026-07-22T00:00:15+00:00', 'group-1', 'team_one',
                       1, 1.5, 'pending')"""
        )
        connection.execute("DELETE FROM live_schema_version")
        connection.execute("INSERT INTO live_schema_version VALUES (9, 'v9')")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="stake values violate"):
        prepare_database(database, tmp_path / "migration-backups")


@pytest.mark.parametrize(
    ("table", "old", "new"),
    (
        (
            "draft_prediction_validations",
            """artifact_fingerprint TEXT NOT NULL
        CHECK (length(artifact_fingerprint) = 64)""",
            """artifact_fingerprint TEXT
        CHECK (artifact_fingerprint IS NULL OR
               length(artifact_fingerprint) = 64)""",
        ),
        (
            "match_players",
            "firstblood_claimed INTEGER",
            "firstblood_claimed INTEGER DEFAULT 0",
        ),
    ),
)
def test_verify_prepared_database_accepts_known_additive_migration_shape(
    tmp_path: Path,
    table: str,
    old: str,
    new: str,
) -> None:
    database = tmp_path / f"compatible-contract-{table}.db"
    prepare_database(database, tmp_path / "migration-backups")
    _rewrite_table_definition(database, table, old, new)

    result = verify_prepared_database(database)

    assert result.live_schema_version == LIVE_VERSION
    assert result.intelligence_schema_version == INTELLIGENCE_VERSION


@pytest.mark.parametrize(
    ("object_type", "statement", "message"),
    (
        (
            "trigger",
            "DROP TRIGGER browser_events_immutable",
            "missing triggers",
        ),
        (
            "index",
            "DROP INDEX idx_live_odds_match_time",
            "missing indexes",
        ),
        (
            "view",
            "DROP VIEW formal_map_eligibility",
            "missing views",
        ),
    ),
)
def test_verify_prepared_database_rejects_missing_schema_object(
    tmp_path: Path,
    object_type: str,
    statement: str,
    message: str,
) -> None:
    database = tmp_path / f"missing-{object_type}.db"
    prepare_database(database, tmp_path / "migration-backups")
    connection = connect(database)
    connection.execute(statement)
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match=message):
        verify_prepared_database(database)


@pytest.mark.parametrize(
    ("object_type", "statement", "message"),
    (
        (
            "index",
            "CREATE INDEX unauthorized_core_index "
            "ON raybet_matches(raybet_match_id)",
            "unexpected indexes: unauthorized_core_index",
        ),
        (
            "index-runtime",
            "CREATE INDEX unauthorized_runtime_index "
            "ON monitor_process_registry(status)",
            "unexpected indexes: unauthorized_runtime_index",
        ),
        (
            "trigger",
            """CREATE TRIGGER unauthorized_cross_table_trigger
               AFTER INSERT ON application_events
               BEGIN
                   DELETE FROM raybet_matches;
               END""",
            "unexpected triggers: unauthorized_cross_table_trigger",
        ),
        (
            "trigger-unrelated",
            """CREATE TRIGGER unauthorized_application_trigger
               AFTER INSERT ON application_events
               BEGIN
                   INSERT INTO application_audit (event_id, detail)
                   VALUES (NEW.event_id, 'created');
               END""",
            "unexpected triggers: unauthorized_application_trigger",
        ),
        (
            "view",
            """CREATE VIEW unauthorized_core_view AS
               SELECT raybet_match_id FROM raybet_matches""",
            "unexpected views: unauthorized_core_view",
        ),
    ),
)
def test_verify_prepared_database_rejects_unexpected_core_schema_object(
    tmp_path: Path,
    object_type: str,
    statement: str,
    message: str,
) -> None:
    database = tmp_path / f"unexpected-{object_type}.db"
    prepare_database(database, tmp_path / "migration-backups")
    connection = connect(database)
    connection.execute(
        "CREATE TABLE application_events (event_id INTEGER PRIMARY KEY)"
    )
    connection.execute(
        """CREATE TABLE application_audit (
               event_id INTEGER NOT NULL,
               detail TEXT NOT NULL
           )"""
    )
    connection.execute(statement)
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match=message):
        verify_prepared_database(database)


def test_verify_prepared_database_allows_unrelated_application_objects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "application-objects.db"
    prepare_database(database, tmp_path / "migration-backups")
    connection = connect(database)
    connection.executescript(
        """
        CREATE TABLE application_events (
            event_id INTEGER PRIMARY KEY,
            source TEXT NOT NULL UNIQUE
        );
        CREATE TABLE live_matches (
            match_id INTEGER PRIMARY KEY,
            league_id INTEGER,
            state TEXT
        );
        CREATE TABLE live_players (
            player_id INTEGER PRIMARY KEY,
            hero_id INTEGER,
            match_id INTEGER
        );
        CREATE TABLE live_winrates (
            winrate_id INTEGER PRIMARY KEY,
            match_id INTEGER
        );
        CREATE INDEX idx_live_matches_league ON live_matches(league_id);
        CREATE INDEX idx_live_matches_state ON live_matches(state);
        CREATE INDEX idx_live_players_hero ON live_players(hero_id);
        CREATE INDEX idx_live_players_match ON live_players(match_id);
        CREATE INDEX idx_live_winrates_match ON live_winrates(match_id);
        """
    )
    object_names = {
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
                 WHERE name IN (
                     'sqlite_autoindex_application_events_1',
                     'monitor_process_registry',
                     'idx_notification_due',
                     'idx_live_matches_league',
                     'idx_live_matches_state',
                     'idx_live_players_hero',
                     'idx_live_players_match',
                     'idx_live_winrates_match'
                 )"""
        )
    }
    connection.commit()
    connection.close()

    result = verify_prepared_database(database)

    assert object_names == {
        "sqlite_autoindex_application_events_1",
        "monitor_process_registry",
        "idx_notification_due",
        "idx_live_matches_league",
        "idx_live_matches_state",
        "idx_live_players_hero",
        "idx_live_players_match",
        "idx_live_winrates_match",
    }
    assert result.live_schema_version == LIVE_VERSION
    assert result.intelligence_schema_version == INTELLIGENCE_VERSION


def test_future_schema_is_rejected_before_backup_or_mutation(tmp_path: Path) -> None:
    database = tmp_path / "future.db"
    connection = connect(database)
    connection.execute(
        "CREATE TABLE live_schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    connection.execute("INSERT INTO live_schema_version VALUES (99, 'future')")
    connection.commit()
    connection.close()
    backup_dir = tmp_path / "backups"

    with pytest.raises(RuntimeError, match="newer than supported"):
        prepare_database(database, backup_dir)

    assert not backup_dir.exists()
    verification = connect(database, read_only=True)
    try:
        assert (
            verification.execute(
                "SELECT 1 FROM sqlite_master WHERE name='provider_matches'"
            ).fetchone()
            is None
        )
        assert (
            verification.execute(
                "SELECT MAX(version) FROM live_schema_version"
            ).fetchone()[0]
            == 99
        )
    finally:
        verification.close()


def test_live_store_itself_rejects_a_future_schema(tmp_path: Path) -> None:
    database = tmp_path / "future-direct.db"
    connection = connect(database)
    connection.execute(
        "CREATE TABLE live_schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    connection.execute("INSERT INTO live_schema_version VALUES (99, 'future')")
    connection.commit()
    connection.close()

    with LiveBettingStore(database) as store:
        with pytest.raises(RuntimeError, match="newer than supported"):
            store.init_schema()
        assert (
            store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='provider_matches'"
            ).fetchone()
            is None
        )


def test_live_current_official_rosh_schema_initializes_empty_database_idempotently(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live-current-empty.db"

    with LiveBettingStore(database) as store:
        store.init_schema()
        store.init_schema()
        objects = {
            (str(row[0]), str(row[1]))
            for row in store.connection.execute(
                """SELECT type, name FROM sqlite_master
                    WHERE name LIKE 'rosh_analysis_runs%'
                       OR name LIKE 'rosh_hero_scores%'
                       OR name LIKE 'rosh_minute_points%'
                       OR name LIKE 'official_rosh_shadow_evaluations%'
                       OR name LIKE 'idx_rosh_runs_%'"""
            )
        }
        assert store.connection.execute(
            "SELECT MAX(version) FROM live_schema_version"
        ).fetchone()[0] == LIVE_VERSION
    assert {
        ("table", "rosh_analysis_runs"),
        ("table", "rosh_hero_scores"),
        ("table", "rosh_minute_points"),
        ("table", "official_rosh_shadow_evaluations"),
        ("index", "idx_rosh_runs_match_profile"),
        ("index", "idx_rosh_runs_draft_profile"),
        ("trigger", "rosh_analysis_runs_immutable_update"),
        ("trigger", "rosh_analysis_runs_immutable_delete"),
        ("trigger", "rosh_hero_scores_immutable_update"),
        ("trigger", "rosh_hero_scores_immutable_delete"),
        ("trigger", "rosh_minute_points_immutable_update"),
        ("trigger", "rosh_minute_points_immutable_delete"),
        ("trigger", "official_rosh_shadow_evaluations_immutable_update"),
        ("trigger", "official_rosh_shadow_evaluations_immutable_delete"),
    }.issubset(objects)


def test_live_v10_rosh_migration_is_additive_and_external_transaction_owned(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live-v10-rosh.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
        store.connection.executescript(
            """DROP TABLE official_rosh_shadow_evaluations;
               DROP TABLE rosh_minute_points;
               DROP TABLE rosh_hero_scores;
               DROP TABLE rosh_analysis_runs;
               DELETE FROM live_schema_version;
               INSERT INTO live_schema_version VALUES (10, 'v10');
               CREATE TABLE operator_v10_data (value TEXT NOT NULL);
               INSERT INTO operator_v10_data VALUES ('preserve-me');"""
        )
        store.connection.commit()

        store.connection.execute("BEGIN IMMEDIATE")
        store.init_schema(external_transaction=True)
        assert store.connection.in_transaction
        assert store.connection.execute(
            "SELECT MAX(version) FROM live_schema_version"
        ).fetchone()[0] == LIVE_VERSION
        store.connection.rollback()
        assert store.connection.execute(
            "SELECT MAX(version) FROM live_schema_version"
        ).fetchone()[0] == 10
        assert store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='rosh_analysis_runs'"
        ).fetchone() is None

        store.init_schema()
        store.init_schema()
        assert store.connection.execute(
            "SELECT value FROM operator_v10_data"
        ).fetchone()[0] == "preserve-me"
        assert [
            int(row[0])
            for row in store.connection.execute(
                "SELECT version FROM live_schema_version ORDER BY version"
            )
        ] == [10, LIVE_VERSION]


@pytest.mark.parametrize(
    ("statement", "message"),
    (
        ("DROP TABLE rosh_minute_points", "missing tables: rosh_minute_points"),
        ("DROP INDEX idx_rosh_runs_match_profile", "missing indexes"),
        (
            "DROP TRIGGER rosh_analysis_runs_immutable_update",
            "missing triggers",
        ),
    ),
)
def test_database_protocol_contract_covers_official_rosh_objects(
    tmp_path: Path,
    statement: str,
    message: str,
) -> None:
    database = tmp_path / "rosh-contract.db"
    prepare_database(database, tmp_path / "migration-backups")
    connection = connect(database)
    connection.execute(statement)
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match=message):
        verify_prepared_database(database)


def test_live_v1_schema_migrates_to_current_and_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "live-v1.db"
    connection = connect(database)
    connection.executescript(
        """CREATE TABLE live_schema_version (
               version INTEGER PRIMARY KEY,
               applied_at TEXT NOT NULL
           );
           INSERT INTO live_schema_version VALUES (1, 'v1');
           CREATE TABLE vision_derived_invalidations (
               dependent_type TEXT NOT NULL CHECK (dependent_type IN
                   ('odds_alignment', 'strategy_decision', 'research_prediction',
                    'shadow_order')),
               dependent_key TEXT NOT NULL,
               raybet_match_id TEXT NOT NULL,
               map_number INTEGER NOT NULL,
               reason TEXT NOT NULL,
               recorded_at TEXT NOT NULL,
               PRIMARY KEY (dependent_type, dependent_key)
           );
           INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, recorded_at)
           VALUES ('shadow_order', 'legacy-order', 'match-1', 1,
                   'legacy conflict', '2026-07-15T12:00:00+00:00');
           CREATE TABLE vision_draft_anchors (
               raybet_match_id TEXT NOT NULL,
               map_number INTEGER NOT NULL,
               draft_hash TEXT NOT NULL,
               radiant_hero_ids TEXT NOT NULL,
               dire_hero_ids TEXT NOT NULL,
               anchored_at TEXT NOT NULL,
               source_frame_ref TEXT NOT NULL,
               status TEXT NOT NULL,
               conflict_at TEXT,
               PRIMARY KEY (raybet_match_id, map_number)
           );
           CREATE TABLE vision_draft_conflicts (
               conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
               raybet_match_id TEXT NOT NULL,
               map_number INTEGER NOT NULL,
               captured_at TEXT NOT NULL,
               source_frame_ref TEXT NOT NULL,
               observed_draft_hash TEXT NOT NULL,
               radiant_hero_ids TEXT NOT NULL,
               dire_hero_ids TEXT NOT NULL,
               reason TEXT NOT NULL,
               recorded_at TEXT NOT NULL,
               UNIQUE (raybet_match_id, map_number, captured_at, source_frame_ref)
           );
           INSERT INTO vision_draft_anchors VALUES (
               'match-1', 1,
               'd67fcba0a6956921cceee6d723126ba8928a468c6753c43835db325a6dda8a6a',
               '[1,2,3,4,5]', '[6,7,8,9,10]',
               '2026-07-15T12:00:00+00:00', 'legacy-frame',
               'anchored', NULL
           );"""
    )
    connection.close()

    with LiveBettingStore(database) as store:
        store.init_schema()
        store.init_schema()
        columns = {
            str(row[1])
            for row in store.connection.execute(
                "PRAGMA table_info(vision_derived_invalidations)"
            )
        }
        assert "block_reason" in columns
        assert tuple(store.connection.execute(
            """SELECT reason, block_reason
                 FROM vision_derived_invalidations
                WHERE dependent_key='legacy-order'"""
        ).fetchone()) == ("legacy conflict", "vision_draft_conflict")
        anchor = store.connection.execute(
            """SELECT radiant_team_side, team_side_anchored_at,
                      team_side_source_frame_ref
                 FROM vision_draft_anchors
                WHERE raybet_match_id='match-1' AND map_number=1"""
        ).fetchone()
        assert tuple(anchor) == (None, None, None)
        assert "observed_radiant_team_side" in {
            str(row[1])
            for row in store.connection.execute(
                "PRAGMA table_info(vision_draft_conflicts)"
            )
        }
        promoted_at = datetime(2026, 7, 15, 12, 0, 1, tzinfo=timezone.utc)
        assert store.insert_vision_observation(VisionObservation(
            "match-1", 1, promoted_at, 600, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "side-frame", "game", "team_two",
        ))
        promoted = store.connection.execute(
            """SELECT radiant_team_side, team_side_anchored_at,
                      team_side_source_frame_ref
                 FROM vision_draft_anchors
                WHERE raybet_match_id='match-1' AND map_number=1"""
        ).fetchone()
        assert tuple(promoted) == (None, None, None)
        assert store.connection.execute(
            """SELECT confirmed FROM vision_observations
                WHERE raybet_match_id='match-1'
                  AND source_frame_ref='side-frame'"""
        ).fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.connection.execute(
                """UPDATE vision_draft_anchors
                      SET radiant_team_side='team_one'
                    WHERE raybet_match_id='match-1' AND map_number=1"""
            )
        assert [
            int(row[0])
            for row in store.connection.execute(
                "SELECT version FROM live_schema_version ORDER BY version"
            )
        ] == [1, LIVE_VERSION]


def test_live_v8_migration_installs_bounded_monitor_candidate_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live-v8-monitor.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
        store.connection.executescript(
            """DROP TRIGGER raybet_match_activity_from_transport;
               DROP TRIGGER raybet_match_activity_from_snapshot;
               DROP TABLE raybet_match_odds_activity;
               DROP INDEX idx_raybet_matches_status_updated;
               DROP INDEX idx_raybet_matches_updated;
               DROP INDEX idx_raybet_matches_schedule_utc;
               DROP INDEX idx_raybet_matches_ended_schedule_review;
               DROP INDEX idx_raybet_matches_timeline;
               DROP INDEX idx_vision_confirmed_game_captured;
               DELETE FROM live_schema_version;
               INSERT INTO live_schema_version VALUES (8, 'v8');"""
        )
        for match_id in ("match-a", "match-b"):
            store.connection.execute(
                """INSERT INTO raybet_matches
                   (raybet_match_id, tournament, team_one, team_two,
                    scheduled_at, best_of, status, live_url, raw_json, updated_at)
                   VALUES (?, 'Cup', 'One', 'Two', NULL, 3, '2', NULL, '{}', ?)""",
                (match_id, "2026-07-18T00:00:00+00:00"),
            )
        store.connection.executemany(
            """INSERT INTO odds_snapshots
               (raybet_match_id, odds_id, odds_group_id, received_at, price,
                status, market_type, period, side, line, outcome_key, supported,
                last_update, raw_json)
               VALUES (?, ?, 'winner', ?, 2.0, '1', 'winner', 'map_1',
                       'team_one', NULL, 'team_one', 1, NULL, '{}')""",
            (
                ("match-a", "a-old", "2026-07-18T00:01:00+00:00"),
                ("match-a", "a-new", "2026-07-18T00:03:00+00:00"),
                ("match-b", "b-only", "2026-07-18T00:02:00+00:00"),
            ),
        )
        store.connection.commit()

    with LiveBettingStore(database) as migrated:
        migrated.init_schema()
        migrated.init_schema()
        assert [
            int(row[0])
            for row in migrated.connection.execute(
                "SELECT version FROM live_schema_version ORDER BY version"
            )
        ] == [8, LIVE_VERSION]
        installed = {
            str(row[0])
            for row in migrated.connection.execute(
                """SELECT name FROM sqlite_master
                    WHERE name IN (
                        'idx_raybet_matches_status_updated',
                        'idx_raybet_matches_updated',
                        'idx_raybet_matches_schedule_utc',
                        'idx_raybet_matches_ended_schedule_review',
                        'idx_raybet_matches_timeline',
                        'idx_vision_confirmed_game_captured',
                        'idx_raybet_match_odds_activity_time',
                        'raybet_match_activity_from_transport',
                        'raybet_match_activity_from_snapshot'
                    )"""
            )
        }
        assert len(installed) == 9
        activity = migrated.connection.execute(
            """SELECT match_row.raybet_match_id,
                      cache.latest_odds_activity_at,
                      (SELECT MAX(snapshot.received_at)
                         FROM odds_snapshots AS snapshot
                        WHERE snapshot.raybet_match_id=match_row.raybet_match_id)
                 FROM raybet_matches AS match_row
                 JOIN raybet_match_odds_activity AS cache
                   ON cache.raybet_match_id=match_row.raybet_match_id
                ORDER BY match_row.raybet_match_id"""
        ).fetchall()
        assert [tuple(row) for row in activity] == [
            ("match-a", "2026-07-18T00:03:00+00:00", "2026-07-18T00:03:00+00:00"),
            ("match-b", "2026-07-18T00:02:00+00:00", "2026-07-18T00:02:00+00:00"),
        ]


def test_prepare_database_migrates_live_v3_contract_to_current(tmp_path: Path) -> None:
    database = tmp_path / "live-v3.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
        store.connection.execute(
            "CREATE TABLE operator_marker (value TEXT NOT NULL)"
        )
        store.connection.execute("INSERT INTO operator_marker VALUES ('preserved')")
        store.connection.execute(
            "DROP TRIGGER shadow_orders_require_strict_mapping_insert"
        )
        store.connection.execute(
            "DROP TRIGGER shadow_order_draft_authority_insert"
        )
        store.connection.execute(
            "DROP TRIGGER strategy_decision_draft_authority_insert"
        )
        store.connection.execute(
            "DROP TRIGGER shadow_order_vision_authority_insert"
        )
        store.connection.execute(
            "DROP TRIGGER strategy_decision_vision_authority_insert"
        )
        store.connection.execute(
            "DROP TRIGGER settlements_authority_insert_guard"
        )
        signal_at = "2026-07-15T12:00:00+00:00"
        expires_at = "2026-07-15T12:00:15+00:00"

        def legacy_order_key(match_id: str, odds_id: str, input_ref: str) -> str:
            identity = "|".join(
                (
                    match_id,
                    odds_id,
                    "winner-group",
                    "team_one",
                    "winner|map_1|team_one|",
                    "legacy-strategy-v1",
                    input_ref,
                )
            )
            return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

        def insert_legacy_order(
            match_id: str,
            *,
            strict_mapping_id: int | None,
            status: str = "pending",
            decision_mappings: tuple[int, ...] = (),
        ) -> str:
            odds_id = f"odds-{match_id}"
            input_ref = f"input-{match_id}"
            order_key = legacy_order_key(match_id, odds_id, input_ref)
            store.connection.execute(
                """INSERT INTO shadow_orders
                   (order_key, raybet_match_id, strict_mapping_id, odds_id,
                    market_key, signaled_at, model_probability,
                    market_probability, signal_price, signal_transport_key,
                    signal_transport_at, expires_at, signal_odds_group_id,
                    signal_outcome_key, signal_identity_verified, stake, status,
                    fill_price, filled_at, rejection_reason)
                   VALUES (?, ?, ?, ?, 'winner|map_1|team_one|', ?, 0.6, 0.5,
                           2.0, ?, ?, ?, 'winner-group', 'team_one', 1, 1.0,
                           ?, ?, ?, NULL)""",
                (
                    order_key,
                    match_id,
                    strict_mapping_id,
                    odds_id,
                    signal_at,
                    f"signal-{match_id}",
                    signal_at,
                    expires_at,
                    status,
                    2.0 if status == "filled" else None,
                    signal_at if status == "filled" else None,
                ),
            )
            store.connection.execute(
                """INSERT INTO shadow_map_attempts
                   (raybet_match_id, map_number, order_key, status, created_at)
                   VALUES (?, 1, ?, ?, ?)""",
                (match_id, order_key, status, signal_at),
            )
            for index, mapping_id in enumerate(decision_mappings, start=1):
                contributions = json.dumps(
                    {
                        "__inputs__": {
                            "strict_live_eligibility": {
                                "mapping_refs": {"strict_mapping_id": mapping_id}
                            }
                        }
                    },
                    sort_keys=True,
                )
                store.connection.execute(
                    """INSERT INTO strategy_decisions
                       (decision_key, raybet_match_id, map_number, decided_at,
                        underdog_side, market_probability, model_probability,
                        edge, data_quality, eligible, reason, contributions_json,
                        input_ref, strategy_version)
                       VALUES (?, ?, 1, ?, 'team_one', 0.5, 0.6, 0.1, 0.8, 1,
                               'eligible', ?, ?, 'legacy-strategy-v1')""",
                    (
                        f"decision-{match_id}-{index}",
                        match_id,
                        signal_at,
                        contributions,
                        input_ref,
                    ),
                )
            return order_key

        unique_key = insert_legacy_order(
            "match-unique", strict_mapping_id=None, decision_mappings=(7,)
        )
        missing_key = insert_legacy_order(
            "match-missing", strict_mapping_id=8
        )
        ambiguous_key = insert_legacy_order(
            "match-ambiguous",
            strict_mapping_id=7,
            decision_mappings=(7, 8),
        )
        filled_key = insert_legacy_order(
            "match-filled", strict_mapping_id=9, status="filled"
        )
        store.connection.execute(
            """INSERT INTO settlements
               (order_key, result, return_units, settled_at, evidence_ref,
                review_required) VALUES (?, 'win', 2.0, ?, 'legacy-result', 0)""",
            (filled_key, signal_at),
        )
        store.connection.execute(
            """INSERT INTO notification_outbox
               (order_key, event_type, channel, status, recipient, message_id,
                payload_json, statistics_cutoff, template_version,
                attempt_count, next_attempt_at, created_at, updated_at)
               VALUES (?, 'filled', 'email', 'pending', 'ops@example.com', ?,
                       '{}', ?, 'dota2-shadow-email-v1', 0, ?, ?, ?)""",
            (
                filled_key,
                "<legacy-filled@example.invalid>",
                signal_at,
                signal_at,
                signal_at,
                signal_at,
            ),
        )
        store.connection.execute("DROP TABLE shadow_order_decision_lineage")
        store.connection.execute("DROP TRIGGER strategy_decisions_immutable_update")
        store.connection.execute("DROP TRIGGER strategy_decisions_immutable_delete")
        store.connection.execute("DROP TRIGGER shadow_orders_terminal_immutable")
        store.connection.execute("DROP TRIGGER shadow_orders_immutable_delete")
        store.connection.execute("DROP TRIGGER settlements_core_immutable")
        store.connection.execute("DROP TRIGGER settlements_immutable_delete")
        store.connection.execute("DELETE FROM live_schema_version")
        store.connection.execute(
            "INSERT INTO live_schema_version VALUES (3, 'v3')"
        )
        store.connection.commit()

    result = prepare_database(
        database,
        tmp_path / "backups",
        now=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
    )

    assert result.live_schema_version == LIVE_VERSION
    assert result.backup is not None and result.backup.is_file()
    verification = verify_prepared_database(database)
    assert verification.live_schema_version == LIVE_VERSION
    connection = connect(database, read_only=True)
    try:
        assert connection.execute(
            "SELECT value FROM operator_marker"
        ).fetchone()[0] == "preserved"
        assert [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM live_schema_version ORDER BY version"
            )
        ] == [3, LIVE_VERSION]
        objects = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                """SELECT type, name FROM sqlite_master
                    WHERE name IN (
                        'shadow_order_decision_lineage',
                        'strategy_decisions_immutable_update',
                        'strategy_decisions_immutable_delete',
                        'shadow_orders_terminal_immutable',
                        'shadow_orders_immutable_delete',
                        'settlements_core_immutable',
                        'settlements_immutable_delete'
                    )"""
            )
        }
        assert objects == {
            ("table", "shadow_order_decision_lineage"),
            ("trigger", "strategy_decisions_immutable_update"),
            ("trigger", "strategy_decisions_immutable_delete"),
            ("trigger", "shadow_orders_terminal_immutable"),
            ("trigger", "shadow_orders_immutable_delete"),
            ("trigger", "settlements_core_immutable"),
            ("trigger", "settlements_immutable_delete"),
        }
        assert connection.execute(
            """SELECT 1 FROM shadow_order_decision_lineage
                WHERE order_key=?""",
            (unique_key,),
        ).fetchone() is None
        for order_key in (unique_key, missing_key, ambiguous_key):
            order = connection.execute(
                """SELECT orders.status, attempt.status, orders.rejection_reason
                     FROM shadow_orders AS orders
                     JOIN shadow_map_attempts AS attempt
                       ON attempt.order_key=orders.order_key
                    WHERE orders.order_key=?""",
                (order_key,),
            ).fetchone()
            assert tuple(order) == (
                "rejected",
                "rejected",
                "decision_lineage_unavailable",
            )
            assert connection.execute(
                """SELECT block_reason FROM vision_derived_invalidations
                    WHERE dependent_type='shadow_order' AND dependent_key=?""",
                (order_key,),
            ).fetchone()[0] == "decision_lineage_unavailable"
        assert tuple(
            connection.execute(
                """SELECT orders.status, settlement.review_required,
                          outbox.status, outbox.last_error
                     FROM shadow_orders AS orders
                     JOIN settlements AS settlement
                       ON settlement.order_key=orders.order_key
                     JOIN notification_outbox AS outbox
                       ON outbox.order_key=orders.order_key
                    WHERE orders.order_key=?""",
                (filled_key,),
            ).fetchone()
        ) == (
            "filled",
            1,
            "dead_letter",
            "decision_lineage_unavailable",
        )
    finally:
        connection.close()


def test_version_one_binary_rejects_current_live_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "live-v2.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
    monkeypatch.setattr(live_storage, "CURRENT_SCHEMA_VERSION", 1)

    with LiveBettingStore(database) as legacy_store:
        with pytest.raises(
            RuntimeError,
            match=rf"version {LIVE_VERSION} is newer than supported",
        ):
            legacy_store.init_schema()
        assert [
            int(row[0])
            for row in legacy_store.connection.execute(
                "SELECT version FROM live_schema_version ORDER BY version"
            )
        ] == [LIVE_VERSION]


def test_existing_database_restores_backup_when_intelligence_init_fails_after_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "existing-phase-failure.db"
    connection = connect(database)
    connection.execute("CREATE TABLE operator_marker (value TEXT NOT NULL)")
    connection.execute("INSERT INTO operator_marker VALUES ('before-migration')")
    connection.commit()
    connection.close()
    observed_live_version: list[int] = []

    def fail_after_live_schema(
        storage: IntelligenceStorage,
        *,
        seed_events: bool = True,
        external_transaction: bool = False,
    ) -> None:
        del seed_events, external_transaction
        migration = storage.connection
        observed_live_version.append(
            int(
                migration.execute(
                    "SELECT MAX(version) FROM live_schema_version"
                ).fetchone()[0]
            )
        )
        raise RuntimeError("injected failure after live schema")

    monkeypatch.setattr(
        database_protocol.IntelligenceStorage,
        "init_schema",
        fail_after_live_schema,
    )

    with pytest.raises(RuntimeError, match="injected failure after live schema"):
        prepare_database(database, tmp_path / "backups")

    assert observed_live_version == [LIVE_VERSION]
    restored = connect(database, read_only=True)
    try:
        assert restored.execute("SELECT value FROM operator_marker").fetchone()[0] == (
            "before-migration"
        )
        assert (
            restored.execute(
                "SELECT 1 FROM sqlite_master WHERE name='live_schema_version'"
            ).fetchone()
            is None
        )
    finally:
        restored.close()
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_fresh_database_is_removed_when_intelligence_init_fails_after_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "fresh-phase-failure.db"
    observed_live_version: list[int] = []

    def fail_after_live_schema(
        storage: IntelligenceStorage,
        *,
        seed_events: bool = True,
        external_transaction: bool = False,
    ) -> None:
        del seed_events, external_transaction
        migration = storage.connection
        observed_live_version.append(
            int(
                migration.execute(
                    "SELECT MAX(version) FROM live_schema_version"
                ).fetchone()[0]
            )
        )
        raise RuntimeError("injected failure after live schema")

    monkeypatch.setattr(
        database_protocol.IntelligenceStorage,
        "init_schema",
        fail_after_live_schema,
    )

    with pytest.raises(RuntimeError, match="injected failure after live schema"):
        prepare_database(database, tmp_path / "backups")

    assert observed_live_version == [LIVE_VERSION]
    assert not database.exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    assert not (tmp_path / "backups").exists()


def test_empty_database_file_is_removed_when_intelligence_init_fails_after_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "empty-phase-failure.db"
    database.touch()

    def fail_after_live_schema(
        storage: IntelligenceStorage,
        *,
        seed_events: bool = True,
        external_transaction: bool = False,
    ) -> None:
        del seed_events, external_transaction
        assert (
            storage.connection.execute(
                "SELECT MAX(version) FROM live_schema_version"
            ).fetchone()[0]
            == LIVE_VERSION
        )
        raise RuntimeError("injected failure after live schema")

    monkeypatch.setattr(
        database_protocol.IntelligenceStorage,
        "init_schema",
        fail_after_live_schema,
    )

    with pytest.raises(RuntimeError, match="injected failure after live schema"):
        prepare_database(database, tmp_path / "backups")

    assert not database.exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    assert not (tmp_path / "backups").exists()


def test_failed_migration_restores_the_online_backup(tmp_path: Path) -> None:
    database = tmp_path / "incompatible.db"
    connection = connect(database)
    connection.execute("CREATE TABLE event_registry (event_id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO event_registry VALUES ('operator-row')")
    connection.commit()
    connection.close()

    with pytest.raises(sqlite3.OperationalError):
        prepare_database(
            database,
            tmp_path / "backups",
            now=datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc),
        )

    verification = connect(database, read_only=True)
    try:
        assert verification.execute("SELECT event_id FROM event_registry").fetchone()[
            0
        ] == ("operator-row")
        assert (
            verification.execute(
                "SELECT 1 FROM sqlite_master WHERE name='provider_matches'"
            ).fetchone()
            is None
        )
        assert (
            verification.execute(
                "SELECT 1 FROM sqlite_master WHERE name='live_schema_version'"
            ).fetchone()
            is None
        )
    finally:
        verification.close()
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_operator_restore_preserves_the_replaced_database_online(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "source-backup.db"
    database = tmp_path / "current.db"
    safety = tmp_path / "before-restore.db"
    for path, value in ((backup, "from-backup"), (database, "before-restore")):
        connection = connect(path)
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (?)", (value,))
        connection.commit()
        connection.close()

    saved = restore_database_backup(backup, database, safety_backup=safety)

    assert saved == safety.resolve()
    restored = connect(database, read_only=True)
    preserved = connect(safety, read_only=True)
    try:
        assert restored.execute("SELECT value FROM marker").fetchone()[0] == (
            "from-backup"
        )
        assert preserved.execute("SELECT value FROM marker").fetchone()[0] == (
            "before-restore"
        )
    finally:
        restored.close()
        preserved.close()
