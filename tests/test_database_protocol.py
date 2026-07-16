from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import live_betting.database_protocol as database_protocol
import live_betting.storage as live_storage
from event_intelligence.storage import (
    CURRENT_SCHEMA_VERSION as INTELLIGENCE_VERSION,
    IntelligenceStorage,
)
from live_betting.database_protocol import (
    check_schema_versions,
    online_backup,
    prepare_database,
    restore_database_backup,
    verify_prepared_database,
)
from live_betting.storage import CURRENT_SCHEMA_VERSION as LIVE_VERSION
from live_betting.storage import LiveBettingStore
from live_betting.vision import VisionObservation
from shared.sqlite import connect


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
    verification = connect(database, read_only=True)
    try:
        assert (
            verification.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='draft_lineage_revisions'"
            ).fetchone()
            is not None
        )
    finally:
        verification.close()


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


def test_live_v1_schema_migrates_to_v4_and_is_idempotent(tmp_path: Path) -> None:
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
        assert tuple(promoted) == (
            "team_two", promoted_at.isoformat(), "side-frame"
        )
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
        ] == [1, 4]


def test_prepare_database_migrates_live_v3_contract_to_v4(tmp_path: Path) -> None:
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

    assert result.live_schema_version == 4
    assert result.backup is not None and result.backup.is_file()
    verification = verify_prepared_database(database)
    assert verification.live_schema_version == 4
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
        ] == [3, 4]
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
        unique = connection.execute(
            """SELECT orders.status, orders.strict_mapping_id,
                      lineage.decision_key
                 FROM shadow_orders AS orders
                 JOIN shadow_order_decision_lineage AS lineage
                   ON lineage.order_key=orders.order_key
                WHERE orders.order_key=?""",
            (unique_key,),
        ).fetchone()
        assert tuple(unique) == ("pending", 7, "decision-match-unique-1")
        for order_key in (missing_key, ambiguous_key):
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


def test_version_one_binary_rejects_live_v4_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "live-v2.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
    monkeypatch.setattr(live_storage, "CURRENT_SCHEMA_VERSION", 1)

    with LiveBettingStore(database) as legacy_store:
        with pytest.raises(RuntimeError, match="version 4 is newer than supported"):
            legacy_store.init_schema()
        assert [
            int(row[0])
            for row in legacy_store.connection.execute(
                "SELECT version FROM live_schema_version ORDER BY version"
            )
        ] == [4]


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
    ) -> None:
        del seed_events
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
    ) -> None:
        del seed_events
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
    ) -> None:
        del seed_events
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
