from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

# These modules still construct SQLite files or assert retired SQLite-only
# operations. Keep them discoverable, but exclude them from PostgreSQL CI until
# their fixtures are rewritten against the PostgreSQL integration harness.
LEGACY_SQLITE_MODULES = frozenset(
    {
        "tests/test_accept_strict_live_mapping_cli.py",
        "tests/test_browser_ingest.py",
        "tests/test_browser_storage.py",
        "tests/test_cli_docs_consistency.py",
        "tests/test_database_bundle.py",
        "tests/test_database_cutover.py",
        "tests/test_database_protocol.py",
        "tests/test_direct_source_isolation.py",
        "tests/test_draft_backtest.py",
        "tests/test_event_registry.py",
        "tests/test_fetch_authority.py",
        "tests/test_fetch_db_transaction.py",
        "tests/test_historical_rosh_backfill.py",
        "tests/test_historical_rosh_cli.py",
        "tests/test_historical_rosh_storage.py",
        "tests/test_historical_rosh_worker.py",
        "tests/test_ingest_adapters.py",
        "tests/test_intelligence_report.py",
        "tests/test_intelligence_storage.py",
        "tests/test_live_betting.py",
        "tests/test_live_landmark_strategy.py",
        "tests/test_live_report.py",
        "tests/test_monitor_alerts.py",
        "tests/test_monitor_control.py",
        "tests/test_monitoring_dashboard.py",
        "tests/test_notification_outbox.py",
        "tests/test_odds_legacy_compactor.py",
        "tests/test_odds_response_storage_v2.py",
        "tests/test_official_rosh_run_coordinator.py",
        "tests/test_official_rosh_shadow_runtime.py",
        "tests/test_player_scoring.py",
        "tests/test_postmatch_identity.py",
        "tests/test_postmatch_settlement.py",
        "tests/test_raybet_collector_resilience.py",
        "tests/test_raybet_direct_response_audit.py",
        "tests/test_raybet_sanitization.py",
        "tests/test_raybet_stream_scripts.py",
        "tests/test_realtime_vision.py",
        "tests/test_research_live_predictions.py",
        "tests/test_role_persistence.py",
        "tests/test_rosh_lineup_storage.py",
        "tests/test_rosh_parity.py",
        "tests/test_rosh_parity_storage.py",
        "tests/test_service_coordination_cli.py",
        "tests/test_settlement_authority.py",
        "tests/test_shadow_monitor_safety.py",
        "tests/test_strategy_contract_storage.py",
        "tests/test_strict_live_eligibility.py",
        "tests/test_successor_fill.py",
        "tests/test_team_profiles.py",
        "tests/test_vision_frame_integrity.py",
        "tests/test_vision_retention.py",
        "tests/test_web_intelligence.py",
        "tests/test_web_postmatch_link.py",
        "tests/test_web_prematch.py",
        "tests/test_web_rosh_analysis.py",
        "tests/test_winner_timeline_v2.py",
    }
)
LEGACY_SQLITE_MODULE_BUDGET = 57
actual_legacy_sqlite_modules = len(LEGACY_SQLITE_MODULES)
if actual_legacy_sqlite_modules != LEGACY_SQLITE_MODULE_BUDGET:
    raise RuntimeError(
        "legacy_sqlite budget must match the current module set: "
        f"{actual_legacy_sqlite_modules} modules != "
        f"{LEGACY_SQLITE_MODULE_BUDGET} allowed"
    )
missing_legacy_sqlite_modules = sorted(
    path for path in LEGACY_SQLITE_MODULES if not (ROOT / path).is_file()
)
if missing_legacy_sqlite_modules:
    raise RuntimeError(
        "legacy_sqlite contains missing test modules: "
        + ", ".join(missing_legacy_sqlite_modules)
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "legacy_sqlite: depends on the retired SQLite runtime or operations",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    marker = pytest.mark.legacy_sqlite
    for item in items:
        try:
            relative_path = item.path.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            continue
        if relative_path in LEGACY_SQLITE_MODULES:
            item.add_marker(marker)
