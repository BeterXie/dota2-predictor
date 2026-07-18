"""Deterministic replay of immutable browser-event envelopes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .browser_contract import BrowserEvent, EventType, Transport
from .browser_ingest import ingest_browser_event
from .storage import LiveBettingStore, read_browser_event_payload


REPLAY_TABLES = (
    "browser_events",
    "odds_raw_artifacts",
    "odds_transport_observations",
    "odds_response_outcomes",
    "odds_response_states",
    "odds_response_state_outcomes",
    "odds_snapshots",
    "strategy_decisions",
    "shadow_orders",
    "settlements",
)


class BrowserReplayError(RuntimeError):
    """Raised when an immutable source event cannot be replayed safely."""


def _event_from_row(
    row: sqlite3.Row, payload: dict[str, Any]
) -> tuple[BrowserEvent, datetime]:
    try:
        event = BrowserEvent.model_validate(
            {
                "schema_version": row["schema_version"],
                "event_id": row["event_id"],
                "capture_session_id": row["capture_session_id"],
                "captured_at_utc": row["captured_at"],
                "page_origin": row["page_origin"],
                "page_path": row["page_path"],
                "source_path": row["source_path"],
                "transport": Transport(str(row["transport"])),
                "event_type": EventType(str(row["event_type"])),
                "raybet_match_id": row["raybet_match_id"],
                "game_id": row["game_id"],
                "payload": payload,
                "payload_hash": row["payload_hash"],
                "payload_bytes": row["payload_bytes"],
                "capture_reason": row["capture_reason"],
                "extension_version": row["extension_version"],
            }
        )
        received_at = datetime.fromisoformat(
            str(row["received_at"]).replace("Z", "+00:00")
        )
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ValueError("received_at must include a timezone")
        return event, received_at.astimezone(timezone.utc)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise BrowserReplayError(
            f"immutable browser event {row['event_id']} is invalid"
        ) from error


def _payload_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    archive_root: Path,
) -> dict[str, Any]:
    try:
        return read_browser_event_payload(
            connection,
            archive_root,
            str(row["event_id"]),
        )
    except RuntimeError as error:
        raise BrowserReplayError(
            f"immutable browser event {row['event_id']} payload is invalid"
        ) from error


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    return value


def _table_digest(connection: sqlite3.Connection, table: str) -> tuple[int, str]:
    columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
    rows = connection.execute(f"SELECT * FROM {table}").fetchall()
    values = []
    for row in rows:
        values.append([
            (
                Path(str(row[column])).name
                if table == "odds_raw_artifacts" and column == "storage_path"
                else _json_value(row[column])
            )
            for column in columns
        ])
    values.sort(key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    encoded = json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return len(rows), hashlib.sha256(encoded).hexdigest()


def _summary(connection: sqlite3.Connection, outcomes: Counter[str], event_count: int) -> dict[str, Any]:
    tables: dict[str, dict[str, Any]] = {}
    digest_input: list[tuple[str, int, str]] = []
    for table in REPLAY_TABLES:
        count, digest = _table_digest(connection, table)
        tables[table] = {"rows": count, "sha256": digest}
        digest_input.append((table, count, digest))
    encoded = json.dumps(digest_input, separators=(",", ":")).encode("utf-8")
    return {
        "events": event_count,
        "outcomes": dict(sorted(outcomes.items())),
        "tables": tables,
        "durable_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def replay_browser_events(
    source_database: str | Path,
    target_database: str | Path,
    *,
    restart_after: int | None = None,
    overwrite: bool = False,
    mode: str = "arrival",
    source_archive_root: str | Path | None = None,
) -> dict[str, Any]:
    """Replay source browser events into a fresh database.

    ``restart_after`` closes and reopens the target store after that many
    events, allowing restart equivalence to be checked without changing event
    time or delivery semantics. ``arrival`` preserves the recorded companion
    arrival time and orders by it; ``capture`` explicitly reconstructs causal
    capture order and uses capture time as the replay arrival time. The latter
    is an offline reconstruction, not a claim about original delivery.

    This harness intentionally replays browser ingestion only. Strategy,
    vision, simulated fill, and settlement require their separate immutable
    inputs and are reported as downstream-unrun rather than implied by the
    table digest. The source database is never modified.
    """
    source_path = Path(source_database).resolve()
    target_path = Path(target_database).resolve()
    archive_root = (
        Path(source_archive_root).resolve()
        if source_archive_root is not None
        else source_path.parent / "live_betting" / "raw-v2"
    )
    if source_path == target_path:
        raise ValueError("source and target databases must be different")
    if restart_after is not None and restart_after <= 0:
        raise ValueError("restart_after must be positive")
    if mode not in {"arrival", "capture"}:
        raise ValueError("mode must be 'arrival' or 'capture'")
    if target_path.exists():
        if not overwrite:
            raise FileExistsError(target_path)
        target_path.unlink()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(source_path)) as source:
        source.row_factory = sqlite3.Row
        rows = [
            (row, _payload_from_row(source, row, archive_root))
            for row in source.execute("SELECT * FROM browser_events").fetchall()
        ]

    def row_time(row: sqlite3.Row, column: str) -> datetime:
        value = datetime.fromisoformat(str(row[column]).replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise BrowserReplayError(f"event {row['event_id']} has a naive {column}")
        return value.astimezone(timezone.utc)

    sort_columns = ("captured_at", "event_id") if mode == "capture" else (
        "received_at", "captured_at", "event_id"
    )
    rows.sort(key=lambda item: tuple(
        row_time(item[0], column) if column != "event_id" else str(item[0][column])
        for column in sort_columns
    ))
    source_statuses = Counter(str(row["processing_status"]) for row, _ in rows)
    capture_order_ties = sum(
        1
        for previous, current in zip(rows, rows[1:])
        if str(previous[0]["captured_at"]) == str(current[0]["captured_at"])
    )

    outcomes: Counter[str] = Counter()
    store: LiveBettingStore | None = None
    try:
        for index, (row, payload) in enumerate(rows, start=1):
            if store is None:
                store = LiveBettingStore(target_path)
                store.init_schema()
            event, recorded_received_at = _event_from_row(row, payload)
            replay_received_at = (
                event.captured_at_utc if mode == "capture" else recorded_received_at
            )
            result = ingest_browser_event(
                store, event, received_at=replay_received_at
            )
            outcomes[result.outcome] += 1
            if restart_after is not None and index < len(rows) and index % restart_after == 0:
                store.close()
                store = None
        if store is None:
            store = LiveBettingStore(target_path)
            store.init_schema()
        summary = _summary(store.connection, outcomes, len(rows))
        summary.update(
            {
                "mode": mode,
                "source_processing_statuses": dict(sorted(source_statuses.items())),
                "capture_order_ties": capture_order_ties,
                "downstream": "ingest_only",
                "downstream_strategy_executed": False,
                "downstream_settlement_executed": False,
            }
        )
        return summary
    finally:
        if store is not None:
            store.close()
