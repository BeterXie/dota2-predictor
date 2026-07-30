from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError

from database.engine import build_engine, require_database_url


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def postgres_engine() -> Iterator[Engine]:
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    base_url = make_url(require_database_url(configured))
    database_name = f"dota2_predictor_order_test_{uuid4().hex}"
    admin_engine = create_engine(
        base_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    test_url = base_url.set(database=database_name)
    engine: Engine | None = None
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option(
            "sqlalchemy.url",
            test_url.render_as_string(hide_password=False),
        )
        command.upgrade(config, "head")
        engine = build_engine(test_url.render_as_string(hide_password=False))
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
        admin_engine.dispose()


def _insert_pending_order(connection, order_key: str) -> None:
    connection.execute(
        text(
            """
            INSERT INTO shadow_orders (
                order_key, raybet_match_id, odds_id, market_key, signaled_at,
                model_probability, market_probability, signal_price,
                signal_transport_key, signal_transport_at, expires_at,
                signal_identity_verified, stake, status
            ) VALUES (
                :order_key, 'match-1', 'odds-1', 'winner|map_1|team_one|',
                '2026-07-30T00:00:00Z', 0.61, 0.48, 2.1,
                'transport-1', '2026-07-30T00:00:00Z',
                '2026-07-30T00:02:00Z', 1, 0.25, 'pending'
            )
            """
        ),
        {"order_key": order_key},
    )


def test_shadow_order_schema_links_all_declared_authorities(
    postgres_engine: Engine,
) -> None:
    inspector = inspect(postgres_engine)
    assert {
        "live_frames",
        "live_events",
        "model_quotes",
        "shadow_orders",
        "shadow_order_decision_lineage",
    } <= set(inspector.get_table_names())

    targets = {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys("shadow_orders")
    }
    assert {
        "strict_live_map_mappings",
        "prospective_draft_curves",
        "prospective_draft_landmarks",
        "draft_deployment_bundles",
        "draft_model_artifacts",
        "draft_calibration_artifacts",
    } <= targets

    with postgres_engine.connect() as connection:
        trigger_names = set(
            connection.execute(
                text(
                    "SELECT trigger_name FROM information_schema.triggers "
                    "WHERE event_object_table = 'shadow_orders'"
                )
            ).scalars()
        )
    assert {
        "shadow_orders_terminal_immutable",
        "shadow_orders_immutable_delete",
        "strict_live_shadow_impact_after_insert",
    } <= trigger_names


def test_pending_order_allows_one_terminal_transition_only(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        _insert_pending_order(connection, "order-1")
        connection.execute(
            text(
                "UPDATE shadow_orders "
                "SET status = 'filled', fill_price = 2.05, "
                "filled_at = '2026-07-30T00:01:00Z' "
                "WHERE order_key = 'order-1'"
            )
        )

    with postgres_engine.connect() as connection:
        state = connection.execute(
            text(
                "SELECT status, fill_price, filled_at, rejection_reason "
                "FROM shadow_orders WHERE order_key = 'order-1'"
            )
        ).one()
    assert state == ("filled", 2.05, "2026-07-30T00:01:00Z", None)

    with pytest.raises(DBAPIError, match="terminal state is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE shadow_orders "
                    "SET status = 'rejected', fill_price = NULL, filled_at = NULL, "
                    "rejection_reason = 'changed' WHERE order_key = 'order-1'"
                )
            )

    with pytest.raises(DBAPIError, match="shadow orders are immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM shadow_orders WHERE order_key = 'order-1'")
            )


def test_pending_order_payload_and_decision_lineage_are_immutable(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        _insert_pending_order(connection, "order-2")
        connection.execute(
            text(
                "INSERT INTO shadow_order_decision_lineage "
                "(order_key, decision_key, recorded_at) VALUES "
                "('order-2', 'decision-1', '2026-07-30T00:00:00Z')"
            )
        )

    with pytest.raises(DBAPIError, match="terminal state is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE shadow_orders SET stake = 0.5 "
                    "WHERE order_key = 'order-2'"
                )
            )

    with pytest.raises(DBAPIError, match="decision lineage is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE shadow_order_decision_lineage "
                    "SET decision_key = 'decision-2' WHERE order_key = 'order-2'"
                )
            )
