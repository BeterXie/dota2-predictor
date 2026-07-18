from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from live_betting.settlement import (
    SettlementAuthorityError,
    persist_authoritative_settlement_snapshot,
    persisted_settlement_authority_reason,
    record_settlement_authority_review,
    resolve_authoritative_settlement,
    settle_authoritative_order,
)
from live_betting.report import _isolate_unverified_settlements
from live_betting.notifications import (
    EVENT_SETTLED,
    TEMPLATE_VERSION,
    _formal_notification_block_reason,
)
from live_betting.vision_frame_registry import (
    VisionFrameReceipt,
    register_vision_frame_artifact,
)
from tests.draft_authority_fixture import make_test_vision_observation


NOW = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)


def authority_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    vision = make_test_vision_observation(
        raybet_match_id="match-1",
        map_number=1,
        captured_at=NOW,
        label="settlement-authority",
    )
    connection.executescript(
        """
        CREATE TABLE strict_live_map_mappings (
            mapping_id INTEGER PRIMARY KEY,
            raybet_match_id TEXT NOT NULL,
            map_number INTEGER NOT NULL
        );
        CREATE TABLE shadow_orders (
            order_key TEXT PRIMARY KEY,
            raybet_match_id TEXT NOT NULL,
            strict_mapping_id INTEGER,
            market_key TEXT NOT NULL,
            signal_outcome_key TEXT,
            fill_price REAL,
            stake REAL NOT NULL,
            status TEXT NOT NULL,
            filled_at TEXT,
            vision_source_frame_ref TEXT,
            vision_source_frame_sha256 TEXT,
            vision_source_frame_bytes INTEGER
        );
        CREATE TABLE strategy_decisions (
            decision_key TEXT PRIMARY KEY,
            vision_source_frame_ref TEXT,
            vision_source_frame_sha256 TEXT,
            vision_source_frame_bytes INTEGER
        );
        CREATE TABLE shadow_order_decision_lineage (
            order_key TEXT PRIMARY KEY,
            decision_key TEXT NOT NULL
        );
        CREATE TABLE vision_frame_artifacts (
            frame_ref TEXT PRIMARY KEY,
            content_sha256 TEXT NOT NULL,
            byte_length INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            registered_at TEXT NOT NULL
        );
        CREATE TABLE vision_frame_artifact_relocations (
            relocation_id TEXT PRIMARY KEY,
            relocation_sequence INTEGER NOT NULL,
            frame_ref TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            byte_length INTEGER NOT NULL,
            old_storage_path TEXT NOT NULL,
            new_storage_path TEXT NOT NULL,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            relocated_at TEXT NOT NULL
        );
        CREATE TABLE vision_frame_artifact_retirements (
            retirement_id TEXT PRIMARY KEY,
            frame_ref TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            byte_length INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            retired_at TEXT NOT NULL
        );
        CREATE TABLE shadow_map_attempts (
            raybet_match_id TEXT NOT NULL,
            map_number INTEGER NOT NULL,
            order_key TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE settlement_reconciliations (
            raybet_match_id TEXT NOT NULL,
            map_number INTEGER NOT NULL,
            strict_mapping_id INTEGER,
            dota_match_id INTEGER NOT NULL,
            raybet_winner_side TEXT,
            opendota_winner_side TEXT NOT NULL,
            raybet_evidence_ref TEXT NOT NULL,
            opendota_evidence_ref TEXT NOT NULL,
            evidence_ref TEXT,
            raybet_evidence_id INTEGER,
            opendota_evidence_id INTEGER,
            raybet_observed_at TEXT,
            opendota_observed_at TEXT,
            first_usable_at TEXT,
            status TEXT NOT NULL,
            first_observed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE map_results (
            raybet_match_id TEXT NOT NULL,
            map_number INTEGER NOT NULL,
            strict_mapping_id INTEGER,
            dota_match_id INTEGER NOT NULL,
            winner_side TEXT NOT NULL,
            team_one_kills INTEGER,
            team_two_kills INTEGER,
            duration_seconds INTEGER,
            evidence_ref TEXT NOT NULL,
            reconciliation_ref TEXT,
            raybet_evidence_id INTEGER,
            opendota_evidence_id INTEGER,
            raybet_evidence_ref TEXT,
            opendota_evidence_ref TEXT,
            raybet_observed_at TEXT,
            opendota_observed_at TEXT,
            first_usable_at TEXT,
            settled_at TEXT NOT NULL
        );
        CREATE TABLE settlement_result_evidence (
            evidence_id INTEGER PRIMARY KEY,
            raybet_match_id TEXT NOT NULL,
            map_number INTEGER NOT NULL,
            dota_match_id INTEGER,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            winner_side TEXT,
            evidence_ref TEXT NOT NULL,
            facts_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            first_usable_at TEXT,
            raybet_audit_key TEXT,
            raybet_transport_key TEXT,
            raybet_response_state_hash TEXT,
            raybet_response_artifact_hash TEXT,
            opendota_artifact_id TEXT,
            opendota_observation_id TEXT,
            opendota_content_hash TEXT
        );
        CREATE TABLE settlements (
            order_key TEXT PRIMARY KEY,
            result TEXT NOT NULL,
            return_units REAL NOT NULL,
            settled_at TEXT NOT NULL,
            evidence_ref TEXT NOT NULL,
            review_required INTEGER NOT NULL
        );
        CREATE TABLE settlement_authority (
            order_key TEXT PRIMARY KEY,
            raybet_match_id TEXT NOT NULL,
            map_number INTEGER NOT NULL,
            strict_mapping_id INTEGER NOT NULL,
            dota_match_id INTEGER NOT NULL,
            winner_side TEXT NOT NULL,
            fill_price REAL NOT NULL,
            stake_units REAL NOT NULL,
            derived_result TEXT NOT NULL,
            derived_return_units REAL NOT NULL,
            derived_return_amount REAL NOT NULL,
            map_result_evidence_ref TEXT NOT NULL,
            raybet_evidence_ref TEXT NOT NULL,
            opendota_evidence_ref TEXT NOT NULL,
            raybet_evidence_id INTEGER NOT NULL,
            opendota_evidence_id INTEGER NOT NULL,
            raybet_observed_at TEXT NOT NULL,
            opendota_observed_at TEXT NOT NULL,
            first_usable_at TEXT NOT NULL,
            reconciliation_updated_at TEXT NOT NULL,
            settled_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE settlement_authority_audit (
            order_key TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            UNIQUE (order_key, status, reason, actor)
        );
        INSERT INTO strict_live_map_mappings VALUES (7, 'match-1', 1);
        INSERT INTO shadow_map_attempts VALUES (
            'match-1', 1, 'order-1', 'filled'
        );
        INSERT INTO settlement_reconciliations VALUES (
            'match-1', 1, 7, 9001, 'team_one', 'team_one',
            'raybet:final:1', 'opendota:9001',
            'settlement-reconciliation:match-1:map:1', 1, 2,
            '2026-07-17T10:00:00+00:00',
            '2026-07-17T10:00:00+00:00',
            '2026-07-17T10:00:00+00:00', 'confirmed',
            '2026-07-17T10:00:00+00:00',
            '2026-07-17T10:00:00+00:00'
        );
        INSERT INTO map_results VALUES (
            'match-1', 1, 7, 9001, 'team_one', 30, 20, 2400,
            'settlement-reconciliation:match-1:map:1',
            'settlement-reconciliation:match-1:map:1', 1, 2,
            'raybet:final:1', 'opendota:9001',
            '2026-07-17T10:00:00+00:00',
            '2026-07-17T10:00:00+00:00',
            '2026-07-17T10:00:00+00:00',
            '2026-07-17T10:00:00+00:00'
        );
        INSERT INTO settlement_result_evidence VALUES (
            1, 'match-1', 1, 9001, 'raybet', 'confirmed', 'team_one',
            'raybet:final:1',
            '{"raybet_match_id":"match-1","map_number":1,"strict_mapping_id":7,"dota_match_id":9001,"winner_side":"team_one"}',
            '2026-07-17T10:00:00+00:00',
            '2026-07-17T10:00:00+00:00', 'raybet-audit',
            'raybet-transport', 'raybet-state', 'raybet-artifact',
            NULL, NULL, NULL
        );
        INSERT INTO settlement_result_evidence VALUES (
            2, 'match-1', 1, 9001, 'opendota', 'confirmed', 'team_one',
            'opendota:9001',
            '{"raybet_match_id":"match-1","map_number":1,"strict_mapping_id":7,"dota_match_id":9001,"winner_side":"team_one"}',
            '2026-07-17T10:00:00+00:00',
            '2026-07-17T10:00:00+00:00', NULL, NULL, NULL, NULL,
            'opendota-artifact', 'opendota-observation', 'opendota-content'
        );
        """
    )
    register_vision_frame_artifact(
        connection,
        VisionFrameReceipt(
            vision.source_frame_ref,
            str(vision.source_frame_sha256),
            int(vision.source_frame_bytes),
            Path(str(vision.source_frame_path)),
        ),
        registered_at=NOW,
    )
    connection.execute(
        "INSERT INTO strategy_decisions VALUES ('decision-1', ?, ?, ?)",
        (
            vision.source_frame_ref,
            vision.source_frame_sha256,
            vision.source_frame_bytes,
        ),
    )
    connection.execute(
        "INSERT INTO shadow_order_decision_lineage VALUES ('order-1', 'decision-1')"
    )
    connection.execute(
        """INSERT INTO shadow_orders VALUES (
               'order-1', 'match-1', 7, 'winner|map_1|team_one|',
               'team_one', 2.5, 1.0, 'filled',
               '2026-07-17T09:59:00+00:00', ?, ?, ?
           )""",
        (
            vision.source_frame_ref,
            vision.source_frame_sha256,
            vision.source_frame_bytes,
        ),
    )
    return connection


def persist_authority(connection: sqlite3.Connection) -> None:
    authority = resolve_authoritative_settlement(connection, "order-1")
    connection.execute(
        "INSERT INTO settlements VALUES (?, ?, ?, ?, ?, 0)",
        (
            authority.order_key,
            authority.result,
            authority.return_units,
            authority.settled_at.isoformat(),
            authority.map_result_evidence_ref,
        ),
    )
    assert persist_authoritative_settlement_snapshot(connection, authority)


class AuthorityStore:
    """Small transactional store double for authority-write tests."""

    def __init__(self, connection: sqlite3.Connection, *, replace: bool = False):
        self.connection = connection
        self.replace = replace
        self.inserted: list[tuple[object, ...]] = []

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self.connection.in_transaction:
            self.connection.execute("SAVEPOINT authority_test")
            try:
                yield
            except BaseException:
                self.connection.execute("ROLLBACK TO SAVEPOINT authority_test")
                self.connection.execute("RELEASE SAVEPOINT authority_test")
                raise
            else:
                self.connection.execute("RELEASE SAVEPOINT authority_test")
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def insert_settlement(
        self,
        order_key: str,
        result: str,
        return_units: float,
        settled_at: datetime,
        evidence_ref: str,
        review_required: bool = False,
    ) -> bool:
        self.inserted.append(
            (order_key, result, return_units, settled_at, evidence_ref, review_required)
        )
        if self.replace:
            self.connection.execute(
                "UPDATE map_results SET winner_side='team_two' "
                "WHERE raybet_match_id='match-1' AND map_number=1"
            )
        cursor = self.connection.execute(
            "INSERT INTO settlements VALUES (?, ?, ?, ?, ?, ?)",
            (
                order_key,
                result,
                return_units,
                settled_at.isoformat(),
                evidence_ref,
                int(review_required),
            ),
        )
        return cursor.rowcount == 1


def test_authoritative_settlement_derives_result_and_return() -> None:
    connection = authority_connection()
    try:
        resolved = resolve_authoritative_settlement(connection, "order-1")
    finally:
        connection.close()

    assert resolved.strict_mapping_id == 7
    assert resolved.dota_match_id == 9001
    assert resolved.winner_side == "team_one"
    assert resolved.result == "win"
    assert resolved.fill_price == 2.5
    assert resolved.stake == 1.0
    assert resolved.return_units == 2.5
    assert resolved.return_amount == 2.5
    assert resolved.settled_at == NOW


@pytest.mark.parametrize(
    ("filled_at", "reason"),
    (
        (None, "settlement_order_time_invalid"),
        ("2026-07-17T10:00:00+00:00", "settlement_time_order_invalid"),
        ("2026-07-17T10:00:01+00:00", "settlement_time_order_invalid"),
    ),
)
def test_authoritative_settlement_requires_fill_strictly_before_result(
    filled_at: str | None,
    reason: str,
) -> None:
    connection = authority_connection()
    try:
        connection.execute(
            "UPDATE shadow_orders SET filled_at=? WHERE order_key='order-1'",
            (filled_at,),
        )
        with pytest.raises(SettlementAuthorityError, match=reason):
            resolve_authoritative_settlement(connection, "order-1")
    finally:
        connection.close()


def test_authoritative_settlement_accepts_fill_strictly_before_result() -> None:
    connection = authority_connection()
    try:
        connection.execute(
            """UPDATE shadow_orders
                  SET filled_at='2026-07-17T09:59:59.999000+00:00'
                WHERE order_key='order-1'"""
        )

        resolved = resolve_authoritative_settlement(connection, "order-1")

        assert resolved.order_key == "order-1"
        assert resolved.settled_at == NOW
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("statement", "reason"),
    (
        (
            "DELETE FROM settlement_reconciliations",
            "settlement_reconciliation_missing",
        ),
        ("DELETE FROM map_results", "settlement_map_result_missing"),
        (
            "UPDATE settlement_reconciliations SET strict_mapping_id=8",
            "settlement_reconciliation_mismatch",
        ),
        (
            "UPDATE map_results SET strict_mapping_id=8",
            "settlement_map_result_mismatch",
        ),
        (
            "UPDATE map_results SET dota_match_id=9002",
            "settlement_map_result_mismatch",
        ),
        (
            "UPDATE map_results SET winner_side='team_two'",
            "settlement_map_result_mismatch",
        ),
        (
            "UPDATE map_results SET evidence_ref='opendota:9001'",
            "settlement_map_result_evidence_mismatch",
        ),
        (
            "UPDATE settlement_result_evidence SET winner_side='team_two' "
            "WHERE source='raybet'",
            "settlement_source_evidence_mismatch",
        ),
        (
            "UPDATE shadow_map_attempts SET raybet_match_id='other-match'",
            "settlement_order_not_filled",
        ),
    ),
)
def test_authoritative_settlement_rejects_broken_exact_chain(
    statement: str,
    reason: str,
) -> None:
    connection = authority_connection()
    try:
        connection.execute(statement)
        with pytest.raises(SettlementAuthorityError, match=reason):
            resolve_authoritative_settlement(connection, "order-1")
    finally:
        connection.close()


def test_persisted_authority_revalidates_exact_snapshot() -> None:
    connection = authority_connection()
    try:
        persist_authority(connection)
        assert persisted_settlement_authority_reason(connection, "order-1") is None
    finally:
        connection.close()


def test_legacy_settlement_without_authority_fails_closed() -> None:
    connection = authority_connection()
    try:
        authority = resolve_authoritative_settlement(connection, "order-1")
        connection.execute(
            "INSERT INTO settlements VALUES (?, ?, ?, ?, ?, 0)",
            (
                authority.order_key,
                authority.result,
                authority.return_units,
                authority.settled_at.isoformat(),
                authority.map_result_evidence_ref,
            ),
        )
        assert (
            persisted_settlement_authority_reason(connection, "order-1")
            == "settlement_authority_missing"
        )
    finally:
        connection.close()


def test_persisted_authority_detects_later_reconciliation_conflict() -> None:
    connection = authority_connection()
    try:
        persist_authority(connection)
        connection.execute(
            "UPDATE settlement_reconciliations SET status='manual_review'"
        )
        assert (
            persisted_settlement_authority_reason(connection, "order-1")
            == "settlement_reconciliation_not_confirmed"
        )
    finally:
        connection.close()


def test_persisted_authority_detects_snapshot_mismatch() -> None:
    connection = authority_connection()
    try:
        persist_authority(connection)
        connection.execute("UPDATE settlement_authority SET strict_mapping_id=8")
        assert (
            persisted_settlement_authority_reason(connection, "order-1")
            == "settlement_authority_snapshot_mismatch"
        )
    finally:
        connection.close()


def test_authoritative_write_uses_only_saved_order_and_map_result() -> None:
    connection = authority_connection()
    store = AuthorityStore(connection)
    try:
        assert settle_authoritative_order(store, "order-1") is True
        assert store.inserted == [
            (
                "order-1",
                "win",
                2.5,
                NOW,
                "settlement-reconciliation:match-1:map:1",
                False,
            )
        ]
        assert persisted_settlement_authority_reason(connection, "order-1") is None
    finally:
        connection.close()


def test_missing_chain_records_review_without_formal_ledger() -> None:
    connection = authority_connection()
    store = AuthorityStore(connection)
    try:
        connection.execute("DELETE FROM map_results")
        with pytest.raises(
            SettlementAuthorityError, match="settlement_map_result_missing"
        ) as captured:
            settle_authoritative_order(store, "order-1")
        assert record_settlement_authority_review(
            connection,
            "order-1",
            captured.value.reason,
            actor="test",
        )
        assert connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 0
        assert tuple(connection.execute(
            "SELECT status, reason FROM settlement_authority_audit"
        ).fetchone()) == ("manual_review", "settlement_map_result_missing")
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("statement", "reason"),
    (
        (
            "UPDATE settlement_result_evidence "
            "SET observed_at='2026-07-17T10:00:01+00:00' WHERE source='raybet'",
            "settlement_source_evidence_time_mismatch",
        ),
        (
            "UPDATE settlement_reconciliations "
            "SET first_observed_at='2026-07-17T10:00:01+00:00'",
            "settlement_reconciliation_time_mismatch",
        ),
        (
            "UPDATE shadow_orders "
            "SET filled_at='2026-07-17T10:00:01+00:00'",
            "settlement_time_order_invalid",
        ),
        (
            "UPDATE settlement_result_evidence SET facts_json="
            "'{\"dota_match_id\":9001,\"winner_side\":\"team_one\","
            "\"strict_mapping_id\":8}' WHERE source='opendota'",
            "settlement_source_evidence_mismatch",
        ),
    ),
)
def test_authoritative_write_rejects_field_and_time_mismatch(
    statement: str,
    reason: str,
) -> None:
    connection = authority_connection()
    try:
        connection.execute(statement)
        with pytest.raises(SettlementAuthorityError, match=reason):
            settle_authoritative_order(AuthorityStore(connection), "order-1")
        assert connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM settlement_authority"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_same_transaction_authority_replacement_rolls_back_everything() -> None:
    connection = authority_connection()
    try:
        with pytest.raises(
            SettlementAuthorityError, match="settlement_map_result_mismatch"
        ):
            settle_authoritative_order(
                AuthorityStore(connection, replace=True), "order-1"
            )
        assert connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM settlement_authority"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT winner_side FROM map_results"
        ).fetchone()[0] == "team_one"
    finally:
        connection.close()


def test_legacy_ledger_is_not_backfilled_into_formal_authority() -> None:
    connection = authority_connection()
    try:
        authority = resolve_authoritative_settlement(connection, "order-1")
        connection.execute(
            "INSERT INTO settlements VALUES (?, ?, ?, ?, ?, 0)",
            (
                authority.order_key,
                authority.result,
                authority.return_units,
                authority.settled_at.isoformat(),
                authority.map_result_evidence_ref,
            ),
        )
        with pytest.raises(
            SettlementAuthorityError, match="settlement_authority_missing"
        ):
            settle_authoritative_order(AuthorityStore(connection), "order-1")
        assert connection.execute(
            "SELECT COUNT(*) FROM settlement_authority"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_report_isolates_legacy_settlement_from_scored_results() -> None:
    connection = authority_connection()
    try:
        authority = resolve_authoritative_settlement(connection, "order-1")
        connection.execute(
            "INSERT INTO settlements VALUES (?, ?, ?, ?, ?, 0)",
            (
                authority.order_key,
                authority.result,
                authority.return_units,
                authority.settled_at.isoformat(),
                authority.map_result_evidence_ref,
            ),
        )
        rows = connection.execute(
            """SELECT orders.order_key, orders.status, settlements.result,
                      settlements.review_required
                 FROM shadow_orders AS orders
                 JOIN settlements ON settlements.order_key=orders.order_key"""
        ).fetchall()
        isolated, failures = _isolate_unverified_settlements(connection, rows)
        assert failures == {"settlement_authority_missing": 1}
        assert isolated[0]["reconciliation_status"] is None
        assert isolated[0]["settlement_authority_reason"] == (
            "settlement_authority_missing"
        )
    finally:
        connection.close()


def test_notification_gate_revalidates_exact_settlement_authority() -> None:
    connection = authority_connection()
    try:
        persist_authority(connection)
        baseline = {"order_key": "order-1", "stake_units": 1.0}
        payload = {
            **baseline,
            "template_version": TEMPLATE_VERSION,
            "event_type": EVENT_SETTLED,
            "simulation": True,
            "real_wager_placed": False,
            "decision_lineage_status": "verified",
            "decision_key": "decision-1",
            "decision_input_ref": "input-1",
            "strategy_version": "strategy-1",
            "fill_transport_key": "fill-1",
            "result": "win",
            "return_units": 2.5,
            "profit_loss_units": 1.5,
            "evidence_ref": "settlement-reconciliation:match-1:map:1",
            "settled_at": NOW.isoformat(),
        }
        row = {
            "event_type": EVENT_SETTLED,
            "template_version": TEMPLATE_VERSION,
            "payload_json": json.dumps(payload),
            "statistics_cutoff": NOW.isoformat(),
            "created_at": NOW.isoformat(),
        }
        with (
            patch(
                "live_betting.notifications._stored_entry_payload",
                return_value=baseline,
            ),
            patch(
                "live_betting.notifications.filled_order_payload",
                return_value=baseline,
            ),
        ):
            assert (
                _formal_notification_block_reason(
                    connection, row, "order-1"  # type: ignore[arg-type]
                )
                is None
            )
            connection.execute(
                """UPDATE settlement_result_evidence
                      SET observed_at='2026-07-17T10:00:01+00:00'
                    WHERE source='raybet'"""
            )
            assert _formal_notification_block_reason(
                connection, row, "order-1"  # type: ignore[arg-type]
            ) == "settlement_source_evidence_time_mismatch"
        assert connection.execute(
            """SELECT COUNT(*) FROM settlement_authority_audit
                WHERE reason='settlement_source_evidence_time_mismatch'"""
        ).fetchone()[0] == 1
    finally:
        connection.close()
