from __future__ import annotations

from web.alerts import _conditions


def test_downstream_worker_failures_create_operational_conditions() -> None:
    conditions = _conditions(
        None,  # type: ignore[arg-type]
        [
            {
                "component": "map_decision_worker",
                "status": "unhealthy",
                "last_error": "checkpoint loop failed",
            },
            {
                "component": "postmatch_worker",
                "status": "degraded",
                "last_error": "official match still unlinked",
            },
        ],
    )

    assert conditions["operational:map_decision_worker"]["severity"] == "critical"
    assert conditions["operational:postmatch_worker"]["severity"] == "warning"


def test_starting_downstream_workers_do_not_create_operational_conditions() -> None:
    conditions = _conditions(
        None,  # type: ignore[arg-type]
        [
            {"component": "map_decision_worker", "status": "starting"},
            {"component": "postmatch_worker", "status": "healthy"},
        ],
    )

    assert conditions == {}
