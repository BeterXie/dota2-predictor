from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from event_intelligence.legacy_rosh_reconstruction import (
    LEGACY_ROSH_FORMULA_VERSION,
    LegacyRoshStoredRecord,
    classify_legacy_rosh_record,
    recompute_legacy_pure_score,
)
from prematch.stratz_rosh import normalize_rosh_analysis, score_rosh_picks


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "stratz-rosh.json"
UTC = timezone.utc


def _hash(value: object) -> str:
    body = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(body).hexdigest()


def _evidence() -> tuple[dict[str, object], float]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    radiant = tuple(fixture["radiant_heroes"])
    dire = tuple(fixture["dire_heroes"])
    radiant_picks = [
        {"heroId": hero_id, "positionId": position}
        for position, hero_id in enumerate(radiant, 1)
    ]
    dire_picks = [
        {"heroId": hero_id, "positionId": position}
        for position, hero_id in enumerate(dire, 1)
    ]
    analysis = normalize_rosh_analysis(fixture["responses"])
    scored = score_rosh_picks(radiant_picks, dire_picks, analysis)
    pure_score = float(scored["pure_lineup_score"])
    source_at = datetime(2026, 1, 1, tzinfo=UTC)
    evidence: dict[str, object] = {
        "source": "stratz",
        "source_week": int(source_at.timestamp()),
        "source_as_of": source_at.isoformat(),
        "cache_week_start": int(source_at.timestamp()),
        "formula_version": LEGACY_ROSH_FORMULA_VERSION,
        "historical_match_id": 123,
        "response_hashes": {"heroes": "a" * 64},
        "player_response_hashes": {},
        "player_slots": [],
        "player_stats_as_of": None,
        "retrospective": True,
        "current_player_adjustment_only": True,
        "backtest_eligible": False,
        "pure_minute_table": scored["pure_minute_table"],
        "score": {
            "pure_lineup_score": pure_score,
            "current_player_adjusted_lineup_score": None,
            "effective_lineup_score": pure_score,
            "scoring_mode": "pure",
            "player_coverage_count": 0,
        },
        "legacy_formula_inputs": {
            "radiant_picks": radiant_picks,
            "dire_picks": dire_picks,
            "analysis": analysis,
        },
    }
    return evidence, pure_score


def _record(*, cutoff_safe: bool = True) -> LegacyRoshStoredRecord:
    evidence, score = _evidence()
    source_at = datetime.fromtimestamp(int(evidence["source_week"]), tz=UTC)
    cutoff = source_at + timedelta(days=1 if cutoff_safe else -1)
    return LegacyRoshStoredRecord(
        match_id=123,
        score_key="b" * 64,
        formula_version=LEGACY_ROSH_FORMULA_VERSION,
        prediction_cutoff=cutoff,
        source_week=int(evidence["source_week"]),
        source_as_of=str(evidence["source_as_of"]),
        evidence=evidence,
        evidence_hash=_hash(evidence),
        stored_score=score,
        radiant_hero_ids=(1, 2, 3, 4, 5),
        dire_hero_ids=(6, 7, 8, 9, 10),
        radiant_expected=(1, 2, 3, 4, 5),
        dire_expected=(6, 7, 8, 9, 10),
        event_id="test-event",
        patch=741,
    )


def test_complete_frozen_inputs_exactly_replay_without_stored_score_input() -> None:
    record = _record()

    result = classify_legacy_rosh_record(record)

    assert result.classification == "exact_legacy_replayable"
    assert result.required_inputs_complete is True
    assert result.independent_replay_succeeded is True
    assert result.recomputed_score == pytest.approx(record.stored_score)
    assert result.absolute_difference == pytest.approx(0.0)


def test_derived_minute_table_without_raw_statistics_is_only_partial() -> None:
    record = _record()
    evidence = dict(record.evidence)
    evidence.pop("legacy_formula_inputs")
    record = replace(record, evidence=evidence, evidence_hash=_hash(evidence))

    result = classify_legacy_rosh_record(record)

    assert result.classification == "partially_replayable"
    assert result.minute_table_complete is True
    assert result.required_inputs_complete is False
    assert result.recomputed_score is None
    assert result.missing_reason == "raw_formula_inputs_unavailable"


def test_final_score_without_replay_inputs_is_score_only() -> None:
    record = _record()
    evidence = dict(record.evidence)
    evidence.pop("legacy_formula_inputs")
    evidence.pop("pure_minute_table")
    evidence.pop("response_hashes")
    record = replace(record, evidence=evidence, evidence_hash=_hash(evidence))

    result = classify_legacy_rosh_record(record)

    assert result.classification == "score_only"
    assert result.recomputed_score is None


def test_cutoff_unsafe_overrides_successful_replay() -> None:
    result = classify_legacy_rosh_record(_record(cutoff_safe=False))

    assert result.independent_replay_succeeded is True
    assert result.classification == "cutoff_unsafe"
    assert "source_as_of_after_prediction_cutoff" in str(result.unsafe_reason)
    assert "source_week_after_prediction_cutoff" in str(result.unsafe_reason)


def test_invalid_evidence_hash_cannot_replay() -> None:
    result = classify_legacy_rosh_record(
        replace(_record(), evidence_hash="0" * 64)
    )

    assert result.evidence_hash_valid is False
    assert result.independent_replay_succeeded is False
    assert result.classification == "score_only"
    assert result.missing_reason == "evidence_hash_invalid"


def test_replay_is_offline_deterministic_and_independent_of_database_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()

    def blocked_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked_network)
    external_database_state = {
        "target_result": {"radiant_win": True},
        "postmatch_hero_stats": {1: {"wins": 999}},
        "future_matches": [],
        "current_official_profile": {"version": "official-v2"},
    }
    first = recompute_legacy_pure_score(
        record.evidence,
        formula_version=record.formula_version,
        radiant_expected=record.radiant_expected,
        dire_expected=record.dire_expected,
    )
    del external_database_state["target_result"]
    external_database_state["postmatch_hero_stats"] = {}
    external_database_state["future_matches"].append({"match_id": 999999})
    external_database_state["current_official_profile"] = {"version": "changed"}
    second = recompute_legacy_pure_score(
        record.evidence,
        formula_version=record.formula_version,
        radiant_expected=record.radiant_expected,
        dire_expected=record.dire_expected,
    )

    assert first == second


def test_audit_module_has_no_official_profile_or_network_client_dependency() -> None:
    source = (
        ROOT / "event_intelligence" / "legacy_rosh_reconstruction.py"
    ).read_text(encoding="utf-8")

    assert "stratz_official_profile" not in source
    assert "stratz_rosh_client" not in source
    assert "requests" not in source
    assert "httpx" not in source
