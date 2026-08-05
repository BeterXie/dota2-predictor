"""Allow fitted parameters on gate-failed prematch calibrations.

Revision ID: 20260805_0025
Revises: 20260805_0024
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text


revision: str = "20260805_0025"
down_revision: str | None = "20260805_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT_NAME = "ck_prematch_calibration_parameters_by_status"


def _drop_parameters_status_constraint() -> None:
    op.execute(
        text(
            """
            DO $migration$
            DECLARE
                constraint_names text[];
            BEGIN
                SELECT array_agg(constraint_row.conname ORDER BY constraint_row.conname)
                  INTO constraint_names
                  FROM pg_constraint AS constraint_row
                  JOIN pg_class AS relation
                    ON relation.oid = constraint_row.conrelid
                  JOIN pg_namespace AS namespace
                    ON namespace.oid = relation.relnamespace
                 WHERE namespace.nspname = current_schema()
                   AND relation.relname = 'prematch_calibration_artifacts'
                   AND constraint_row.contype = 'c'
                   AND position(
                       'parameters_json IS NULL' IN
                       pg_get_constraintdef(constraint_row.oid)
                   ) > 0
                   AND position(
                       'unsupported' IN pg_get_constraintdef(constraint_row.oid)
                   ) > 0
                   AND position(
                       'failed' IN pg_get_constraintdef(constraint_row.oid)
                   ) > 0;
                IF coalesce(cardinality(constraint_names), 0) > 1 THEN
                    RAISE EXCEPTION
                        'expected at most one prematch calibration parameter/status constraint, found %',
                        coalesce(cardinality(constraint_names), 0);
                ELSIF coalesce(cardinality(constraint_names), 0) = 1 THEN
                    EXECUTE format(
                        'ALTER TABLE prematch_calibration_artifacts DROP CONSTRAINT %I',
                        constraint_names[1]
                    );
                END IF;
            END;
            $migration$;
            """
        )
    )


def upgrade() -> None:
    _drop_parameters_status_constraint()
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "prematch_calibration_artifacts",
        "(status = 'unsupported' AND parameters_json IS NULL) OR "
        "status = 'failed' OR "
        "(status IN ('provisional', 'reconstructed_only', "
        "'shadow_collecting', 'passed') AND parameters_json IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        _CONSTRAINT_NAME,
        "prematch_calibration_artifacts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_prematch_calibration_parameters_by_status_v1",
        "prematch_calibration_artifacts",
        "(status IN ('unsupported', 'failed')) = (parameters_json IS NULL)",
    )
