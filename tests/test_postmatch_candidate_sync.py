from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import live_betting.postmatch_monitor as postmatch
from live_betting.official_map_identity import OfficialMapResolution
from live_betting.postmatch_monitor import (
    _candidate_summary_ids,
    _detail_matches_candidate_series,
    sync_exact_postmatch_candidate,
)


UTC = timezone.utc
MAP_TIMES = {
    1: datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    2: datetime(2026, 8, 10, 13, 20, tzinfo=UTC),
}


def _summary(match_id: int, start_time: int, *, league_id: int = 19944) -> dict:
    return {
        "match_id": match_id,
        "start_time": start_time,
        "series_id": 9001,
        "leagueid": league_id,
    }


def _detail(
    match_id: int,
    start_time: int,
    *,
    radiant: str = "Level UP",
    dire: str = "Natus Vincere",
) -> dict:
    return {
        "match_id": match_id,
        "start_time": start_time,
        "series_id": 9001,
        "leagueid": 19944,
        "duration": 2400,
        "radiant_win": True,
        "radiant_team_id": 11,
        "dire_team_id": 22,
        "radiant_team": {"team_id": 11, "name": radiant},
        "dire_team": {"team_id": 22, "name": dire},
        "league": {"leagueid": 19944, "name": "EPL Masters 2026"},
        "players": [],
    }


def test_candidate_summaries_require_exact_league_series_and_map_window() -> None:
    first = int(MAP_TIMES[1].timestamp())
    second = int(MAP_TIMES[2].timestamp())
    summaries = [
        _summary(101, first + 60),
        _summary(102, first + 60, league_id=77),
        _summary(103, second + 3600),
        {**_summary(104, first + 60), "series_id": None},
    ]

    assert _candidate_summary_ids(
        summaries,
        league_id=19944,
        map_times=MAP_TIMES,
    ) == (101,)


def test_candidate_detail_requires_exact_teams_and_official_identity() -> None:
    first = int(MAP_TIMES[1].timestamp())
    expected = frozenset({"levelup", "natusvincere"})

    assert _detail_matches_candidate_series(
        _detail(101, first + 60),
        match_id=101,
        league_id=19944,
        map_times=MAP_TIMES,
        team_names=expected,
    )
    assert not _detail_matches_candidate_series(
        _detail(101, first + 60, dire="MOUZ"),
        match_id=101,
        league_id=19944,
        map_times=MAP_TIMES,
        team_names=expected,
    )


class _Result:
    def __init__(self, *, one=None, rows=()) -> None:
        self.one = one
        self.rows = list(rows)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def execute(self, query: str, _params=()):
        if "FROM raybet_matches" in query:
            return _Result(
                one={
                    "team_one": "Level Up",
                    "team_two": "Natus Vincere",
                    "tournament": "EPL大师赛",
                    "best_of": 3,
                    "status": "3",
                    "raw_json": json.dumps(self.payload),
                }
            )
        if "FROM event_candidates" in query:
            return _Result(
                rows=[
                    {
                        "provider_event_id": "19944",
                        "canonical_name": "EPL Masters 2026",
                        "league_name": "EPL Masters 2026",
                    }
                ]
            )
        raise AssertionError(query)


def test_postmatch_candidate_sync_archives_only_exact_series_details(
    monkeypatch,
) -> None:
    local_first = "2026-08-10 20:00:00"
    local_second = "2026-08-10 21:20:00"
    map_data = {
        "1": {"status": 2, "cmDate": local_first},
        "2": {"status": 1, "cmDate": local_second},
        "3": {"status": 0, "cmDate": ""},
    }
    payload = {
        "tournament_short_name": "EPL Masters",
        "team": [
            {
                "pos": 1,
                "team_name": "Level Up",
                "score": {"manualControlData": {"data": map_data}},
            },
            {
                "pos": 2,
                "team_name": "Natus Vincere",
                "score": {"manualControlData": {"data": map_data}},
            },
        ],
    }
    first = int(MAP_TIMES[1].timestamp())
    second = int(MAP_TIMES[2].timestamp())
    summaries = [
        _summary(101, first + 60),
        _summary(102, second + 60),
        _summary(103, first + 90),
    ]

    class Client:
        async def get_league_matches(self, league_id: int):
            assert league_id == 19944
            return summaries

        async def get_match(self, match_id: int):
            if match_id == 103:
                return _detail(match_id, first + 90, dire="MOUZ")
            return _detail(match_id, first + 60 if match_id == 101 else second + 60)

    class Archive:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def archive_json(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace()

    class CoreStore:
        def __init__(self) -> None:
            self.matches: list[int] = []

        def insert_match(self, detail: dict) -> None:
            self.matches.append(int(detail["match_id"]))

    resolutions = iter(
        [
            OfficialMapResolution("unlinked", "exact_official_series_not_found", (1, 2)),
            OfficialMapResolution("confirmed", "raybet_explicit_map_time_unique", (1, 2)),
        ]
    )
    monkeypatch.setattr(
        postmatch,
        "resolve_exact_official_map_links",
        lambda *_args: next(resolutions),
    )
    monkeypatch.setattr(
        postmatch,
        "_invalidate_vision_observations_outside_official_maps",
        lambda *_args: {
            "vision_observation_invalidations": 0,
            "vision_observation_deconfirmed": 0,
            "vision_invalidated_map_numbers": [],
        },
    )
    monkeypatch.setattr(
        postmatch,
        "persist_verified_official_map_results",
        lambda *_args: {
            "status": "confirmed",
            "reason": "verified_registered_opendota_result",
            "inserted": 2,
            "unchanged": 0,
            "map_numbers": [1, 2],
        },
    )
    archive = Archive()
    core = CoreStore()

    result = asyncio.run(
        sync_exact_postmatch_candidate(
            SimpleNamespace(connection=_Connection(payload)),
            Client(),
            archive,
            core,
            "series-one",
        )
    )

    assert result == {
        "status": "confirmed",
        "reason": "raybet_explicit_map_time_unique",
        "raybet_match_id": "series-one",
        "details_synced": 2,
        "attempted": True,
        "official_result_evidence": {
            "status": "confirmed",
            "reason": "verified_registered_opendota_result",
            "inserted": 2,
            "unchanged": 0,
            "map_numbers": [1, 2],
        },
        "vision_observation_invalidations": 0,
        "vision_observation_deconfirmed": 0,
        "vision_invalidated_map_numbers": [],
    }
    assert core.matches == [101, 102]
    assert [call["endpoint"] for call in archive.calls] == [
        "/api/leagues/19944/matches",
        "/api/matches/101",
        "/api/matches/102",
    ]
