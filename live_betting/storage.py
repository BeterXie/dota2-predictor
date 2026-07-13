"""SQLite persistence for live collection and shadow orders."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .models import LiveEvent, LiveFrame, Market, OddsSnapshot, ProviderMatch, ShadowOrder
from .pricing import market_key
from .strategy import attempt_fill, is_open


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS provider_matches (
    provider TEXT NOT NULL,
    provider_match_id TEXT NOT NULL,
    tournament TEXT,
    team_one TEXT,
    team_two TEXT,
    scheduled_at TEXT,
    best_of INTEGER,
    status TEXT,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, provider_match_id)
);
CREATE TABLE IF NOT EXISTS raybet_matches (
    raybet_match_id TEXT PRIMARY KEY,
    tournament TEXT,
    team_one TEXT,
    team_two TEXT,
    scheduled_at TEXT,
    best_of INTEGER,
    status TEXT,
    live_url TEXT,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match_links (
    raybet_match_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_match_id TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (raybet_match_id, provider)
);
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raybet_match_id TEXT NOT NULL,
    odds_id TEXT NOT NULL,
    odds_group_id TEXT,
    received_at TEXT NOT NULL,
    price REAL NOT NULL,
    status TEXT,
    market_type TEXT NOT NULL,
    period TEXT NOT NULL,
    side TEXT,
    line REAL,
    outcome_key TEXT NOT NULL,
    supported INTEGER NOT NULL,
    last_update TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE (raybet_match_id, odds_id, received_at)
);
CREATE INDEX IF NOT EXISTS idx_live_odds_match_time
    ON odds_snapshots(raybet_match_id, received_at);
CREATE TABLE IF NOT EXISTS browser_events (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    capture_session_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    transport TEXT NOT NULL,
    event_type TEXT NOT NULL,
    raybet_match_id TEXT,
    game_id INTEGER,
    page_origin TEXT NOT NULL,
    page_path TEXT NOT NULL,
    source_path TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_bytes INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    capture_reason TEXT,
    extension_version TEXT NOT NULL,
    recognized INTEGER NOT NULL,
    processing_status TEXT NOT NULL,
    processing_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_browser_events_match_time
    ON browser_events(raybet_match_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_browser_events_type_time
    ON browser_events(event_type, captured_at);
CREATE TRIGGER IF NOT EXISTS browser_events_immutable
BEFORE UPDATE ON browser_events
WHEN OLD.event_id IS NOT NEW.event_id
  OR OLD.schema_version IS NOT NEW.schema_version
  OR OLD.capture_session_id IS NOT NEW.capture_session_id
  OR OLD.captured_at IS NOT NEW.captured_at
  OR OLD.received_at IS NOT NEW.received_at
  OR OLD.transport IS NOT NEW.transport
  OR OLD.event_type IS NOT NEW.event_type
  OR OLD.raybet_match_id IS NOT NEW.raybet_match_id
  OR OLD.game_id IS NOT NEW.game_id
  OR OLD.page_origin IS NOT NEW.page_origin
  OR OLD.page_path IS NOT NEW.page_path
  OR OLD.source_path IS NOT NEW.source_path
  OR OLD.payload_hash IS NOT NEW.payload_hash
  OR OLD.payload_bytes IS NOT NEW.payload_bytes
  OR OLD.payload_json IS NOT NEW.payload_json
  OR OLD.capture_reason IS NOT NEW.capture_reason
  OR OLD.extension_version IS NOT NEW.extension_version
  OR OLD.recognized IS NOT NEW.recognized
BEGIN
    SELECT RAISE(ABORT, 'browser event payload is immutable');
END;
CREATE TABLE IF NOT EXISTS odds_transport_observations (
    observation_key TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('direct', 'browser')),
    source_event_id TEXT,
    raybet_match_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    normalized_state_hash TEXT NOT NULL,
    timing_status TEXT NOT NULL,
    processing_status TEXT NOT NULL,
    normalized_change_count INTEGER NOT NULL,
    FOREIGN KEY (source_event_id) REFERENCES browser_events(event_id)
);
CREATE INDEX IF NOT EXISTS idx_odds_transport_match_time
    ON odds_transport_observations(raybet_match_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_odds_transport_hash_time
    ON odds_transport_observations(normalized_state_hash, observed_at);
CREATE TRIGGER IF NOT EXISTS odds_transport_observations_guard_update
BEFORE UPDATE ON odds_transport_observations
WHEN OLD.observation_key IS NOT NEW.observation_key
  OR OLD.source IS NOT NEW.source
  OR OLD.source_event_id IS NOT NEW.source_event_id
  OR OLD.raybet_match_id IS NOT NEW.raybet_match_id
  OR OLD.observed_at IS NOT NEW.observed_at
  OR OLD.normalized_state_hash IS NOT NEW.normalized_state_hash
  OR OLD.timing_status IS NOT NEW.timing_status
  OR NOT (
      (OLD.processing_status IS NEW.processing_status
       AND OLD.normalized_change_count IS NEW.normalized_change_count)
      OR (OLD.processing_status='processing'
          AND NEW.processing_status='processed'
          AND OLD.normalized_change_count=0
          AND NEW.normalized_change_count>=0)
  )
BEGIN
    SELECT RAISE(ABORT, 'odds transport observation is immutable');
END;
CREATE TRIGGER IF NOT EXISTS odds_transport_observations_immutable_delete
BEFORE DELETE ON odds_transport_observations
BEGIN
    SELECT RAISE(ABORT, 'odds transport observation is immutable');
END;
CREATE TABLE IF NOT EXISTS odds_response_outcomes (
    observation_key TEXT NOT NULL,
    raybet_match_id TEXT NOT NULL,
    odds_id TEXT NOT NULL,
    odds_group_id TEXT,
    received_at TEXT NOT NULL,
    price REAL NOT NULL,
    status TEXT,
    market_type TEXT NOT NULL,
    period TEXT NOT NULL,
    side TEXT,
    line REAL,
    outcome_key TEXT NOT NULL,
    supported INTEGER NOT NULL,
    last_update TEXT,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (observation_key, odds_id),
    FOREIGN KEY (observation_key)
        REFERENCES odds_transport_observations(observation_key)
);
CREATE INDEX IF NOT EXISTS idx_odds_response_match_outcome
    ON odds_response_outcomes(raybet_match_id, odds_id, observation_key);
CREATE TRIGGER IF NOT EXISTS odds_response_outcomes_immutable_update
BEFORE UPDATE ON odds_response_outcomes
BEGIN
    SELECT RAISE(ABORT, 'odds response outcome is immutable');
END;
CREATE TRIGGER IF NOT EXISTS odds_response_outcomes_immutable_delete
BEFORE DELETE ON odds_response_outcomes
BEGIN
    SELECT RAISE(ABORT, 'odds response outcome is immutable');
END;
CREATE TABLE IF NOT EXISTS live_frames (
    provider TEXT NOT NULL,
    provider_match_id TEXT NOT NULL,
    provider_game_id TEXT,
    sequence TEXT NOT NULL DEFAULT '',
    source_at TEXT,
    received_at TEXT NOT NULL,
    game_time INTEGER,
    team_one_kills INTEGER,
    team_two_kills INTEGER,
    team_one_gold INTEGER,
    team_two_gold INTEGER,
    state TEXT,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (provider, provider_match_id, provider_game_id, sequence)
);
CREATE TABLE IF NOT EXISTS live_events (
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    provider_match_id TEXT NOT NULL,
    provider_game_id TEXT,
    event_type TEXT NOT NULL,
    source_at TEXT,
    received_at TEXT NOT NULL,
    game_time INTEGER,
    team TEXT,
    player TEXT,
    value REAL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (provider, provider_event_id)
);
CREATE TABLE IF NOT EXISTS model_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raybet_match_id TEXT NOT NULL,
    provider_game_id TEXT,
    market_key TEXT NOT NULL,
    model_probability REAL NOT NULL,
    market_probability REAL NOT NULL,
    edge REAL NOT NULL,
    quoted_at TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    input_ref TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_orders (
    order_key TEXT PRIMARY KEY,
    raybet_match_id TEXT NOT NULL,
    odds_id TEXT NOT NULL,
    market_key TEXT NOT NULL,
    signaled_at TEXT NOT NULL,
    model_probability REAL NOT NULL,
    market_probability REAL NOT NULL,
    signal_price REAL NOT NULL,
    signal_transport_key TEXT NOT NULL,
    signal_transport_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    signal_odds_group_id TEXT,
    signal_outcome_key TEXT,
    signal_identity_verified INTEGER NOT NULL
        CHECK (signal_identity_verified IN (0, 1)),
    stake REAL NOT NULL,
    status TEXT NOT NULL,
    fill_price REAL,
    filled_at TEXT,
    rejection_reason TEXT
);
CREATE TABLE IF NOT EXISTS settlements (
    order_key TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    return_units REAL NOT NULL,
    settled_at TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    review_required INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notification_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_key TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('filled', 'settled')),
    channel TEXT NOT NULL DEFAULT 'email',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'sent', 'dead_letter')),
    recipient TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    statistics_cutoff TEXT NOT NULL,
    template_version TEXT NOT NULL,
    lease_token TEXT,
    lease_until TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    last_error TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (order_key, event_type, channel)
);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
    ON notification_outbox(status, next_attempt_at, lease_until);
CREATE TABLE IF NOT EXISTS notification_outbox_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_health (
    component TEXT PRIMARY KEY,
    status TEXT NOT NULL
        CHECK (status IN ('starting', 'healthy', 'degraded', 'unhealthy', 'stopped')),
    last_heartbeat_at TEXT,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS notification_outbox_payload_immutable
BEFORE UPDATE ON notification_outbox
WHEN OLD.order_key IS NOT NEW.order_key
  OR OLD.event_type IS NOT NEW.event_type
  OR OLD.channel IS NOT NEW.channel
  OR OLD.payload_json IS NOT NEW.payload_json
  OR OLD.statistics_cutoff IS NOT NEW.statistics_cutoff
  OR OLD.template_version IS NOT NEW.template_version
  OR OLD.recipient IS NOT NEW.recipient
  OR OLD.message_id IS NOT NEW.message_id
BEGIN
    SELECT RAISE(ABORT, 'notification outbox payload is immutable');
END;
CREATE TABLE IF NOT EXISTS collector_runs (
    collector TEXT PRIMARY KEY,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    cursor TEXT,
    gap_detected INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS vision_observations (
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER,
    captured_at TEXT NOT NULL,
    game_clock_seconds INTEGER,
    is_paused INTEGER,
    radiant_hero_ids TEXT NOT NULL,
    dire_hero_ids TEXT NOT NULL,
    radiant_team_side TEXT,
    clock_confidence REAL NOT NULL,
    draft_confidence REAL NOT NULL,
    source_frame_ref TEXT NOT NULL,
    screen_state TEXT NOT NULL,
    confirmed INTEGER NOT NULL,
    PRIMARY KEY (raybet_match_id, captured_at, source_frame_ref)
);
CREATE INDEX IF NOT EXISTS idx_vision_match_map_time
    ON vision_observations(raybet_match_id, map_number, captured_at);
CREATE TABLE IF NOT EXISTS odds_alignments (
    odds_snapshot_id INTEGER PRIMARY KEY,
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER,
    game_clock_seconds INTEGER,
    observation_captured_at TEXT,
    method TEXT NOT NULL,
    lag_seconds REAL,
    usable INTEGER NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_alignment_match_map_time
    ON odds_alignments(raybet_match_id, map_number, game_clock_seconds);
CREATE TABLE IF NOT EXISTS strategy_decisions (
    decision_key TEXT PRIMARY KEY,
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER NOT NULL,
    decided_at TEXT NOT NULL,
    underdog_side TEXT NOT NULL,
    market_probability REAL NOT NULL,
    model_probability REAL NOT NULL,
    edge REAL NOT NULL,
    data_quality REAL NOT NULL,
    eligible INTEGER NOT NULL,
    reason TEXT NOT NULL,
    contributions_json TEXT NOT NULL,
    input_ref TEXT NOT NULL,
    strategy_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_map_attempts (
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER NOT NULL,
    order_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (raybet_match_id, map_number)
);
CREATE TABLE IF NOT EXISTS map_results (
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
"""


class LiveBettingStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._transaction_depth = 0
        self._savepoint_sequence = 0

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "LiveBettingStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def init_schema(self) -> None:
        self.connection.executescript(SCHEMA_SQL)
        self._migrate_shadow_order_signal_fields()
        columns = {row[1] for row in self.connection.execute(
            "PRAGMA table_info(vision_observations)"
        )}
        if "radiant_team_side" not in columns:
            self.connection.execute(
                "ALTER TABLE vision_observations ADD COLUMN radiant_team_side TEXT"
            )
        from .strict_eligibility import init_strict_live_eligibility_schema

        init_strict_live_eligibility_schema(self.connection)
        self.connection.commit()

    def _migrate_shadow_order_signal_fields(self) -> None:
        """Add strict signal identity to databases created by earlier versions."""
        self.connection.execute(
            "DROP TRIGGER IF EXISTS shadow_orders_signal_identity_immutable"
        )
        columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(shadow_orders)")
        }
        additive_columns = {
            "signal_transport_key": "TEXT NOT NULL DEFAULT ''",
            "signal_transport_at": "TEXT NOT NULL DEFAULT ''",
            "expires_at": "TEXT NOT NULL DEFAULT ''",
            "signal_odds_group_id": "TEXT",
            "signal_outcome_key": "TEXT",
            "signal_identity_verified": (
                "INTEGER NOT NULL DEFAULT 0 "
                "CHECK (signal_identity_verified IN (0, 1))"
            ),
        }
        for name, definition in additive_columns.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE shadow_orders ADD COLUMN {name} {definition}"
                )

        rows = self.connection.execute(
            """SELECT order_key, raybet_match_id, signaled_at
                 FROM shadow_orders
                WHERE signal_transport_key IS NULL OR signal_transport_key=''
                   OR signal_transport_at IS NULL OR signal_transport_at=''
                   OR expires_at IS NULL OR expires_at=''"""
        ).fetchall()
        for row in rows:
            signaled_at = datetime.fromisoformat(str(row["signaled_at"]))
            signal_time = self._iso(signaled_at)
            self.connection.execute(
                """UPDATE shadow_orders
                      SET signal_transport_key=?, signal_transport_at=?, expires_at=?
                    WHERE order_key=?""",
                (
                    f"legacy:{row['order_key']}",
                    signal_time,
                    self._iso(signaled_at + timedelta(seconds=15)),
                    str(row["order_key"]),
                ),
            )

        identity_rows = self.connection.execute(
            """SELECT order_key, raybet_match_id, odds_id, market_key,
                      signal_price, signal_transport_key, signal_transport_at
                 FROM shadow_orders
                WHERE signal_identity_verified!=1
                   OR signal_odds_group_id IS NULL OR signal_odds_group_id=''
                   OR signal_outcome_key IS NULL OR signal_outcome_key=''"""
        ).fetchall()
        for row in identity_rows:
            outcome = self.connection.execute(
                """SELECT outcome.odds_group_id, outcome.outcome_key,
                          outcome.price, outcome.status, outcome.supported,
                          outcome.market_type, outcome.period, outcome.side,
                          outcome.line
                     FROM odds_response_outcomes outcome
                     JOIN odds_transport_observations transport
                       ON transport.observation_key=outcome.observation_key
                    WHERE outcome.observation_key=?
                      AND outcome.raybet_match_id=? AND outcome.odds_id=?
                      AND transport.observed_at=?
                      AND transport.timing_status='on_time'
                      AND transport.processing_status='processed'""",
                (
                    str(row["signal_transport_key"]),
                    str(row["raybet_match_id"]),
                    str(row["odds_id"]),
                    str(row["signal_transport_at"]),
                ),
            ).fetchone()
            identity_is_proven = (
                outcome is not None
                and bool(str(outcome["odds_group_id"] or ""))
                and bool(str(outcome["outcome_key"]))
                and float(outcome["price"]) == float(row["signal_price"])
                and is_open(outcome["status"])
                and bool(outcome["supported"])
                and market_key(
                    str(outcome["market_type"]),
                    str(outcome["period"]),
                    outcome["side"],
                    outcome["line"],
                )
                == str(row["market_key"])
            )
            self.connection.execute(
                """UPDATE shadow_orders
                      SET signal_odds_group_id=?, signal_outcome_key=?,
                          signal_identity_verified=?
                    WHERE order_key=?""",
                (
                    outcome["odds_group_id"] if identity_is_proven else None,
                    str(outcome["outcome_key"])
                    if identity_is_proven
                    else None,
                    int(identity_is_proven),
                    str(row["order_key"]),
                ),
            )

        signal_columns = {
            str(row[1]): (int(row[3]), row[4])
            for row in self.connection.execute("PRAGMA table_info(shadow_orders)")
            if str(row[1]) in additive_columns
        }
        expected_columns = {
            "signal_transport_key": (1, None),
            "signal_transport_at": (1, None),
            "expires_at": (1, None),
            "signal_odds_group_id": (0, None),
            "signal_outcome_key": (0, None),
            "signal_identity_verified": (1, None),
        }
        if signal_columns != expected_columns:
            self.connection.execute("DROP TABLE IF EXISTS shadow_orders_strict_migration")
            self.connection.execute(
                """CREATE TABLE shadow_orders_strict_migration (
                    order_key TEXT PRIMARY KEY,
                    raybet_match_id TEXT NOT NULL,
                    odds_id TEXT NOT NULL,
                    market_key TEXT NOT NULL,
                    signaled_at TEXT NOT NULL,
                    model_probability REAL NOT NULL,
                    market_probability REAL NOT NULL,
                    signal_price REAL NOT NULL,
                    signal_transport_key TEXT NOT NULL,
                    signal_transport_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    signal_odds_group_id TEXT,
                    signal_outcome_key TEXT,
                    signal_identity_verified INTEGER NOT NULL
                        CHECK (signal_identity_verified IN (0, 1)),
                    stake REAL NOT NULL,
                    status TEXT NOT NULL,
                    fill_price REAL,
                    filled_at TEXT,
                    rejection_reason TEXT
                )"""
            )
            self.connection.execute(
                """INSERT INTO shadow_orders_strict_migration
                    (order_key, raybet_match_id, odds_id, market_key, signaled_at,
                     model_probability, market_probability, signal_price,
                     signal_transport_key, signal_transport_at, expires_at,
                     signal_odds_group_id, signal_outcome_key,
                     signal_identity_verified, stake, status, fill_price,
                     filled_at, rejection_reason)
                    SELECT order_key, raybet_match_id, odds_id, market_key,
                           signaled_at, model_probability, market_probability,
                           signal_price, signal_transport_key,
                           signal_transport_at, expires_at,
                           signal_odds_group_id, signal_outcome_key,
                           signal_identity_verified, stake, status, fill_price,
                           filled_at, rejection_reason
                      FROM shadow_orders"""
            )
            self.connection.execute("DROP TABLE shadow_orders")
            self.connection.execute(
                "ALTER TABLE shadow_orders_strict_migration RENAME TO shadow_orders"
            )

        self.connection.executescript(
            """
            DROP TRIGGER IF EXISTS shadow_orders_require_signal_insert;
            DROP TRIGGER IF EXISTS shadow_orders_require_signal_update;
            DROP TRIGGER IF EXISTS shadow_orders_signal_identity_immutable;
            CREATE TRIGGER IF NOT EXISTS shadow_orders_require_signal_insert
            BEFORE INSERT ON shadow_orders
            WHEN NEW.signal_transport_key IS NULL OR NEW.signal_transport_key=''
              OR NEW.signal_transport_at IS NULL OR NEW.signal_transport_at=''
              OR NEW.expires_at IS NULL OR NEW.expires_at=''
              OR NEW.signal_identity_verified!=1
              OR NEW.signal_odds_group_id IS NULL OR NEW.signal_odds_group_id=''
              OR NEW.signal_outcome_key IS NULL OR NEW.signal_outcome_key=''
            BEGIN
                SELECT RAISE(ABORT, 'shadow order signal identity is required');
            END;
            CREATE TRIGGER IF NOT EXISTS shadow_orders_require_signal_update
            BEFORE UPDATE ON shadow_orders
            WHEN NEW.signal_transport_key IS NULL OR NEW.signal_transport_key=''
              OR NEW.signal_transport_at IS NULL OR NEW.signal_transport_at=''
              OR NEW.expires_at IS NULL OR NEW.expires_at=''
            BEGIN
                SELECT RAISE(ABORT, 'shadow order signal identity is required');
            END;
            CREATE TRIGGER shadow_orders_signal_identity_immutable
            BEFORE UPDATE ON shadow_orders
            WHEN OLD.raybet_match_id IS NOT NEW.raybet_match_id
              OR OLD.odds_id IS NOT NEW.odds_id
              OR OLD.market_key IS NOT NEW.market_key
              OR OLD.signaled_at IS NOT NEW.signaled_at
              OR OLD.signal_price IS NOT NEW.signal_price
              OR OLD.signal_transport_key IS NOT NEW.signal_transport_key
              OR OLD.signal_transport_at IS NOT NEW.signal_transport_at
              OR OLD.expires_at IS NOT NEW.expires_at
              OR OLD.signal_odds_group_id IS NOT NEW.signal_odds_group_id
              OR OLD.signal_outcome_key IS NOT NEW.signal_outcome_key
              OR OLD.signal_identity_verified IS NOT NEW.signal_identity_verified
            BEGIN
                SELECT RAISE(ABORT, 'shadow order signal identity is immutable');
            END;
            """
        )

    @staticmethod
    def json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        try:
            cursor = self.connection.execute(sql, parameters)
        except Exception:
            if self._transaction_depth == 0:
                self.connection.rollback()
            raise
        if self._transaction_depth == 0:
            self.connection.commit()
        return cursor

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit a unit of work atomically while supporting nested callers."""
        if self._transaction_depth:
            self._savepoint_sequence += 1
            name = f"transaction_{self._savepoint_sequence}"
            with self.savepoint(name):
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
            return

        self.connection.execute("BEGIN IMMEDIATE")
        self._transaction_depth = 1
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            try:
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        finally:
            self._transaction_depth = 0

    @contextmanager
    def savepoint(self, name: str) -> Iterator[None]:
        """Create a named rollback boundary inside an active transaction."""
        if self._transaction_depth == 0:
            raise RuntimeError("savepoint requires an active transaction")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("invalid savepoint name")
        self.connection.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self.connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.connection.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self.connection.execute(f"RELEASE SAVEPOINT {name}")

    @staticmethod
    def _event_value(event: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
        if isinstance(event, Mapping):
            return event.get(name, default)
        return getattr(event, name, default)

    @staticmethod
    def _iso(value: datetime | str) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc)
            return value.isoformat()
        return str(value)

    @staticmethod
    def _scalar(value: Any) -> Any:
        return value.value if isinstance(value, Enum) else value

    def upsert_provider_match(self, match: ProviderMatch, updated_at: datetime) -> None:
        self.execute(
            """INSERT INTO provider_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, provider_match_id) DO UPDATE SET
              tournament=excluded.tournament, team_one=excluded.team_one,
              team_two=excluded.team_two, scheduled_at=excluded.scheduled_at,
              best_of=excluded.best_of, status=excluded.status,
              raw_json=excluded.raw_json, updated_at=excluded.updated_at""",
            (match.provider, match.provider_match_id, match.tournament, match.team_one,
             match.team_two, match.scheduled_at.isoformat() if match.scheduled_at else None,
             match.best_of, match.status, self.json(match.raw), updated_at.isoformat()),
        )

    def upsert_raybet_match(self, row: dict[str, Any], updated_at: datetime) -> None:
        teams = sorted(row.get("team") or [], key=lambda item: int(item.get("pos") or 0))
        team_one = str(teams[0].get("team_name") or "") if teams else ""
        team_two = str(teams[1].get("team_name") or "") if len(teams) > 1 else ""
        round_name = str(row.get("round") or "").lower()
        best_of = int(round_name[2:]) if round_name.startswith("bo") and round_name[2:].isdigit() else None
        self.execute(
            """INSERT INTO raybet_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(raybet_match_id) DO UPDATE SET
              tournament=excluded.tournament, team_one=excluded.team_one,
              team_two=excluded.team_two, scheduled_at=excluded.scheduled_at,
              best_of=excluded.best_of, status=excluded.status,
              live_url=excluded.live_url, raw_json=excluded.raw_json,
              updated_at=excluded.updated_at""",
            (str(row.get("id")), str(row.get("tournament_name") or ""), team_one, team_two,
             row.get("start_time"), best_of, str(row.get("status") or ""),
             row.get("live_url"), self.json(row), updated_at.isoformat()),
        )

    def insert_browser_raybet_match(
        self, row: dict[str, Any], updated_at: datetime
    ) -> bool:
        """Insert sanitized browser metadata without replacing direct-owned data."""
        teams = sorted(row.get("team") or [], key=lambda item: int(item.get("pos") or 0))
        team_one = str(teams[0].get("team_name") or "") if teams else ""
        team_two = str(teams[1].get("team_name") or "") if len(teams) > 1 else ""
        round_name = str(row.get("round") or "").lower()
        best_of = (
            int(round_name[2:])
            if round_name.startswith("bo") and round_name[2:].isdigit()
            else None
        )
        cursor = self.execute(
            """INSERT OR IGNORE INTO raybet_matches
            (raybet_match_id, tournament, team_one, team_two, scheduled_at, best_of,
             status, live_url, raw_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (str(row.get("id")), str(row.get("tournament_name") or ""),
             team_one, team_two, row.get("start_time"), best_of,
             str(row.get("status") or ""), self.json({}), updated_at.isoformat()),
        )
        return cursor.rowcount == 1

    def insert_browser_event(
        self,
        event: Mapping[str, Any] | Any,
        *,
        received_at: datetime,
        recognized: bool,
        processing_status: str = "pending",
        processing_reason: str | None = None,
    ) -> bool:
        captured_at = self._event_value(
            event, "captured_at_utc", self._event_value(event, "captured_at")
        )
        payload = self._event_value(event, "payload", {})
        cursor = self.execute(
            """INSERT OR IGNORE INTO browser_events
            (event_id, schema_version, capture_session_id, captured_at, received_at,
             transport, event_type, raybet_match_id, game_id, page_origin, page_path,
             source_path, payload_hash, payload_bytes, payload_json, capture_reason,
             extension_version, recognized, processing_status, processing_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(self._event_value(event, "event_id")),
                int(self._event_value(event, "schema_version")),
                str(self._event_value(event, "capture_session_id")),
                self._iso(captured_at),
                self._iso(received_at),
                str(self._scalar(self._event_value(event, "transport"))),
                str(self._scalar(self._event_value(event, "event_type"))),
                self._event_value(event, "raybet_match_id"),
                self._event_value(event, "game_id"),
                str(self._event_value(event, "page_origin")),
                str(self._event_value(event, "page_path")),
                str(self._event_value(event, "source_path")),
                str(self._event_value(event, "payload_hash")),
                int(self._event_value(event, "payload_bytes")),
                self.json(payload),
                self._event_value(event, "capture_reason"),
                str(self._event_value(event, "extension_version")),
                int(recognized),
                processing_status,
                processing_reason,
            ),
        )
        return cursor.rowcount == 1

    def update_browser_event_status(
        self, event_id: str, status: str, reason: str | None = None
    ) -> bool:
        cursor = self.execute(
            """UPDATE browser_events
               SET processing_status=?, processing_reason=? WHERE event_id=?""",
            (status, reason, event_id),
        )
        return cursor.rowcount == 1

    def observation_timing_status(
        self, raybet_match_id: str, observed_at: datetime
    ) -> str:
        newest = self.connection.execute(
            """SELECT observed_at FROM odds_transport_observations
               WHERE raybet_match_id=? AND timing_status!='late'
               ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
            (raybet_match_id,),
        ).fetchone()
        if newest and self._iso(observed_at) < str(newest["observed_at"]):
            return "late"
        return "on_time"

    def insert_transport_observation(
        self,
        *,
        observation_key: str,
        source: str,
        source_event_id: str | None,
        raybet_match_id: str,
        observed_at: datetime,
        normalized_state_hash: str,
        timing_status: str,
        processing_status: str,
        normalized_change_count: int,
    ) -> bool:
        cursor = self.execute(
            """INSERT OR IGNORE INTO odds_transport_observations
            (observation_key, source, source_event_id, raybet_match_id, observed_at,
             normalized_state_hash, timing_status, processing_status,
             normalized_change_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (observation_key, source, source_event_id, raybet_match_id,
             self._iso(observed_at), normalized_state_hash, timing_status,
             processing_status, normalized_change_count),
        )
        return cursor.rowcount == 1

    def store_odds_observation(
        self,
        *,
        source: str,
        observation_key: str,
        source_event_id: str | None,
        raybet_match_id: str,
        observed_at: datetime,
        normalized_state_hash: str,
        snapshots: Sequence[OddsSnapshot],
    ) -> tuple[str, int]:
        """Atomically retain one complete response and its semantic state changes."""
        seen_odds_ids: set[str] = set()
        for snapshot in snapshots:
            if snapshot.raybet_match_id != raybet_match_id:
                raise ValueError("response outcome match id mismatch")
            if snapshot.received_at != observed_at:
                raise ValueError("response outcome transport time mismatch")
            if snapshot.odds_id in seen_odds_ids:
                raise ValueError("duplicate odds id in one response")
            seen_odds_ids.add(snapshot.odds_id)

        with self.transaction():
            existing = self.connection.execute(
                """SELECT source, source_event_id, raybet_match_id, observed_at,
                          normalized_state_hash, timing_status,
                          normalized_change_count
                   FROM odds_transport_observations WHERE observation_key=?""",
                (observation_key,),
            ).fetchone()
            if existing:
                identity = (
                    str(existing["source"]),
                    existing["source_event_id"],
                    str(existing["raybet_match_id"]),
                    str(existing["observed_at"]),
                    str(existing["normalized_state_hash"]),
                )
                expected = (
                    source,
                    source_event_id,
                    raybet_match_id,
                    self._iso(observed_at),
                    normalized_state_hash,
                )
                if identity != expected:
                    raise ValueError("observation key already belongs to another response")
                persisted_outcomes = self.connection.execute(
                    """SELECT raybet_match_id, odds_id, odds_group_id, received_at,
                              price, status, market_type, period, side, line,
                              outcome_key, supported, last_update, raw_json
                         FROM odds_response_outcomes
                        WHERE observation_key=? ORDER BY odds_id""",
                    (observation_key,),
                ).fetchall()
                if not persisted_outcomes:
                    self._insert_response_outcomes(observation_key, snapshots)
                else:
                    actual_outcomes = [tuple(row) for row in persisted_outcomes]
                    expected_outcomes = sorted(
                        (self._response_outcome_values(snapshot) for snapshot in snapshots),
                        key=lambda values: str(values[1]),
                    )
                    if actual_outcomes != expected_outcomes:
                        raise ValueError(
                            "observation key response membership or payload differs"
                        )
                return str(existing["timing_status"]), 0

            timing_status = self.observation_timing_status(raybet_match_id, observed_at)
            processing_status = "audit_only" if timing_status == "late" else "processing"
            inserted = self.insert_transport_observation(
                observation_key=observation_key,
                source=source,
                source_event_id=source_event_id,
                raybet_match_id=raybet_match_id,
                observed_at=observed_at,
                normalized_state_hash=normalized_state_hash,
                timing_status=timing_status,
                processing_status=processing_status,
                normalized_change_count=0,
            )
            if not inserted:
                return timing_status, 0

            self._insert_response_outcomes(observation_key, snapshots)
            change_count = 0
            if timing_status != "late":
                change_count = sum(int(self.insert_odds(snapshot)) for snapshot in snapshots)
                processing_status = "processed"
            self.execute(
                """UPDATE odds_transport_observations
                   SET processing_status=?, normalized_change_count=?
                   WHERE observation_key=?""",
                (processing_status, change_count, observation_key),
            )
            return timing_status, change_count

    def _insert_response_outcomes(
        self, observation_key: str, snapshots: Sequence[OddsSnapshot]
    ) -> None:
        """Persist exact response membership, independently of semantic changes."""
        for snapshot in snapshots:
            self.execute(
                """INSERT OR IGNORE INTO odds_response_outcomes
                (observation_key, raybet_match_id, odds_id, odds_group_id,
                 received_at, price, status, market_type, period, side, line,
                 outcome_key, supported, last_update, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (observation_key, *self._response_outcome_values(snapshot)),
            )

    def _response_outcome_values(self, snapshot: OddsSnapshot) -> tuple[Any, ...]:
        market = snapshot.market
        return (
            snapshot.raybet_match_id,
            snapshot.odds_id,
            snapshot.odds_group_id,
            self._iso(snapshot.received_at),
            snapshot.price,
            None if snapshot.status is None else str(snapshot.status),
            market.market_type,
            market.period,
            market.side,
            market.line,
            market.outcome_key,
            int(market.supported),
            snapshot.last_update,
            json.dumps(
                snapshot.raw,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            ),
        )

    def upsert_match_link(
        self, raybet_match_id: str, provider: str, provider_match_id: str,
        confidence: float, status: str, reason: str, created_at: datetime,
    ) -> None:
        self.execute(
            """INSERT INTO match_links VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(raybet_match_id, provider) DO UPDATE SET
              provider_match_id=CASE WHEN match_links.status='accepted'
                THEN match_links.provider_match_id ELSE excluded.provider_match_id END,
              confidence=excluded.confidence, status=CASE WHEN match_links.status='accepted'
                THEN match_links.status ELSE excluded.status END, reason=excluded.reason""",
            (raybet_match_id, provider, provider_match_id, confidence, status, reason,
             created_at.isoformat()),
        )

    def insert_odds(self, snapshot: OddsSnapshot) -> bool:
        market = snapshot.market
        previous = self.connection.execute(
            """SELECT price, status, last_update FROM odds_snapshots
            WHERE raybet_match_id=? AND odds_id=? AND received_at<=?
            ORDER BY received_at DESC, id DESC LIMIT 1""",
            (snapshot.raybet_match_id, snapshot.odds_id,
             self._iso(snapshot.received_at)),
        ).fetchone()
        current = (snapshot.price, str(snapshot.status), snapshot.last_update)
        if previous and tuple(previous) == current:
            return False
        cursor = self.execute(
            """INSERT OR IGNORE INTO odds_snapshots
            (raybet_match_id, odds_id, odds_group_id, received_at, price, status,
             market_type, period, side, line, outcome_key, supported, last_update, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot.raybet_match_id, snapshot.odds_id, snapshot.odds_group_id,
              self._iso(snapshot.received_at), snapshot.price, str(snapshot.status),
             market.market_type, market.period, market.side, market.line,
             market.outcome_key, int(market.supported), snapshot.last_update,
             self.json(snapshot.raw)),
        )
        return cursor.rowcount == 1

    def next_fill_candidate(self, order: ShadowOrder) -> sqlite3.Row | None:
        """Return the target outcome only from the first eligible response."""
        return self.connection.execute(
            """WITH successor AS (
                   SELECT observation_key, raybet_match_id, observed_at
                     FROM odds_transport_observations
                    WHERE raybet_match_id=? AND observed_at>?
                      AND timing_status='on_time'
                      AND processing_status='processed'
                    ORDER BY observed_at, observation_key LIMIT 1
               )
               SELECT outcome.*, successor.observed_at AS transport_observed_at,
                      successor.observation_key AS transport_observation_key
                 FROM successor
                 JOIN odds_response_outcomes outcome
                   ON outcome.observation_key=successor.observation_key
                  AND outcome.raybet_match_id=successor.raybet_match_id
                WHERE outcome.odds_id=?""",
            (
                order.raybet_match_id,
                self._iso(order.signal_transport_at),
                order.odds_id,
            ),
        ).fetchone()

    def processed_transport_watermark(
        self, raybet_match_id: str, *, as_of: datetime
    ) -> datetime | None:
        """Return persisted event-time progress, never the worker wall clock."""
        row = self.connection.execute(
            """SELECT observed_at FROM odds_transport_observations
                 WHERE raybet_match_id=? AND observed_at<=?
                   AND timing_status='on_time'
                   AND processing_status='processed'
                 ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
            (raybet_match_id, self._iso(as_of)),
        ).fetchone()
        return (
            datetime.fromisoformat(str(row["observed_at"]))
            if row is not None
            else None
        )

    def _signal_identity_matches(self, order: ShadowOrder) -> bool:
        if not order.signal_identity_verified:
            return False
        row = self.connection.execute(
            """SELECT transport.raybet_match_id, transport.observed_at,
                      transport.timing_status, transport.processing_status,
                      outcome.odds_group_id, outcome.outcome_key,
                      outcome.price, outcome.status, outcome.supported,
                      outcome.market_type, outcome.period, outcome.side,
                      outcome.line
                 FROM odds_transport_observations AS transport
                 JOIN odds_response_outcomes AS outcome
                   ON outcome.observation_key=transport.observation_key
                WHERE transport.observation_key=?
                  AND outcome.raybet_match_id=? AND outcome.odds_id=?""",
            (
                order.signal_transport_key,
                order.raybet_match_id,
                order.odds_id,
            ),
        ).fetchone()
        if row is None:
            return False
        return (
            str(row["raybet_match_id"]) == order.raybet_match_id
            and str(row["observed_at"]) == self._iso(order.signal_transport_at)
            and str(row["timing_status"]) == "on_time"
            and str(row["processing_status"]) == "processed"
            and str(row["odds_group_id"] or "") == order.signal_odds_group_id
            and str(row["outcome_key"] or "") == order.signal_outcome_key
            and float(row["price"]) == order.signal_price
            and is_open(row["status"])
            and bool(row["supported"])
            and market_key(
                str(row["market_type"]),
                str(row["period"]),
                row["side"],
                row["line"],
            )
            == market_key(
                order.market.market_type,
                order.market.period,
                order.market.side,
                order.market.line,
            )
        )

    def process_pending_successor(
        self,
        order: ShadowOrder,
        *,
        watermark: datetime,
        max_slippage: float = 0.03,
    ) -> ShadowOrder | None:
        """Resolve a pending order from its exact first visible successor.

        A returned order was transitioned atomically with its map attempt. None
        means that the order remains pending or another worker already resolved it.
        """
        with self.transaction():
            current = self.connection.execute(
                """SELECT raybet_match_id, odds_id, signal_transport_key,
                          signal_transport_at, expires_at,
                          signal_odds_group_id, signal_outcome_key,
                          signal_identity_verified, status
                     FROM shadow_orders WHERE order_key=?""",
                (order.order_key,),
            ).fetchone()
            if current is None:
                raise ValueError("shadow order is not persisted")
            if str(current["status"]) != "pending":
                return None
            persisted_identity = (
                str(current["raybet_match_id"]),
                str(current["odds_id"]),
                str(current["signal_transport_key"]),
                str(current["signal_transport_at"]),
                str(current["expires_at"]),
                current["signal_odds_group_id"],
                current["signal_outcome_key"],
                bool(current["signal_identity_verified"]),
            )
            requested_identity = (
                order.raybet_match_id,
                order.odds_id,
                order.signal_transport_key,
                self._iso(order.signal_transport_at),
                self._iso(order.expires_at),
                order.signal_odds_group_id,
                order.signal_outcome_key,
                order.signal_identity_verified,
            )
            if persisted_identity != requested_identity:
                raise ValueError("shadow order does not match persisted signal identity")

            signal_is_valid = self._signal_identity_matches(order)
            successor = None
            if signal_is_valid:
                successor = self.connection.execute(
                    """SELECT observation_key, raybet_match_id, observed_at
                         FROM odds_transport_observations
                        WHERE raybet_match_id=? AND observed_at>?
                          AND observed_at<=?
                          AND timing_status='on_time'
                          AND processing_status='processed'
                        ORDER BY observed_at, observation_key LIMIT 1""",
                    (
                        order.raybet_match_id,
                        self._iso(order.signal_transport_at),
                        self._iso(watermark),
                    ),
                ).fetchone()

            resolved: ShadowOrder | None = None
            if not signal_is_valid:
                resolved = replace(
                    order,
                    status="rejected",
                    rejection_reason="signal_identity_unverified",
                )
            elif successor is not None:
                observed_at = datetime.fromisoformat(str(successor["observed_at"]))
                if observed_at > order.expires_at:
                    resolved = replace(
                        order,
                        status="rejected",
                        rejection_reason="fill_timeout",
                    )
                else:
                    outcome = self.connection.execute(
                        """SELECT * FROM odds_response_outcomes
                            WHERE observation_key=? AND raybet_match_id=?
                              AND odds_id=?""",
                        (
                            str(successor["observation_key"]),
                            order.raybet_match_id,
                            order.odds_id,
                        ),
                    ).fetchone()
                    if outcome is None:
                        resolved = replace(
                            order,
                            status="rejected",
                            rejection_reason="outcome_missing",
                        )
                    else:
                        resolved = attempt_fill(
                            order,
                            self._response_snapshot(outcome),
                            observed_at=observed_at,
                            max_slippage=max_slippage,
                            now=observed_at,
                        )

            if resolved is None or resolved.status == "pending":
                return None
            order_update = self.connection.execute(
                """UPDATE shadow_orders
                      SET status=?, fill_price=?, filled_at=?, rejection_reason=?
                    WHERE order_key=? AND status='pending'""",
                (
                    resolved.status,
                    resolved.fill_price,
                    self._iso(resolved.filled_at) if resolved.filled_at else None,
                    resolved.rejection_reason,
                    resolved.order_key,
                ),
            )
            if order_update.rowcount != 1:
                return None
            if not self.update_map_attempt(
                resolved.order_key, resolved.status, expected_status="pending"
            ):
                raise RuntimeError("pending order has no matching pending map attempt")
            if resolved.status == "filled":
                map_row = self.connection.execute(
                    """SELECT map_number FROM shadow_map_attempts
                        WHERE order_key=?""",
                    (resolved.order_key,),
                ).fetchone()
                if map_row is None or resolved.filled_at is None:
                    raise RuntimeError("filled order is missing map provenance")
                from .notifications import EVENT_FILLED, simulation_payload

                self.enqueue_notification(
                    order_key=resolved.order_key,
                    event_type=EVENT_FILLED,
                    payload=simulation_payload(
                        EVENT_FILLED,
                        {
                            "raybet_match_id": resolved.raybet_match_id,
                            "map_number": int(map_row["map_number"]),
                            "selected_side": resolved.market.side,
                            "signal_price": resolved.signal_price,
                            "fill_price": resolved.fill_price,
                            "model_probability": resolved.model_probability,
                            "market_probability": resolved.market_probability,
                            "edge": resolved.model_probability
                            - resolved.market_probability,
                            "signal_transport_at": resolved.signal_transport_at,
                            "filled_at": resolved.filled_at,
                            "order_key": resolved.order_key,
                        },
                    ),
                    stats_cutoff_at=resolved.filled_at,
                    created_at=resolved.filled_at,
                )
            return resolved

    @staticmethod
    def _response_snapshot(row: sqlite3.Row) -> OddsSnapshot:
        market = Market(
            str(row["market_type"]),
            str(row["period"]),
            row["side"],
            row["line"],
            str(row["outcome_key"]),
            bool(row["supported"]),
        )
        return OddsSnapshot(
            str(row["raybet_match_id"]),
            str(row["odds_id"]),
            row["odds_group_id"],
            datetime.fromisoformat(str(row["received_at"])),
            float(row["price"]),
            row["status"],
            market,
            row["last_update"],
            json.loads(str(row["raw_json"])),
        )

    def insert_frame(self, frame: LiveFrame) -> None:
        self.execute(
            """INSERT OR IGNORE INTO live_frames VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (frame.provider, frame.provider_match_id, frame.provider_game_id,
             frame.sequence or "", frame.source_at.isoformat() if frame.source_at else None,
             frame.received_at.isoformat(), frame.game_time, frame.team_one_kills,
             frame.team_two_kills, frame.team_one_gold, frame.team_two_gold, frame.state,
             self.json(frame.raw)),
        )

    def insert_event(self, event: LiveEvent) -> None:
        self.execute(
            """INSERT OR IGNORE INTO live_events VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.provider, event.provider_event_id, event.provider_match_id,
             event.provider_game_id, event.event_type,
             event.source_at.isoformat() if event.source_at else None,
             event.received_at.isoformat(), event.game_time, event.team, event.player,
             event.value, self.json(event.raw)),
        )

    def insert_order(self, order: ShadowOrder) -> bool:
        if not self._signal_identity_matches(order):
            return False
        cursor = self.execute(
            """INSERT OR IGNORE INTO shadow_orders
            (order_key, raybet_match_id, odds_id, market_key, signaled_at,
             model_probability, market_probability, signal_price,
             signal_transport_key, signal_transport_at, expires_at,
             signal_odds_group_id, signal_outcome_key,
             signal_identity_verified, stake, status, fill_price, filled_at,
             rejection_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order.order_key, order.raybet_match_id, order.odds_id,
             market_key(order.market.market_type, order.market.period,
                        order.market.side, order.market.line),
             order.signaled_at.isoformat(), order.model_probability,
             order.market_probability, order.signal_price,
             order.signal_transport_key, self._iso(order.signal_transport_at),
             self._iso(order.expires_at), order.signal_odds_group_id,
             order.signal_outcome_key, int(order.signal_identity_verified),
             order.stake, order.status,
             order.fill_price, self._iso(order.filled_at) if order.filled_at else None,
             order.rejection_reason),
        )
        return cursor.rowcount == 1

    def update_order(self, order: ShadowOrder) -> None:
        self.execute(
            """UPDATE shadow_orders SET status=?, fill_price=?, filled_at=?,
            rejection_reason=? WHERE order_key=?""",
            (order.status, order.fill_price,
             order.filled_at.isoformat() if order.filled_at else None,
             order.rejection_reason, order.order_key),
        )

    def record_collector(
        self, collector: str, *, success_at: datetime | None = None,
        error_at: datetime | None = None, error: str | None = None,
        cursor: str | None = None, gap: bool = False,
    ) -> None:
        self.execute(
            """INSERT INTO collector_runs
            (collector, last_success_at, last_error_at, last_error, cursor, gap_detected)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(collector) DO UPDATE SET
              last_success_at=COALESCE(excluded.last_success_at, collector_runs.last_success_at),
              last_error_at=COALESCE(excluded.last_error_at, collector_runs.last_error_at),
              last_error=excluded.last_error, cursor=COALESCE(excluded.cursor, collector_runs.cursor),
              gap_detected=excluded.gap_detected""",
            (collector, success_at.isoformat() if success_at else None,
             error_at.isoformat() if error_at else None, error, cursor, int(gap)),
        )

    def insert_vision_observation(self, observation: Any) -> bool:
        cursor = self.execute(
            """INSERT OR IGNORE INTO vision_observations
            (raybet_match_id, map_number, captured_at, game_clock_seconds,
             is_paused, radiant_hero_ids, dire_hero_ids, radiant_team_side,
             clock_confidence, draft_confidence, source_frame_ref, screen_state,
             confirmed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (observation.raybet_match_id, observation.map_number,
             observation.captured_at.isoformat(), observation.game_clock_seconds,
             None if observation.is_paused is None else int(observation.is_paused),
             self.json(list(observation.radiant_hero_ids)),
             self.json(list(observation.dire_hero_ids)),
             observation.radiant_team_side,
             observation.clock_confidence, observation.draft_confidence,
             observation.source_frame_ref, observation.screen_state,
             int(observation.is_confirmed)),
        )
        return cursor.rowcount == 1

    def insert_alignment(self, alignment: Any) -> bool:
        cursor = self.execute(
            """INSERT OR REPLACE INTO odds_alignments VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (alignment.odds_snapshot_id, alignment.raybet_match_id,
             alignment.map_number, alignment.game_clock_seconds,
             alignment.observation_captured_at.isoformat()
             if alignment.observation_captured_at else None,
             alignment.method, alignment.lag_seconds, int(alignment.usable),
             alignment.reason),
        )
        return cursor.rowcount == 1

    def insert_decision(self, decision: Any) -> bool:
        cursor = self.execute(
            """INSERT OR IGNORE INTO strategy_decisions VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (decision.decision_key, decision.raybet_match_id, decision.map_number,
             decision.decided_at.isoformat(), decision.underdog_side,
             decision.market_probability, decision.model_probability,
             decision.edge, decision.data_quality, int(decision.eligible),
             decision.reason, self.json(decision.contributions),
             decision.input_ref, decision.strategy_version),
        )
        return cursor.rowcount == 1

    def reserve_map_attempt(
        self, raybet_match_id: str, map_number: int, order_key: str,
        status: str, created_at: datetime,
    ) -> bool:
        cursor = self.execute(
            """INSERT OR IGNORE INTO shadow_map_attempts VALUES (?, ?, ?, ?, ?)""",
            (raybet_match_id, map_number, order_key, status, created_at.isoformat()),
        )
        return cursor.rowcount == 1

    def update_map_attempt(
        self,
        order_key: str,
        status: str,
        *,
        expected_status: str | None = None,
    ) -> bool:
        if expected_status is None:
            cursor = self.execute(
                "UPDATE shadow_map_attempts SET status=? WHERE order_key=?",
                (status, order_key),
            )
        else:
            cursor = self.execute(
                """UPDATE shadow_map_attempts SET status=?
                    WHERE order_key=? AND status=?""",
                (status, order_key, expected_status),
            )
        return cursor.rowcount == 1

    def has_map_attempt(self, raybet_match_id: str, map_number: int) -> bool:
        row = self.connection.execute(
            """SELECT 1 FROM shadow_map_attempts
               WHERE raybet_match_id=? AND map_number=?""",
            (raybet_match_id, map_number),
        ).fetchone()
        return row is not None

    def insert_map_order(self, order: ShadowOrder, map_number: int) -> bool:
        """Atomically reserve a map and persist its only shadow order."""
        if not self._signal_identity_matches(order):
            return False
        with self.transaction():
            reserved = self.connection.execute(
                """INSERT OR IGNORE INTO shadow_map_attempts
                   VALUES (?, ?, ?, ?, ?)""",
                (order.raybet_match_id, map_number, order.order_key,
                 order.status, order.signaled_at.isoformat()),
            )
            if reserved.rowcount != 1:
                return False
            self.connection.execute(
                """INSERT INTO shadow_orders
                (order_key, raybet_match_id, odds_id, market_key, signaled_at,
                 model_probability, market_probability, signal_price,
                 signal_transport_key, signal_transport_at, expires_at,
                 signal_odds_group_id, signal_outcome_key,
                 signal_identity_verified, stake, status, fill_price, filled_at,
                 rejection_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order.order_key, order.raybet_match_id, order.odds_id,
                 market_key(order.market.market_type, order.market.period,
                            order.market.side, order.market.line),
                 order.signaled_at.isoformat(), order.model_probability,
                 order.market_probability, order.signal_price,
                 order.signal_transport_key, self._iso(order.signal_transport_at),
                 self._iso(order.expires_at), order.signal_odds_group_id,
                 order.signal_outcome_key, int(order.signal_identity_verified),
                 order.stake, order.status,
                 order.fill_price,
                 self._iso(order.filled_at) if order.filled_at else None,
                 order.rejection_reason),
            )
            return True

    def insert_map_result(self, result: Any) -> bool:
        cursor = self.execute(
            """INSERT OR IGNORE INTO map_results VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result.raybet_match_id, result.map_number, result.dota_match_id,
             result.winner_side, result.team_one_kills, result.team_two_kills,
             result.duration_seconds, result.evidence_ref,
             result.settled_at.isoformat()),
        )
        return cursor.rowcount == 1

    def enqueue_notification(
        self,
        *,
        order_key: str,
        event_type: str,
        payload: Mapping[str, Any],
        stats_cutoff_at: datetime,
        created_at: datetime,
    ) -> bool:
        from .notifications import enqueue

        return enqueue(
            self.connection,
            order_key=order_key,
            event_type=event_type,
            payload=payload,
            stats_cutoff_at=stats_cutoff_at,
            created_at=created_at,
        )

    def insert_settlement(
        self, order_key: str, result: str, return_units: float,
        settled_at: datetime, evidence_ref: str, review_required: bool = False,
    ) -> bool:
        with self.transaction():
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO settlements VALUES (?, ?, ?, ?, ?, ?)""",
                (order_key, result, return_units, settled_at.isoformat(), evidence_ref,
                 int(review_required)),
            )
            if cursor.rowcount != 1:
                return False
            if not review_required:
                order = self.connection.execute(
                    """SELECT raybet_match_id, market_key, fill_price
                         FROM shadow_orders WHERE order_key=?""",
                    (order_key,),
                ).fetchone()
                if order is not None:
                    from .notifications import EVENT_SETTLED, simulation_payload

                    self.enqueue_notification(
                        order_key=order_key,
                        event_type=EVENT_SETTLED,
                        payload=simulation_payload(
                            EVENT_SETTLED,
                            {
                                "raybet_match_id": str(order["raybet_match_id"]),
                                "result": result,
                                "return_units": return_units,
                                "fill_price": order["fill_price"],
                                "evidence_ref": evidence_ref,
                                "settled_at": settled_at,
                                "order_key": order_key,
                            },
                        ),
                        stats_cutoff_at=settled_at,
                        created_at=settled_at,
                    )
            return True
