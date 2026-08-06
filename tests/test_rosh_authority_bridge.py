from __future__ import annotations

from dataclasses import replace

from event_intelligence.rosh_authority_bridge import (
    _LegacyRow,
    _audit_player_identity,
)
from event_intelligence.rosh_support_funnel import _legacy_funnel
from scripts.verify_rosh_authority_bridge import (
    DIRE,
    DIRE_PLAYERS,
    MATCH_ID,
    RADIANT,
    RADIANT_PLAYERS,
    _target,
)


def _row() -> _LegacyRow:
    return _LegacyRow(
        score_key="a" * 64,
        match_id=MATCH_ID,
        radiant_heroes=RADIANT,
        dire_heroes=DIRE,
        radiant_players=None,
        dire_players=None,
        player_coverage_count=0,
        formula_version="test",
        evidence={},
        evidence_hash_valid=True,
    )


def _diagnostics(row: _LegacyRow) -> tuple[int, dict[str, int]]:
    support, diagnostics = _audit_player_identity((row,), {MATCH_ID: _target()})
    return support, {item.reason: item.support for item in diagnostics}


def test_missing_player_identity_is_optional_audit_evidence() -> None:
    assert _diagnostics(_row()) == (
        0,
        {
            "player_coverage_incomplete": 1,
            "player_ids_unavailable": 1,
        },
    )


def test_mismatched_player_identity_remains_diagnostic() -> None:
    row = replace(
        _row(),
        radiant_players=(901, 902, 903, 904, 905),
        dire_players=DIRE_PLAYERS,
        player_coverage_count=10,
    )

    assert _diagnostics(row) == (0, {"player_identity_mismatch": 1})


def test_matching_player_identity_is_recorded_but_not_required() -> None:
    row = replace(
        _row(),
        radiant_players=RADIANT_PLAYERS,
        dire_players=DIRE_PLAYERS,
        player_coverage_count=10,
    )

    assert _diagnostics(row) == (1, {})


def test_legacy_support_funnel_has_no_player_coverage_gate() -> None:
    stages = _legacy_funnel(
        (
            {
                "match_id": MATCH_ID,
                "radiant_hero_ids_json": str(list(RADIANT)),
                "dire_hero_ids_json": str(list(DIRE)),
                "backtest_eligible": True,
            },
        ),
        formal_match_ids={MATCH_ID},
        exact_position_ids={MATCH_ID},
        official_match_ids={MATCH_ID},
    )

    assert "player_coverage_complete" not in {stage.stage for stage in stages}
