"""Deliver outbox notifications with at-least-once SMTP semantics."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_betting.notifications import (  # noqa: E402
    claim,
    ensure_sendable,
    mark_failure,
    mark_sent,
)
from live_betting.health import record_health  # noqa: E402
from live_betting.service_coordination import (  # noqa: E402
    add_single_database_argument,
    database_writer_authority,
)
from live_betting.smtp_delivery import (  # noqa: E402
    SMTPConfig,
    SMTPConfigurationError,
    classify_error,
    send_message,
)
from live_betting.storage import LiveBettingStore  # noqa: E402


logger = logging.getLogger(__name__)


def run_once(store: LiveBettingStore, config: SMTPConfig) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    record = claim(store.connection, now=now)
    if record is None:
        return {"status": "idle"}
    assert record.lease_token is not None
    send_check_at = datetime.now(timezone.utc)
    if not ensure_sendable(
        store.connection,
        outbox_id=record.outbox_id,
        lease_token=record.lease_token,
        now=send_check_at,
    ):
        return {
            "status": "suppressed",
            "outbox_id": record.outbox_id,
        }
    try:
        send_message(record, config)
    except Exception as error:  # noqa: BLE001 - classify without logging secrets
        transient, reason = classify_error(error)
        failed_at = datetime.now(timezone.utc)
        updated = mark_failure(
            store.connection,
            outbox_id=record.outbox_id,
            lease_token=record.lease_token,
            transient=transient,
            reason=reason,
            now=failed_at,
        )
        if not updated:
            status = "lease_lost"
        else:
            row = store.connection.execute(
                "SELECT status FROM notification_outbox WHERE outbox_id=?",
                (record.outbox_id,),
            ).fetchone()
            stored_status = str(row[0]) if row is not None else "missing"
            status = {
                "pending": "retry_scheduled",
                "dead_letter": "dead_letter",
            }.get(stored_status, f"state_{stored_status}")
        return {
            "status": status,
            "outbox_id": record.outbox_id,
            "updated": updated,
            "reason": reason,
        }
    completed_at = datetime.now(timezone.utc)
    updated = mark_sent(
        store.connection,
        outbox_id=record.outbox_id,
        lease_token=record.lease_token,
        sent_at=completed_at,
    )
    if updated:
        status = "sent"
    else:
        audit = store.connection.execute(
            """SELECT action FROM notification_outbox_audit
                WHERE outbox_id=? ORDER BY audit_id DESC LIMIT 1""",
            (record.outbox_id,),
        ).fetchone()
        status = (
            "sent_then_quarantined"
            if audit is not None and str(audit[0]) == "sent_then_quarantined"
            else "lease_lost"
        )
    return {"status": status, "outbox_id": record.outbox_id}


def _run_cli(args: argparse.Namespace) -> int:
    with LiveBettingStore(args.database) as store:
        if not getattr(args, "schema_prepared", False):
            store.init_schema()
        try:
            config = SMTPConfig.from_environment()
        except SMTPConfigurationError:
            now = datetime.now(timezone.utc)
            record_health(
                store.connection,
                "mail_worker",
                "degraded",
                heartbeat_at=now,
                error_at=now,
                error="configuration_missing",
                details={"source": "worker"},
            )
            print(json.dumps({
                "status": "mail_unhealthy",
                "reason": "configuration_missing",
            }))
            return 0
        started_at = datetime.now(timezone.utc)
        record_health(
            store.connection,
            "mail_worker",
            "starting",
            heartbeat_at=started_at,
            details={"source": "worker"},
        )
        while True:
            try:
                result = run_once(store, config)
                heartbeat = datetime.now(timezone.utc)
                run_status = str(result.get("status", "unknown"))
                degraded = run_status in {
                    "dead_letter",
                    "lease_lost",
                    "retry_scheduled",
                    "sent_then_quarantined",
                } or run_status.startswith("state_")
                record_health(
                    store.connection,
                    "mail_worker",
                    "degraded" if degraded else "healthy",
                    heartbeat_at=heartbeat,
                    success_at=None if degraded else heartbeat,
                    error_at=heartbeat if degraded else None,
                    error=run_status if degraded else None,
                    details={"source": "worker", "run_status": run_status},
                )
                print(json.dumps(result, ensure_ascii=False))
            except Exception as error:
                failed_at = datetime.now(timezone.utc)
                record_health(
                    store.connection,
                    "mail_worker",
                    "degraded",
                    heartbeat_at=failed_at,
                    error_at=failed_at,
                    error=type(error).__name__,
                    details={"source": "worker"},
                )
                logger.exception("notification worker iteration failed")
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(parser, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    with database_writer_authority(args.database):
        return _run_cli(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
