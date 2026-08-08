from __future__ import annotations

from live_betting import runtime_schema


def test_runtime_control_contract_contains_retained_workers() -> None:
    assert runtime_schema.CONTROL_COMPONENT_NAMES == (
        "raybet_collector",
        "vision_supervisor",
    )


def test_runtime_contract_no_longer_requires_notification_outbox() -> None:
    assert "notification_outbox" not in runtime_schema._REQUIRED_TABLES
    assert "idx_notification_outbox_due" not in runtime_schema._REQUIRED_INDEXES
    assert "notification_outbox_payload_immutable" not in runtime_schema._REQUIRED_TRIGGERS
