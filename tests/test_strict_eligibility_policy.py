from __future__ import annotations

from datetime import datetime, timezone

from live_betting.strict_eligibility import _EventPolicy, _formal_event_reason


NOW = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)


def _event(*, tier: str, prize_pool_usd: int) -> _EventPolicy:
    return _EventPolicy(
        event_id="audited-event",
        canonical_name="Audited Event",
        scope="formal_main_event",
        approval_status="approved",
        evidence_status="manually_audited",
        tier=tier,
        prize_pool_usd=prize_pool_usd,
        approved_by="manual_event_audit",
        approved_at=NOW,
        main_event_start_at=NOW,
        main_event_end_at=NOW,
        official_evidence_urls=("https://example.test/event",),
        included_stages=("main_event",),
        excluded_categories=(
            "qualifier",
            "division_2",
            "exhibition",
            "forfeit",
            "void_remake",
        ),
        include_internal_lcq=False,
        exclusion_flags=(True, True, True, True, True),
    )


def test_audited_tier_two_event_is_formally_eligible() -> None:
    assert _formal_event_reason(_event(tier="tier_2", prize_pool_usd=0), NOW) is None


def test_unknown_event_tier_remains_ineligible() -> None:
    assert (
        _formal_event_reason(_event(tier="tier_3", prize_pool_usd=1_000_000), NOW)
        == "event_tier_not_formal"
    )


def test_negative_event_prize_remains_ineligible() -> None:
    assert (
        _formal_event_reason(_event(tier="tier_2", prize_pool_usd=-1), NOW)
        == "event_prize_below_minimum"
    )
