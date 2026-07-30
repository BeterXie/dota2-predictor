"""Create monitor control, alert, and runtime contract tables.

Revision ID: 20260730_0009
Revises: 20260730_0008
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0009"
down_revision: str | None = "20260730_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_CONTRACT_DIGEST = (
    "7403fa6318b671f024b8765179b87e33ad0faf2b5d67ac6e90d1e689be3816fe"
)
COMPONENTS = (
    "'raybet_collector', 'shadow_monitor', 'vision_supervisor', "
    "'draft_publisher', 'mail_worker'"
)


def upgrade() -> None:
    op.create_table(
        "runtime_schema_version",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("contract_digest", sa.Text(), nullable=False),
        sa.Column("installed_at", sa.Text(), nullable=False),
        sa.CheckConstraint("version > 0"),
        sa.CheckConstraint("length(contract_digest) = 64"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO runtime_schema_version (
                version, contract_digest, installed_at
            ) VALUES (
                1, :contract_digest,
                replace(
                    to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                    '_', ':'
                )
            )
            """
        ).bindparams(contract_digest=RUNTIME_CONTRACT_DIGEST)
    )
    _widen_notification_events()
    _create_control_tables()
    _create_alert_tables()
    _create_runtime_triggers()


def _widen_notification_events() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE constraint_name text;
            BEGIN
                SELECT con.conname
                INTO constraint_name
                FROM pg_constraint AS con
                JOIN pg_class AS relation ON relation.oid = con.conrelid
                WHERE relation.relname = 'notification_outbox'
                  AND con.contype = 'c'
                  AND pg_get_constraintdef(con.oid) LIKE '%event_type%'
                LIMIT 1;
                IF constraint_name IS NOT NULL THEN
                    EXECUTE format(
                        'ALTER TABLE notification_outbox DROP CONSTRAINT %I',
                        constraint_name
                    );
                END IF;
            END;
            $$
            """
        )
    )
    op.create_check_constraint(
        "ck_notification_outbox_event_type",
        "notification_outbox",
        "event_type IN ('filled', 'settled', 'monitor_alert', 'monitor_recovery')",
    )
    op.create_index(
        "idx_notification_outbox_due",
        "notification_outbox",
        ["status", "next_attempt_at", "lease_until"],
    )


def _create_control_tables() -> None:
    op.create_table(
        "monitor_process_registry",
        sa.Column("component", sa.Text(), primary_key=True),
        sa.Column("pid", sa.BigInteger()),
        sa.Column("command_hash", sa.Text(), nullable=False),
        sa.Column("command_json", sa.Text(), nullable=False),
        sa.Column("process_created_at", sa.Double()),
        sa.Column("started_at", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(f"component IN ({COMPONENTS})"),
        sa.CheckConstraint("status IN ('running', 'stopped')"),
    )
    op.create_table(
        "monitor_control_audit",
        sa.Column("audit_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False, unique=True),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("ok", sa.SmallInteger(), nullable=False),
        sa.Column("pid", sa.BigInteger()),
        sa.Column("command_hash", sa.Text()),
        sa.Column("process_created_at", sa.Double()),
        sa.Column("client_host", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.Text(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.CheckConstraint(f"component IN ({COMPONENTS})"),
        sa.CheckConstraint("action IN ('start', 'stop', 'restart')"),
        sa.CheckConstraint("ok IN (0, 1)"),
    )


def _create_alert_tables() -> None:
    op.create_table(
        "monitor_alert_candidates",
        sa.Column("dedupe_key", sa.Text(), primary_key=True),
        sa.Column("first_detected_at", sa.Text(), nullable=False),
        sa.Column("last_detected_at", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "monitor_alert_incidents",
        sa.Column("incident_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("first_detected_at", sa.Text(), nullable=False),
        sa.Column("opened_at", sa.Text(), nullable=False),
        sa.Column("last_detected_at", sa.Text(), nullable=False),
        sa.Column("recovered_at", sa.Text()),
        sa.Column("acknowledged_at", sa.Text()),
        sa.Column("acknowledged_by", sa.Text()),
        sa.Column("source_json", sa.Text(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("dedupe_key", "episode"),
        sa.CheckConstraint("episode > 0"),
        sa.CheckConstraint("category IN ('operational', 'paper_signal')"),
        sa.CheckConstraint("severity IN ('warning', 'critical')"),
        sa.CheckConstraint("status IN ('active', 'recovered')"),
        sa.CheckConstraint("occurrence_count > 0"),
    )
    op.create_table(
        "monitor_alert_audit",
        sa.Column("audit_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "incident_id",
            sa.BigInteger(),
            sa.ForeignKey("monitor_alert_incidents.incident_id"),
            nullable=False,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "action IN ('opened', 'observed', 'acknowledged', 'recovered')"
        ),
    )
    op.create_index(
        "idx_monitor_alert_active_key",
        "monitor_alert_incidents",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "idx_monitor_alert_status_opened",
        "monitor_alert_incidents",
        ["status", sa.text("opened_at DESC")],
    )


def _create_runtime_triggers() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION guard_notification_outbox_payload()
            RETURNS trigger AS $$
            BEGIN
                IF OLD.order_key IS DISTINCT FROM NEW.order_key
                    OR OLD.event_type IS DISTINCT FROM NEW.event_type
                    OR OLD.channel IS DISTINCT FROM NEW.channel
                    OR OLD.payload_json IS DISTINCT FROM NEW.payload_json
                    OR OLD.statistics_cutoff IS DISTINCT FROM NEW.statistics_cutoff
                    OR OLD.template_version IS DISTINCT FROM NEW.template_version
                    OR OLD.recipient IS DISTINCT FROM NEW.recipient
                    OR OLD.message_id IS DISTINCT FROM NEW.message_id
                THEN
                    RAISE EXCEPTION 'notification outbox payload is immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER notification_outbox_payload_immutable
            BEFORE UPDATE ON notification_outbox
            FOR EACH ROW EXECUTE FUNCTION guard_notification_outbox_payload()
            """
        )
    )
    for table, prefix, update_message, delete_message in (
        (
            "monitor_control_audit",
            "monitor_control_audit",
            "monitor control audit rows are immutable",
            "monitor control audit rows cannot be deleted",
        ),
        (
            "monitor_alert_audit",
            "monitor_alert_audit",
            "monitor alert audit rows are immutable",
            "monitor alert audit rows cannot be deleted",
        ),
    ):
        for operation, message in (
            ("UPDATE", update_message),
            ("DELETE", delete_message),
        ):
            op.execute(
                sa.text(
                    f"""
                    CREATE TRIGGER {prefix}_no_{operation.lower()}
                    BEFORE {operation} ON {table}
                    FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                        '{message}'
                    )
                    """
                )
            )


def downgrade() -> None:
    op.drop_table("monitor_alert_audit")
    op.drop_table("monitor_alert_incidents")
    op.drop_table("monitor_alert_candidates")
    op.drop_table("monitor_control_audit")
    op.drop_table("monitor_process_registry")
    op.drop_table("runtime_schema_version")
    op.drop_index("idx_notification_outbox_due", table_name="notification_outbox")
    op.drop_constraint(
        "ck_notification_outbox_event_type",
        "notification_outbox",
        type_="check",
    )
    op.create_check_constraint(
        "notification_outbox_event_type_check",
        "notification_outbox",
        "event_type IN ('filled', 'settled')",
    )
    op.execute(
        sa.text(
            "DROP TRIGGER notification_outbox_payload_immutable "
            "ON notification_outbox"
        )
    )
    op.execute(sa.text("DROP FUNCTION guard_notification_outbox_payload()"))
