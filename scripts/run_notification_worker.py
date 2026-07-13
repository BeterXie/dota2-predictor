"""Deliver simulation notifications from the transactional outbox."""

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

from live_betting.notifications import claim, mark_failure, mark_sent  # noqa: E402
from live_betting.health import record_health  # noqa: E402
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
    try:
        send_message(record, config)
    except Exception as error:  # noqa: BLE001 - classify without logging secrets
        transient, reason = classify_error(error)
        updated = mark_failure(
            store.connection,
            outbox_id=record.outbox_id,
            lease_token=record.lease_token,
            transient=transient,
            reason=reason,
            now=now,
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
    updated = mark_sent(
        store.connection,
        outbox_id=record.outbox_id,
        lease_token=record.lease_token,
        sent_at=now,
    )
    return {"status": "sent" if updated else "lease_lost", "outbox_id": record.outbox_id}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    with LiveBettingStore(args.database) as store:
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
