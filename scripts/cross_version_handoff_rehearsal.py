"""Run an isolated current-write -> recovery worker -> current handoff rehearsal.

The command never points either writer at the production database.  The supplied
production database is used only as a stable identity sentinel by the existing
production-path guard.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence


BASELINE_COMMIT = "8f6d4cd8f31be5eb8af6894c90f39e1589c5d465"
RESULT_SCHEMA = "dota2-cross-version-handoff-rehearsal-v2"


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, Path)):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=_json_default,
        )
        + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _attempted_production_connections(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        bool(json.loads(line).get("production_identity_match"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        tuple(command),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        check=False,
        text=True,
        capture_output=True,
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    return {
        "command": list(command),
        "cwd": str(cwd.resolve()),
        "exit_status": completed.returncode,
        "stdout": {
            "path": str(stdout_path.resolve()),
            "sha256": _sha256(stdout_path),
            "bytes": stdout_path.stat().st_size,
        },
        "stderr": {
            "path": str(stderr_path.resolve()),
            "sha256": _sha256(stderr_path),
            "bytes": stderr_path.stat().st_size,
        },
    }


def _last_json_line(path: Path) -> dict[str, Any]:
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"last JSON line is not an object: {path}")
        return value
    raise ValueError(f"command produced no JSON object: {path}")


def _raw_odds_payload(snapshot: Any) -> dict[str, Any]:
    market = snapshot.market
    outcome: dict[str, Any] = {
        "id": snapshot.odds_id,
        "odds_group_id": snapshot.odds_group_id or "",
        "match_stage": str(market.period).replace("map_", "r"),
        "odds": str(snapshot.price),
        "status": snapshot.status,
        "group_short_name": "Winner",
        "tag": "win",
    }
    if market.side in {"team_one", "team_two"}:
        outcome["team_id"] = 101 if market.side == "team_one" else 202
    return {
        "result": {
            "id": snapshot.raybet_match_id,
            "game_id": 151,
            "team": [
                {"team_id": 101, "team_name": "Alpha", "pos": 1},
                {"team_id": 202, "team_name": "Beta", "pos": 2},
            ],
            "odds": [outcome],
        }
    }


def add_direct_successor(args: argparse.Namespace) -> int:
    from live_betting.markets import normalized_state_hash
    from live_betting.models import Market, OddsSnapshot
    from live_betting.storage import LiveBettingStore
    from scripts.p0_evidence import production_sqlite_guard

    with production_sqlite_guard(args.production_database, args.connection_audit):
        with LiveBettingStore(args.database) as store:
            row = store.connection.execute(
                "SELECT * FROM shadow_orders WHERE status='pending' "
                "ORDER BY order_key LIMIT 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("no pending order available")
            market_type, period, side, line = str(row["market_key"]).split("|", 3)
            received_at = datetime.fromisoformat(
                str(row["signal_transport_at"])
            ) + timedelta(seconds=args.seconds_after_signal)
            snapshot = OddsSnapshot(
                str(row["raybet_match_id"]),
                str(row["odds_id"]),
                str(row["signal_odds_group_id"]),
                received_at,
                float(args.price),
                1,
                Market(
                    market_type,
                    period,
                    side or None,
                    float(line) if line else None,
                    str(row["signal_outcome_key"]),
                    True,
                ),
            )
            observation_key = "handoff-direct-successor"
            stored = store.store_odds_observation(
                source="direct",
                observation_key=observation_key,
                source_event_id=None,
                raybet_match_id=snapshot.raybet_match_id,
                observed_at=received_at,
                normalized_state_hash=normalized_state_hash([snapshot]),
                snapshots=[snapshot],
                raw_payload=_raw_odds_payload(snapshot),
            )
            store.connection.commit()
            payload: dict[str, Any] = {
                "database": str(args.database.resolve()),
                "order_key": str(row["order_key"]),
                "observation_key": observation_key,
                "received_at": received_at.astimezone(timezone.utc).isoformat(),
                "price": snapshot.price,
                "stored": list(stored),
            }

    attempts = _attempted_production_connections(args.connection_audit)
    payload["attempted_production_connections"] = attempts
    payload["guard_result"] = "passed" if attempts == 0 else "failed"
    print(json.dumps(payload, sort_keys=True))
    return 0 if attempts == 0 else 2


def settle_current(args: argparse.Namespace) -> int:
    from event_intelligence.ingest_adapters import SQLiteIngestAdapter
    from event_intelligence.raw_archive import RawArchive, canonical_json_bytes
    from event_intelligence.registry import EventRegistry
    from event_intelligence.storage import IntelligenceStorage
    from live_betting.raybet import parse_raybet_map_final
    from live_betting.report import build_report
    from live_betting.settlement import settle_authoritative_order
    from live_betting.storage import LiveBettingStore
    from scripts.p0_evidence import production_sqlite_guard
    from web.monitoring import build_monitor_snapshot

    database = args.database.resolve()
    with production_sqlite_guard(args.production_database, args.connection_audit):
        with LiveBettingStore(database) as store:
            order = store.connection.execute(
                "SELECT * FROM shadow_orders ORDER BY order_key LIMIT 1"
            ).fetchone()
            if order is None or str(order["status"]) != "filled":
                raise RuntimeError("order must be filled before current settlement")
            order_key = str(order["order_key"])
            raybet_match_id = str(order["raybet_match_id"])
            strict_mapping_id = int(order["strict_mapping_id"])
            map_number = int(
                store.connection.execute(
                    "SELECT map_number FROM shadow_map_attempts WHERE order_key=?",
                    (order_key,),
                ).fetchone()[0]
            )
            settled_at = datetime.fromisoformat(str(order["filled_at"])) + timedelta(
                minutes=45
            )
            dota_match_id = 99001
            reconciliation_ref = (
                f"settlement-reconciliation:{raybet_match_id}:map:{map_number}"
            )
            raybet_payload = {
                "id": raybet_match_id,
                "game_id": 151,
                "status": 2,
                "team": [
                    {
                        "pos": 1,
                        "team_id": 101,
                        "team_name": "Alpha",
                        "score": {f"r{map_number}": 1},
                    },
                    {
                        "pos": 2,
                        "team_id": 202,
                        "team_name": "Beta",
                        "score": {f"r{map_number}": 0},
                    },
                ],
                "odds": [
                    {
                        "odds_id": "final-one",
                        "odds_group_id": "final-group",
                        "match_stage": f"r{map_number}",
                        "group_short_name": "Winner",
                        "tag": "win",
                        "team_id": 101,
                        "status": 5,
                        "win": 1,
                    },
                    {
                        "odds_id": "final-two",
                        "odds_group_id": "final-group",
                        "match_stage": f"r{map_number}",
                        "group_short_name": "Winner",
                        "tag": "win",
                        "team_id": 202,
                        "status": 5,
                        "win": 0,
                    },
                ],
            }
            raybet_response = {"result": raybet_payload}
            raybet_artifact = store.archive_response_payload(
                raybet_response,
                observed_at=settled_at,
                match_id=raybet_match_id,
                response_kind="final_odds",
            )
            raybet_audit_key = store.record_direct_response_audit(
                raybet_artifact,
                response_kind="final_odds",
                claimed_raybet_match_id=raybet_match_id,
                observed_raybet_match_id=raybet_match_id,
                disposition="audit_only",
                reason="final_result_evidence",
            )
            raybet_final = parse_raybet_map_final(
                raybet_payload,
                map_number,
                observed_at=settled_at,
                expected_match_id=raybet_match_id,
                expected_team_ids=(101, 202),
            )

            intelligence = IntelligenceStorage(store.path, connection=store.connection)
            intelligence.init_schema()
            ingest = SQLiteIngestAdapter(intelligence, EventRegistry(intelligence))
            opendota_archive = RawArchive(
                database.parent / "opendota-handoff-raw",
                observation_sink=ingest.record_raw_artifact,
            )
            opendota_payload = {
                "match_id": dota_match_id,
                "radiant_team_id": 101,
                "dire_team_id": 202,
                "radiant_win": True,
                "radiant_score": 30,
                "dire_score": 20,
                "duration": 2400,
            }
            opendota_receipt = opendota_archive.archive_json(
                source="opendota",
                endpoint=f"/api/matches/{dota_match_id}",
                request_identity=f"/api/matches/{dota_match_id}",
                payload_bytes=canonical_json_bytes(opendota_payload),
                observed_at=settled_at,
                match_id=dota_match_id,
                status_code=200,
                first_usable_at=settled_at,
            )
            identity = {
                "raybet_match_id": raybet_match_id,
                "map_number": map_number,
                "strict_mapping_id": strict_mapping_id,
                "dota_match_id": dota_match_id,
                "winner_side": "team_one",
            }
            reconciliation = store.record_settlement_reconciliation(
                raybet_match_id=raybet_match_id,
                map_number=map_number,
                strict_mapping_id=strict_mapping_id,
                dota_match_id=dota_match_id,
                raybet_status="confirmed",
                raybet_winner_side="team_one",
                opendota_winner_side="team_one",
                raybet_evidence_ref=raybet_final.evidence_ref,
                opendota_evidence_ref=(
                    f"opendota:{dota_match_id}:sha256:"
                    f"{opendota_receipt.content_sha256}"
                ),
                raybet_facts={**identity, **raybet_final.facts()},
                opendota_facts={
                    **identity,
                    "team_one_kills": 30,
                    "team_two_kills": 20,
                    "duration_seconds": 2400,
                },
                status="confirmed",
                reason="sources_consistent",
                raybet_observed_at=settled_at,
                opendota_observed_at=settled_at,
                opendota_first_usable_at=settled_at,
                raybet_audit_key=raybet_audit_key,
                raybet_transport_key=None,
                raybet_response_state_hash=None,
                raybet_response_artifact_hash=raybet_artifact.content_sha256,
                opendota_artifact_id=f"opendota:{opendota_receipt.content_sha256}",
                opendota_observation_id=opendota_receipt.observation_id,
                opendota_content_hash=opendota_receipt.content_sha256,
            )
            if reconciliation["status"] != "confirmed":
                raise RuntimeError(f"authoritative reconciliation failed: {reconciliation}")
            if not store.insert_map_result(
                SimpleNamespace(
                    raybet_match_id=raybet_match_id,
                    map_number=map_number,
                    dota_match_id=dota_match_id,
                    winner_side="team_one",
                    team_one_kills=30,
                    team_two_kills=20,
                    duration_seconds=2400,
                    evidence_ref=reconciliation_ref,
                    settled_at=settled_at,
                ),
                strict_mapping_id=strict_mapping_id,
            ):
                raise RuntimeError("map result was not inserted")
            first = settle_authoritative_order(store, order_key)
            second = settle_authoritative_order(store, order_key)
            report = build_report(store.connection)
            monitor = build_monitor_snapshot(
                store.connection,
                now=settled_at + timedelta(minutes=1),
            )
            store.connection.commit()
            settlement = dict(
                store.connection.execute(
                    "SELECT * FROM settlements WHERE order_key=?", (order_key,)
                ).fetchone()
            )
            outbox = [
                dict(row)
                for row in store.connection.execute(
                    "SELECT event_type, status, message_id "
                    "FROM notification_outbox WHERE order_key=? ORDER BY outbox_id",
                    (order_key,),
                ).fetchall()
            ]
            authority_count = int(
                store.connection.execute(
                    "SELECT COUNT(*) FROM settlement_authority WHERE order_key=?",
                    (order_key,),
                ).fetchone()[0]
            )
            payload: dict[str, Any] = {
                "database": str(database),
                "order_key": order_key,
                "first_settlement_inserted": first,
                "second_settlement_inserted": second,
                "settlement": settlement,
                "settlement_authority_count": authority_count,
                "outbox": outbox,
                "report_status": report.get("status"),
                "report_settled_orders": report.get("settled_orders"),
                "monitor_summary": monitor.get("summary"),
            }

    attempts = _attempted_production_connections(args.connection_audit)
    payload["attempted_production_connections"] = attempts
    payload["guard_result"] = "passed" if attempts == 0 else "failed"
    print(json.dumps(payload, sort_keys=True, default=_json_default))
    return 0 if attempts == 0 and first and not second else 2


def verify_current(args: argparse.Namespace) -> int:
    from live_betting.report import build_report
    from live_betting.settlement import (
        persisted_settlement_authority_reason,
        settle_authoritative_order,
    )
    from live_betting.storage import LiveBettingStore
    from scripts.p0_evidence import production_sqlite_guard
    from web.monitoring import build_monitor_snapshot

    with production_sqlite_guard(args.production_database, args.connection_audit):
        with LiveBettingStore(args.database) as store:
            order = store.connection.execute(
                "SELECT order_key, filled_at, status FROM shadow_orders "
                "ORDER BY order_key LIMIT 1"
            ).fetchone()
            if order is None or str(order["status"]) != "filled":
                raise RuntimeError("filled order missing on current restart")
            order_key = str(order["order_key"])
            before = {
                "settlements": int(
                    store.connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
                ),
                "authority": int(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM settlement_authority"
                    ).fetchone()[0]
                ),
                "outbox": int(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM notification_outbox"
                    ).fetchone()[0]
                ),
                "map_results": int(
                    store.connection.execute("SELECT COUNT(*) FROM map_results").fetchone()[0]
                ),
            }
            inserted = settle_authoritative_order(store, order_key)
            reason = persisted_settlement_authority_reason(
                store.connection, order_key
            )
            report = build_report(store.connection)
            now = datetime.fromisoformat(str(order["filled_at"])) + timedelta(hours=1)
            monitor = build_monitor_snapshot(store.connection, now=now)
            after = {
                "settlements": int(
                    store.connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
                ),
                "authority": int(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM settlement_authority"
                    ).fetchone()[0]
                ),
                "outbox": int(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM notification_outbox"
                    ).fetchone()[0]
                ),
                "map_results": int(
                    store.connection.execute("SELECT COUNT(*) FROM map_results").fetchone()[0]
                ),
            }
            payload: dict[str, Any] = {
                "order_key": order_key,
                "second_process_inserted": inserted,
                "persisted_authority_reason": reason,
                "before": before,
                "after": after,
                "counts_stable": before == after,
                "report_settled_orders": report.get("settled_orders"),
                "monitor_summary": monitor.get("summary"),
            }

    attempts = _attempted_production_connections(args.connection_audit)
    payload["attempted_production_connections"] = attempts
    payload["guard_result"] = "passed" if attempts == 0 else "failed"
    print(json.dumps(payload, sort_keys=True, default=_json_default))
    return 0 if attempts == 0 and payload["counts_stable"] and not inserted and reason is None else 2



def notification_gate(args: argparse.Namespace) -> int:
    """Exercise claim, pre-send safety, and idempotent completion without SMTP."""

    from live_betting.notifications import claim, ensure_sendable, mark_sent
    from live_betting.storage import LiveBettingStore
    from scripts.p0_evidence import production_sqlite_guard

    source_database = args.source_database.resolve(strict=True)
    database_copy = args.database_copy.resolve()
    if database_copy.exists():
        raise ValueError(f"notification gate copy already exists: {database_copy}")
    database_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_database, database_copy)
    now = datetime(2026, 7, 14, 3, 0, tzinfo=timezone.utc)
    observed: list[dict[str, Any]] = []
    with production_sqlite_guard(args.production_database, args.connection_audit):
        with LiveBettingStore(database_copy) as store:
            while True:
                record = claim(store.connection, now=now, lease_seconds=300)
                if record is None:
                    break
                token = record.lease_token
                if token is None:
                    raise RuntimeError("claimed outbox row has no lease token")
                sendable = ensure_sendable(
                    store.connection,
                    outbox_id=record.outbox_id,
                    lease_token=token,
                    now=now + timedelta(seconds=1),
                )
                marked = False
                if sendable:
                    marked = mark_sent(
                        store.connection,
                        outbox_id=record.outbox_id,
                        lease_token=token,
                        sent_at=now + timedelta(seconds=2),
                    )
                observed.append(
                    {
                        "outbox_id": record.outbox_id,
                        "event_type": record.event_type,
                        "sendable": sendable,
                        "marked_sent": marked,
                    }
                )
            statuses = [
                tuple(row)
                for row in store.connection.execute(
                    "SELECT event_type, status FROM notification_outbox "
                    "ORDER BY outbox_id"
                )
            ]

    attempts = _attempted_production_connections(args.connection_audit)
    payload: dict[str, Any] = {
        "database_copy": str(database_copy),
        "observed": observed,
        "statuses": statuses,
        "attempted_production_connections": attempts,
        "guard_result": "passed" if attempts == 0 else "failed",
    }
    passed = bool(
        attempts == 0
        and len(observed) == 3
        and all(item["sendable"] and item["marked_sent"] for item in observed)
        and all(status == "sent" for _event, status in statuses)
    )
    payload["status"] = "passed" if passed else "failed"
    print(json.dumps(payload, sort_keys=True, default=_json_default))
    return 0 if passed else 2


def _copy_tree_contents(source: Path, destination: Path, prefix: str) -> None:
    if source.exists():
        shutil.copytree(source, destination / prefix)


def revocation_projection(args: argparse.Namespace) -> int:
    """Append a settlement revocation and verify report/monitoring isolation."""

    from live_betting.milestone_revocation import (
        MilestoneRevocationConfig,
        append_milestone_revocation,
        create_pair_baseline_manifest,
        initialize_milestone_revocation_ledger,
    )
    from live_betting.report import build_report
    from scripts.p0_evidence import production_sqlite_guard
    from tests.milestone_revocation_fixture import milestone_revocation_record
    from web.monitoring import build_monitor_snapshot

    database = args.database.resolve(strict=True)
    work_root = args.work_root.resolve()
    if work_root.exists() and any(work_root.iterdir()):
        raise ValueError(f"revocation work root must be empty: {work_root}")
    work_root.mkdir(parents=True, exist_ok=True)
    raw_root = work_root / "pair-raw"
    raw_root.mkdir()
    _copy_tree_contents(database.parent / "live_betting" / "raw-v2", raw_root, "raybet")
    _copy_tree_contents(database.parent / "opendota-handoff-raw", raw_root, "opendota")
    ledger = work_root / "revocations"

    with production_sqlite_guard(args.production_database, args.connection_audit):
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """SELECT orders.order_key, lineage.decision_key
                     FROM shadow_orders AS orders
                     JOIN shadow_order_decision_lineage AS lineage
                       ON lineage.order_key=orders.order_key
                    WHERE orders.status='filled' LIMIT 1"""
            ).fetchone()
            if row is None:
                raise RuntimeError("recovered order lineage missing")
            order_key = str(row["order_key"])
            decision_key = str(row["decision_key"])
            baseline = build_report(connection)
            baseline_monitor = build_monitor_snapshot(connection)
        finally:
            connection.close()

        p0_identity = "9" * 64
        pair_manifest = create_pair_baseline_manifest(
            database,
            raw_root,
            p0_baseline_evidence_identity=p0_identity,
        )
        pair_hash = hashlib.sha256(pair_manifest).hexdigest()
        anchor = initialize_milestone_revocation_ledger(
            ledger,
            database_path=database,
            raw_root=raw_root,
            pair_manifest=pair_manifest,
            expected_pair_manifest_hash=pair_hash,
            p0_baseline_evidence_identity=p0_identity,
        )
        record = milestone_revocation_record(
            "settlement",
            sample_key="handoff-sample-1",
        )
        record["affected"] = {
            "decision_keys": [decision_key],
            "order_keys": [order_key],
            "settlement_keys": [order_key],
            "sample_keys": ["handoff-sample-1"],
            "sample_lineage": [
                {
                    "sample_key": "handoff-sample-1",
                    "settlement_key": order_key,
                    "order_key": order_key,
                    "decision_key": decision_key,
                }
            ],
        }
        anchor = append_milestone_revocation(
            ledger,
            record,
            database_path=database,
            raw_root=raw_root,
            expected_anchor=anchor,
            pair_manifest=pair_manifest,
            expected_pair_manifest_hash=pair_hash,
        )
        config = MilestoneRevocationConfig(
            root=ledger,
            database_path=database,
            raw_root=raw_root,
            expected_anchor=anchor,
            pair_manifest=pair_manifest,
            expected_pair_manifest_hash=pair_hash,
        )
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            revoked = build_report(connection, revocation_config=config)
            revoked_monitor = build_monitor_snapshot(
                connection,
                revocation_config=config,
            )
        finally:
            connection.close()

    attempts = _attempted_production_connections(args.connection_audit)
    payload: dict[str, Any] = {
        "order_key": order_key,
        "decision_key": decision_key,
        "baseline_settled_orders": baseline.get("settled_orders"),
        "revoked_settled_orders": revoked.get("settled_orders"),
        "baseline_signals": (baseline.get("orders") or {}).get("signals"),
        "revoked_signals": (revoked.get("orders") or {}).get("signals"),
        "revoked_milestones": revoked.get("milestone_governance", {}).get(
            "revoked_milestones"
        ),
        "report_isolated_orders": revoked.get("governance_isolated_order_count"),
        "monitor_governance_status": revoked_monitor.get(
            "milestone_governance", {}
        ).get("status"),
        "monitor_revoked_milestones": revoked_monitor.get(
            "milestone_governance", {}
        ).get("revoked_milestones"),
        "baseline_monitor_total": baseline_monitor.get("summary", {}).get("total"),
        "revoked_monitor_total": revoked_monitor.get("summary", {}).get("total"),
        "pair_manifest_sha256": pair_hash,
        "anchor_sequence": anchor.get("sequence"),
        "attempted_production_connections": attempts,
        "guard_result": "passed" if attempts == 0 else "failed",
    }
    expected_milestones = ["M2", "M3-C", "M3-E", "M4-C", "M4-E"]
    passed = bool(
        attempts == 0
        and payload["baseline_settled_orders"] == 1
        and payload["revoked_settled_orders"] == 0
        and payload["baseline_signals"] == 1
        and payload["revoked_signals"] == 0
        and payload["report_isolated_orders"] == 1
        and payload["revoked_milestones"] == expected_milestones
        and payload["monitor_revoked_milestones"] == expected_milestones
    )
    payload["status"] = "passed" if passed else "failed"
    print(json.dumps(payload, sort_keys=True, default=_json_default))
    return 0 if passed else 2

def _workspace_head(workspace: Path) -> str:
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=workspace,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ValueError(f"recovery workspace is not a Git worktree: {workspace}")
    return completed.stdout.strip()


def run_rehearsal(args: argparse.Namespace) -> int:
    workspace = Path(__file__).resolve().parents[1]
    recovery_workspace = args.recovery_workspace.resolve(strict=True)
    production_database = args.production_database.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    recovery_head = _workspace_head(recovery_workspace)
    if recovery_head != BASELINE_COMMIT:
        raise ValueError(
            "recovery workspace must be based on exact clean baseline "
            f"{BASELINE_COMMIT}; got {recovery_head}"
        )
    recovery_module = recovery_workspace / "live_betting" / "pending_order_recovery.py"
    if not recovery_module.is_file():
        raise ValueError("recovery workspace does not contain pending-order worker")

    fixture_dir = output_dir / "fixture"
    fixture_db = fixture_dir / "rollback-fixture.db"
    commands_dir = output_dir / "commands"
    commands_dir.mkdir()
    python = str(Path(args.python_executable or sys.executable).resolve())
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (
            str(workspace),
            env.get("PYTHONPATH", ""),
        )
        if value
    )

    build = _run_command(
        (
            python,
            str(workspace / "scripts" / "p0_evidence.py"),
            "build-rollback-fixture",
            "--output-dir",
            str(fixture_dir),
            "--production-database",
            str(production_database),
            "--without-settlement-review",
        ),
        cwd=workspace,
        stdout_path=commands_dir / "01-build.stdout.jsonl",
        stderr_path=commands_dir / "01-build.stderr.log",
        env=env,
    )
    if build["exit_status"] != 0 or not fixture_db.is_file():
        raise RuntimeError("rollback fixture build failed")

    add_audit = commands_dir / "02-add-successor.sqlite.jsonl"
    add = _run_command(
        (
            python,
            str(Path(__file__).resolve()),
            "add-direct-successor",
            "--database",
            str(fixture_db),
            "--production-database",
            str(production_database),
            "--connection-audit",
            str(add_audit),
        ),
        cwd=workspace,
        stdout_path=commands_dir / "02-add-successor.stdout.jsonl",
        stderr_path=commands_dir / "02-add-successor.stderr.log",
        env=env,
    )
    if add["exit_status"] != 0:
        raise RuntimeError("direct successor insertion failed")

    recovery_audit = commands_dir / "03-recovery.sqlite.jsonl"
    recovery_code = (
        "from argparse import Namespace;"
        "from pathlib import Path;"
        "from scripts.p0_evidence import production_sqlite_guard;"
        "from live_betting.pending_order_recovery import _run_cli;"
        "from live_betting.service_coordination import database_writer_authority;"
        f"db=Path({str(fixture_db)!r});"
        f"prod=Path({str(production_database)!r});"
        f"audit=Path({str(recovery_audit)!r});"
        "ctx=production_sqlite_guard(prod,audit);ctx.__enter__();"
        "authority=database_writer_authority(db);authority.__enter__();"
        "status=0;"
        "\ntry:\n status=_run_cli(Namespace(database=db,once=True,interval=1.0))"
        "\nfinally:\n authority.__exit__(None,None,None);ctx.__exit__(None,None,None)"
        "\nraise SystemExit(status)"
    )
    recovery_env = env.copy()
    recovery_env["PYTHONPATH"] = os.pathsep.join(
        (str(recovery_workspace), str(workspace), env.get("PYTHONPATH", ""))
    )
    recovery = _run_command(
        (python, "-c", recovery_code),
        cwd=recovery_workspace,
        stdout_path=commands_dir / "03-recovery.stdout.jsonl",
        stderr_path=commands_dir / "03-recovery.stderr.log",
        env=recovery_env,
    )
    if recovery["exit_status"] != 0:
        raise RuntimeError("pending-order recovery failed")

    settle_audit = commands_dir / "04-settle.sqlite.jsonl"
    settle = _run_command(
        (
            python,
            str(Path(__file__).resolve()),
            "settle-current",
            "--database",
            str(fixture_db),
            "--production-database",
            str(production_database),
            "--connection-audit",
            str(settle_audit),
        ),
        cwd=workspace,
        stdout_path=commands_dir / "04-settle.stdout.jsonl",
        stderr_path=commands_dir / "04-settle.stderr.log",
        env=env,
    )
    if settle["exit_status"] != 0:
        raise RuntimeError("current authoritative settlement failed")

    verify_audit = commands_dir / "05-verify.sqlite.jsonl"
    verify = _run_command(
        (
            python,
            str(Path(__file__).resolve()),
            "verify-current",
            "--database",
            str(fixture_db),
            "--production-database",
            str(production_database),
            "--connection-audit",
            str(verify_audit),
        ),
        cwd=workspace,
        stdout_path=commands_dir / "05-verify.stdout.jsonl",
        stderr_path=commands_dir / "05-verify.stderr.log",
        env=env,
    )
    if verify["exit_status"] != 0:
        raise RuntimeError("current restart/idempotency verification failed")

    notification_audit = commands_dir / "06-notification-gate.sqlite.jsonl"
    notification = _run_command(
        (
            python,
            str(Path(__file__).resolve()),
            "notification-gate",
            "--source-database",
            str(fixture_db),
            "--database-copy",
            str(output_dir / "notification-gate.db"),
            "--production-database",
            str(production_database),
            "--connection-audit",
            str(notification_audit),
        ),
        cwd=workspace,
        stdout_path=commands_dir / "06-notification-gate.stdout.jsonl",
        stderr_path=commands_dir / "06-notification-gate.stderr.log",
        env=env,
    )
    if notification["exit_status"] != 0:
        raise RuntimeError("notification pre-send safety rehearsal failed")

    revocation_audit = commands_dir / "07-revocation.sqlite.jsonl"
    revocation = _run_command(
        (
            python,
            str(Path(__file__).resolve()),
            "revocation-projection",
            "--database",
            str(fixture_db),
            "--work-root",
            str(output_dir / "revocation-projection"),
            "--production-database",
            str(production_database),
            "--connection-audit",
            str(revocation_audit),
        ),
        cwd=workspace,
        stdout_path=commands_dir / "07-revocation.stdout.jsonl",
        stderr_path=commands_dir / "07-revocation.stderr.log",
        env=env,
    )
    if revocation["exit_status"] != 0:
        raise RuntimeError("settlement revocation projection rehearsal failed")

    phase_payloads = {
        "build": _last_json_line(Path(build["stdout"]["path"])),
        "add_successor": _last_json_line(Path(add["stdout"]["path"])),
        "recovery": _last_json_line(Path(recovery["stdout"]["path"])),
        "settle": _last_json_line(Path(settle["stdout"]["path"])),
        "verify": _last_json_line(Path(verify["stdout"]["path"])),
        "notification_gate": _last_json_line(Path(notification["stdout"]["path"])),
        "revocation": _last_json_line(Path(revocation["stdout"]["path"])),
    }
    attempted = sum(
        _attempted_production_connections(path)
        for path in (
            add_audit,
            recovery_audit,
            settle_audit,
            verify_audit,
            notification_audit,
            revocation_audit,
        )
    )
    passed = bool(
        attempted == 0
        and phase_payloads["recovery"].get("filled") == 1
        and phase_payloads["recovery"].get("pending_after") == 0
        and phase_payloads["settle"].get("first_settlement_inserted") is True
        and phase_payloads["settle"].get("second_settlement_inserted") is False
        and phase_payloads["verify"].get("counts_stable") is True
        and phase_payloads["verify"].get("second_process_inserted") is False
        and phase_payloads["notification_gate"].get("status") == "passed"
        and phase_payloads["revocation"].get("status") == "passed"
    )
    result = {
        "schema": RESULT_SCHEMA,
        "status": "passed" if passed else "failed",
        "current_workspace": str(workspace),
        "current_head": _workspace_head(workspace),
        "recovery_workspace": str(recovery_workspace),
        "recovery_head": recovery_head,
        "fixture_database": str(fixture_db),
        "production_database_sentinel": str(production_database),
        "attempted_production_connections": attempted,
        "phases": phase_payloads,
        "commands": {
            "build": build,
            "add_successor": add,
            "recovery": recovery,
            "settle": settle,
            "verify": verify,
            "notification_gate": notification,
            "revocation": revocation,
        },
    }
    result_path = output_dir / "rehearsal-result.json"
    _write_json(result_path, result)
    print(json.dumps({**result, "result_path": str(result_path)}, sort_keys=True, default=_json_default))
    return 0 if passed else 2


def _add_guard_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--production-database", type=Path, required=True)
    parser.add_argument("--connection-audit", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="execute the complete cross-version rehearsal")
    run.add_argument("--recovery-workspace", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--production-database", type=Path, required=True)
    run.add_argument("--python-executable")

    successor = commands.add_parser("add-direct-successor", help=argparse.SUPPRESS)
    _add_guard_arguments(successor)
    successor.add_argument("--price", type=float, default=2.1)
    successor.add_argument("--seconds-after-signal", type=int, default=2)

    settle = commands.add_parser("settle-current", help=argparse.SUPPRESS)
    _add_guard_arguments(settle)

    verify = commands.add_parser("verify-current", help=argparse.SUPPRESS)
    _add_guard_arguments(verify)

    notification = commands.add_parser("notification-gate", help=argparse.SUPPRESS)
    notification.add_argument("--source-database", type=Path, required=True)
    notification.add_argument("--database-copy", type=Path, required=True)
    notification.add_argument("--production-database", type=Path, required=True)
    notification.add_argument("--connection-audit", type=Path, required=True)

    revocation = commands.add_parser("revocation-projection", help=argparse.SUPPRESS)
    _add_guard_arguments(revocation)
    revocation.add_argument("--work-root", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "run":
        return run_rehearsal(args)
    if args.command == "add-direct-successor":
        return add_direct_successor(args)
    if args.command == "settle-current":
        return settle_current(args)
    if args.command == "verify-current":
        return verify_current(args)
    if args.command == "notification-gate":
        return notification_gate(args)
    if args.command == "revocation-projection":
        return revocation_projection(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
