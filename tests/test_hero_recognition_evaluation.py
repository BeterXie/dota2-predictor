from __future__ import annotations

import sys
from typing import Any

import pytest

from scripts.evaluate_hero_recognition import (
    _validate_exact_mapping,
    _validate_single_map_clocks,
    main,
)


def test_exact_mapping_accepts_reversed_radiant_team_order() -> None:
    mapping = _validate_exact_mapping(
        raybet_match_id="42",
        map_number=2,
        opendota_match_id=1234,
        mapping_source="manual_exact",
        raybet_team_one_name="Alpha",
        raybet_team_two_name="Bravo",
        team_one_id=100,
        team_two_id=200,
        radiant_team_id=200,
        dire_team_id=100,
    )

    assert mapping.report_context(10) == {
        "raybet_match_id": "42",
        "raybet_map_number": 2,
        "opendota_match_id": 1234,
        "mapping_source": "manual_exact",
        "mapping_id": None,
        "raybet_team_one_name": "Alpha",
        "raybet_team_two_name": "Bravo",
        "team_one_id": 100,
        "team_two_id": 200,
        "opendota_radiant_team_id": 200,
        "opendota_dire_team_id": 100,
        "truth_hero_count": 10,
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"map_number": 0},
        {"mapping_source": "time_nearest"},
        {"raybet_team_one_name": ""},
        {"team_two_id": 100},
        {"radiant_team_id": 300},
    ],
)
def test_exact_mapping_rejects_incomplete_or_mismatched_identity(
    updates: dict[str, Any],
) -> None:
    values: dict[str, Any] = {
        "raybet_match_id": "42",
        "map_number": 1,
        "opendota_match_id": 1234,
        "mapping_source": "manual_exact",
        "raybet_team_one_name": "Alpha",
        "raybet_team_two_name": "Bravo",
        "team_one_id": 100,
        "team_two_id": 200,
        "radiant_team_id": 100,
        "dire_team_id": 200,
    }
    values.update(updates)

    with pytest.raises(ValueError):
        _validate_exact_mapping(**values)


def test_exact_mapping_rejects_cross_map_clock_reset() -> None:
    with pytest.raises(ValueError, match="game clock reset"):
        _validate_single_map_clocks([60, 600, None, 1_800, 120])

    _validate_single_map_clocks([None, 60, 600, None, 1_800])


def test_database_mode_requires_explicit_mapping_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_hero_recognition.py",
            "--raybet-match-id",
            "42",
            "--map-number",
            "1",
            "--opendota-match-id",
            "1234",
        ],
    )

    with pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 2
