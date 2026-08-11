from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live_betting import official_map_identity
from live_betting.official_map_identity import (
    _normalize_team,
    _normalize_tournament,
    _unique_map_assignment,
)
from live_betting.raybet_state import explicit_raybet_map_times


def _payload(*, third_status: int = 0, third_time: str = "") -> dict[str, object]:
    data = {
        "1": {"status": 2, "cmDate": "2026-08-10 20:02:42"},
        "2": {"status": 1, "cmDate": "2026-08-10 21:49:38"},
        "3": {"status": third_status, "cmDate": third_time},
    }
    return {
        "team": [
            {
                "pos": 1,
                "team_name": "Ilbirs eSports",
                "score": {"manualControlData": {"data": data}},
            },
            {
                "pos": 2,
                "team_name": "Zero Tenacity",
                "score": {"manualControlData": {"data": data}},
            },
        ]
    }


def test_explicit_map_times_exclude_unplayed_third_map() -> None:
    times = explicit_raybet_map_times(_payload(), 3)

    assert tuple(times) == (1, 2)
    assert times[1] == datetime(2026, 8, 10, 12, 2, 42, tzinfo=timezone.utc)
    assert times[2] == datetime(2026, 8, 10, 13, 49, 38, tzinfo=timezone.utc)


def test_conflicting_team_map_times_fail_closed() -> None:
    payload = _payload()
    teams = payload["team"]
    assert isinstance(teams, list)
    second = teams[1]
    assert isinstance(second, dict)
    second["score"] = {
        "manualControlData": {
            "data": {
                "1": {"status": 2, "cmDate": "2026-08-10 20:03:42"},
                "2": {"status": 1, "cmDate": "2026-08-10 21:49:38"},
                "3": {"status": 0, "cmDate": ""},
            }
        }
    }

    assert explicit_raybet_map_times(payload, 3) == {}


def test_unique_assignment_uses_explicit_map_times_not_candidate_order() -> None:
    first = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    second = first + timedelta(hours=1, minutes=20)
    candidates = [
        {"dota_match_id": 202, "start_time": int(second.timestamp()) + 30},
        {"dota_match_id": 201, "start_time": int(first.timestamp()) - 45},
    ]

    assignment = _unique_map_assignment({1: first, 2: second}, candidates)

    assert assignment is not None
    assert assignment[1]["dota_match_id"] == 201
    assert assignment[2]["dota_match_id"] == 202


def test_team_and_tournament_normalization_is_exact_after_known_decoration() -> None:
    assert _normalize_team("_PowerRangers") == _normalize_team("Power Rangers")
    assert _normalize_team("Level UP esports") == _normalize_team("Level Up")
    assert _normalize_team("Na`Vi") == _normalize_team("Natus Vincere")
    assert _normalize_tournament("EPL Masters 2026") == _normalize_tournament(
        "EPL Masters"
    )


class _Result:
    def __init__(self, *, one=None, rows=()) -> None:
        self._one = one
        self._rows = list(rows)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, series: dict[str, object], candidates: list[dict[str, object]]) -> None:
        self.series = series
        self.candidates = candidates

    def execute(self, query: str, _params=()):
        if "FROM raybet_matches" in query:
            return _Result(one=self.series)
        if "FROM matches AS match" in query:
            return _Result(rows=self.candidates)
        raise AssertionError(query)


def test_exact_resolver_requires_registered_details_and_unique_map_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()
    payload.update({"tournament_short_name": "EPL Masters"})
    starts = {
        301: datetime(2026, 8, 10, 12, 10, tzinfo=timezone.utc),
        302: datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc),
    }
    candidates = []
    artifact_paths: dict[str, Path] = {}
    for match_id, start in starts.items():
        artifact_id = f"artifact-{match_id}"
        candidate = {
            "dota_match_id": match_id,
            "series_id": 9001,
            "start_time": int(start.timestamp()),
            "leagueid": 77,
            "radiant_team_id": 11,
            "dire_team_id": 22,
            "radiant_win": True,
            "radiant_team_name": "Ilbirs Esports",
            "dire_team_name": "Zero Tenacity",
            "league_name": "EPL Masters 2026",
            "artifact_id": artifact_id,
        }
        candidates.append(candidate)
        artifact = tmp_path / f"{match_id}.json.gz"
        artifact.write_bytes(
            gzip.compress(
                json.dumps(
                    {
                        "match_id": match_id,
                        "series_id": 9001,
                        "start_time": int(start.timestamp()),
                        "leagueid": 77,
                        "radiant_team_id": 11,
                        "dire_team_id": 22,
                    }
                ).encode("utf-8")
            )
        )
        artifact_paths[artifact_id] = artifact
    connection = _Connection(
        {
            "team_one": "Ilbirs eSports",
            "team_two": "Zero Tenacity",
            "tournament": "EPL Masters",
            "best_of": 3,
            "status": "3",
            "raw_json": json.dumps(payload),
        },
        list(reversed(candidates)),
    )
    monkeypatch.setattr(
        official_map_identity,
        "verify_registered_raw_source_artifact",
        lambda _connection, artifact_id: artifact_paths[artifact_id],
    )

    resolution = official_map_identity.resolve_exact_official_map_links(
        connection,  # type: ignore[arg-type]
        "series-1",
    )

    assert resolution.status == "confirmed"
    assert [link.dota_match_id for link in resolution.links] == [301, 302]
    assert [link.map_number for link in resolution.links] == [1, 2]
    assert resolution.links[0].evidence()["team_name_evidence"] == [
        {
            "raybet_name": "Ilbirs eSports",
            "official_name": "Ilbirs Esports",
            "method": "normalized_exact",
            "source_url": None,
        },
        {
            "raybet_name": "Zero Tenacity",
            "official_name": "Zero Tenacity",
            "method": "normalized_exact",
            "source_url": None,
        },
    ]


def test_sourced_navi_alias_is_visible_in_map_identity_evidence() -> None:
    evidence = official_map_identity._team_name_crosswalk_evidence(
        ("Rune Eaters", "Na`Vi"),
        ("Natus Vincere", "Rune Eaters"),
    )

    assert evidence[1] == {
        "raybet_name": "Na`Vi",
        "official_name": "Natus Vincere",
        "method": "sourced_alias",
        "source_url": "https://liquipedia.net/dota2/Natus_Vincere",
    }
