"""PostgreSQL storage for strict-event intelligence."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy.engine import Engine

from database.engine import build_engine
from database.session import DatabaseResult, PostgresSession


CURRENT_SCHEMA_VERSION = 10
ALEMBIC_HEAD = "20260807_0034"


class IntelligenceStorage:
    """PostgreSQL storage for event-intelligence runtime state."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        if database_url is not None and engine is not None:
            raise ValueError("database_url and engine are mutually exclusive")
        self.engine = engine or build_engine(database_url)
        self._owns_engine = engine is None
        self.connection = PostgresSession(self.engine)
        self._transaction_depth = 0

    def __enter__(self) -> "IntelligenceStorage":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()
        if self._owns_engine:
            self.engine.dispose()

    def init_schema(
        self,
        *,
        seed_events: bool = True,
        external_transaction: bool = False,
    ) -> None:
        revision = self.connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        if revision is None or str(revision[0]) != ALEMBIC_HEAD:
            actual = None if revision is None else str(revision[0])
            raise RuntimeError(
                f"PostgreSQL schema revision {actual!r} is not {ALEMBIC_HEAD}"
            )
        transaction = (
            self._external_transaction()
            if external_transaction
            else self.transaction()
        )
        with transaction:
            if seed_events:
                from .registry import EventRegistry

                registry = EventRegistry(self)
                registry.seed_approved_events()
                self._verify_seeded_events(registry)

    @contextmanager
    def _external_transaction(self) -> Iterator[None]:
        if not self.connection.in_transaction:
            raise RuntimeError("external transaction is not active")
        self._transaction_depth += 1
        try:
            yield
        finally:
            self._transaction_depth -= 1

    @staticmethod
    def _verify_seeded_events(registry: object) -> None:
        from .registry import (
            APPROVED_EVENT_SEEDS,
            AUDITED_AT,
            EXCLUDED_CATEGORIES,
            SCOPE_POLICY_VERSION,
        )

        events = {event.event_id: event for event in registry.formal_events()}
        expected_ids = {str(seed["event_id"]) for seed in APPROVED_EVENT_SEEDS}
        if set(events) != expected_ids:
            raise RuntimeError(
                "approved event seed set conflicts with the audited registry"
            )
        for seed in APPROVED_EVENT_SEEDS:
            event = events[str(seed["event_id"])]
            expected_map_count = seed["expected_map_count"]
            expected = (
                str(seed["canonical_name"]),
                str(seed["tier"]),
                int(seed["prize_pool_usd"]),
                datetime.fromisoformat(str(seed["main_event_start_at"])),
                datetime.fromisoformat(str(seed["main_event_end_at"])),
                int(seed["opendota_league_id"]),
                (),
                tuple(seed["official_evidence_urls"]),
                "manually_audited",
                SCOPE_POLICY_VERSION,
                "formal_main_event",
                "approved",
                "manual_event_audit",
                datetime.fromisoformat(AUDITED_AT),
                (int(expected_map_count) if expected_map_count is not None else None),
                tuple(seed["included_stages"]),
                tuple(EXCLUDED_CATEGORIES),
                bool(seed["include_internal_lcq"]),
            )
            actual = (
                event.canonical_name,
                event.tier,
                event.prize_pool_usd,
                event.main_event_start_at,
                event.main_event_end_at,
                event.opendota_league_id,
                event.secondary_provider_ids,
                event.official_evidence_urls,
                event.evidence_status.value,
                event.scope_policy_version,
                event.scope.value,
                event.approval_status.value,
                event.approved_by,
                event.approved_at,
                event.expected_map_count,
                tuple(stage.value for stage in event.included_stages),
                event.excluded_categories,
                event.include_internal_lcq,
            )
            if actual != expected:
                raise RuntimeError(
                    f"approved event seed policy drift for {event.event_id}"
                )

    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> DatabaseResult:
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
        with self.connection.transaction():
            self._transaction_depth += 1
            try:
                yield
            finally:
                self._transaction_depth -= 1
