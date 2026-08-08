from __future__ import annotations

import pytest

from web.match_identity import (
    match_display_name,
    observation_file_metadata,
    observation_file_name,
)


def test_observation_filename_uses_raybet_table_primary_key() -> None:
    assert observation_file_name("38417147") == "38417147.jsonl"


@pytest.mark.parametrize("value", ("", "../42", "match\\42"))
def test_observation_filename_rejects_unsafe_keys(value: str) -> None:
    with pytest.raises(ValueError, match="raybet_match_id"):
        observation_file_name(value)


def test_match_display_name_prefers_official_identity() -> None:
    assert match_display_name(
        raybet_match_id="38417147",
        official_match_id="8123456789",
        team_one="Team A",
        team_two="Team B",
        tournament="The International 2026",
    ) == "官方 Match ID 8123456789 · Team A vs Team B · The International 2026"


def test_match_display_name_falls_back_to_raybet_identity() -> None:
    assert match_display_name(
        raybet_match_id="38417147",
        official_match_id=None,
        team_one="Team A",
        team_two="Team B",
        tournament=None,
    ) == "RayBet 38417147 · Team A vs Team B"


class _IdentityResult:
    def fetchone(self) -> dict[str, object]:
        return {
            "raybet_match_id": "38417147",
            "official_match_id": "8123456789",
            "team_one": "Team A",
            "team_two": "Team B",
            "tournament": "The International 2026",
        }


class _IdentityConnection:
    def execute(self, query: str, params: tuple[object, ...]) -> _IdentityResult:
        assert "FROM raybet_matches AS match_row" in query
        assert params == ("38417147",)
        return _IdentityResult()


def test_observation_metadata_resolves_stable_filename() -> None:
    metadata = observation_file_metadata(  # type: ignore[arg-type]
        _IdentityConnection(),
        "38417147.jsonl",
    )

    assert metadata == {
        "raybet_match_id": "38417147",
        "official_match_id": "8123456789",
        "display_name": (
            "官方 Match ID 8123456789 · Team A vs Team B · "
            "The International 2026"
        ),
    }
