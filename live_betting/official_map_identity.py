"""Resolve exact Official Match IDs from explicit per-Map provider evidence."""

from __future__ import annotations

import gzip
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from database.session import PostgresSession
from event_intelligence.raw_registry import verify_registered_raw_source_artifact

from .raybet import parse_raybet_map_final
from .raybet_state import explicit_raybet_map_times


MAX_MAP_START_DELTA_SECONDS = 45 * 60
_TEAM_NAME_ALIASES = {
    "navi": {
        "canonical": "natusvincere",
        "source_url": "https://liquipedia.net/dota2/Natus_Vincere",
    },
}
_ENDED_STATUSES = frozenset(
    {"3", "4", "5", "closed", "completed", "ended", "finished", "settled"}
)


@dataclass(frozen=True)
class ExactOfficialMapLink:
    map_number: int
    dota_match_id: int
    official_series_id: int
    league_id: int
    raybet_map_time: datetime
    official_start_time: datetime
    delta_seconds: int
    team_name_evidence: tuple[dict[str, object], ...] = ()

    def evidence(self) -> dict[str, object]:
        return {
            "method": "raybet_explicit_map_time_unique",
            "raybet_source": (
                f"manualControlData.data[{self.map_number}].cmDate"
            ),
            "raybet_map_time": self.raybet_map_time.isoformat(),
            "official_source": "registered_opendota_match",
            "official_start_time": self.official_start_time.isoformat(),
            "delta_seconds": self.delta_seconds,
            "maximum_delta_seconds": MAX_MAP_START_DELTA_SECONDS,
            "official_series_id": self.official_series_id,
            "league_id": self.league_id,
            "team_name_evidence": [
                dict(item) for item in self.team_name_evidence
            ],
        }


@dataclass(frozen=True)
class OfficialMapResolution:
    status: str
    reason: str
    map_numbers: tuple[int, ...]
    links: tuple[ExactOfficialMapLink, ...] = ()


@dataclass(frozen=True)
class VerifiedOfficialMapResult:
    map_number: int
    dota_match_id: int
    official_series_id: int
    league_id: int
    winner_side: str
    team_one_kills: int | None
    team_two_kills: int | None
    duration_seconds: int
    observed_at: datetime
    first_usable_at: datetime
    artifact_id: str
    observation_id: str
    content_hash: str
    missing_fields: tuple[str, ...] = ()

    @property
    def evidence_ref(self) -> str:
        return f"opendota:{self.dota_match_id}:sha256:{self.content_hash}"

    def facts(self, raybet_match_id: str) -> dict[str, object]:
        return {
            "raybet_match_id": raybet_match_id,
            "map_number": self.map_number,
            "dota_match_id": self.dota_match_id,
            "official_series_id": self.official_series_id,
            "league_id": self.league_id,
            "winner_side": self.winner_side,
            "team_one_kills": self.team_one_kills,
            "team_two_kills": self.team_two_kills,
            "duration_seconds": self.duration_seconds,
            "missing_fields": list(self.missing_fields),
            "identity_method": "raybet_explicit_map_time_unique",
            "result_source": "registered_opendota_match",
        }


@dataclass(frozen=True)
class OfficialMapResultResolution:
    status: str
    reason: str
    map_numbers: tuple[int, ...]
    results: tuple[VerifiedOfficialMapResult, ...] = ()


def resolve_exact_official_map_links(
    connection: PostgresSession,
    raybet_match_id: str,
) -> OfficialMapResolution:
    row = connection.execute(
        """SELECT team_one, team_two, tournament, best_of, status, raw_json
             FROM raybet_matches WHERE raybet_match_id=?""",
        (raybet_match_id,),
    ).fetchone()
    if row is None:
        return OfficialMapResolution("unlinked", "raybet_series_not_found", ())
    status = str(row["status"] or "").strip().casefold()
    if status not in _ENDED_STATUSES:
        return OfficialMapResolution("unlinked", "raybet_series_not_ended", ())
    try:
        best_of = int(row["best_of"])
        payload = json.loads(str(row["raw_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return OfficialMapResolution("unlinked", "raybet_map_time_invalid", ())
    if not isinstance(payload, dict) or not 1 <= best_of <= 5:
        return OfficialMapResolution("unlinked", "raybet_map_time_invalid", ())

    team_one = _normalize_team(row["team_one"])
    team_two = _normalize_team(row["team_two"])
    tournament = _normalize_tournament(
        payload.get("tournament_short_name") or row["tournament"]
    )
    if not team_one or not team_two or team_one == team_two or not tournament:
        return OfficialMapResolution("unlinked", "raybet_series_identity_invalid", ())
    if _payload_team_names(payload) != {team_one, team_two}:
        return OfficialMapResolution("unlinked", "raybet_team_identity_conflict", ())

    map_times = explicit_raybet_map_times(payload, best_of)
    map_numbers = tuple(sorted(map_times))
    if not map_numbers:
        return OfficialMapResolution(
            "unlinked", "raybet_explicit_map_time_unavailable", ()
        )

    first = min(map_times.values()) - timedelta(
        seconds=MAX_MAP_START_DELTA_SECONDS
    )
    last = max(map_times.values()) + timedelta(
        seconds=MAX_MAP_START_DELTA_SECONDS
    )
    rows = connection.execute(
        """SELECT match.match_id AS dota_match_id, match.series_id,
                  match.start_time, match.leagueid, match.radiant_team_id,
                  match.dire_team_id, match.radiant_win,
                  radiant.name AS radiant_team_name,
                  dire.name AS dire_team_name, league.name AS league_name,
                  artifact.artifact_id
             FROM matches AS match
             JOIN teams AS radiant ON radiant.team_id=match.radiant_team_id
             JOIN teams AS dire ON dire.team_id=match.dire_team_id
             JOIN leagues AS league ON league.leagueid=match.leagueid
             JOIN LATERAL (
                  SELECT source.artifact_id
                    FROM raw_source_artifacts AS source
                   WHERE source.source='opendota'
                     AND source.artifact_use='primary'
                     AND source.endpoint='/api/matches/' || match.match_id
                     AND source.match_id=match.match_id
                   ORDER BY source.received_at DESC, source.artifact_id DESC
                   LIMIT 1
             ) AS artifact ON TRUE
            WHERE match.start_time BETWEEN ? AND ?
              AND match.series_id IS NOT NULL AND match.series_id>0
              AND match.leagueid IS NOT NULL AND match.leagueid>0
              AND match.radiant_team_id IS NOT NULL
              AND match.dire_team_id IS NOT NULL
              AND match.radiant_win IS NOT NULL
            ORDER BY match.series_id, match.start_time, match.match_id""",
        (int(first.timestamp()), int(last.timestamp())),
    ).fetchall()

    grouped: dict[tuple[int, int, frozenset[int]], list[dict[str, Any]]] = {}
    for candidate_row in rows:
        candidate = dict(candidate_row)
        radiant_name = _normalize_team(candidate["radiant_team_name"])
        dire_name = _normalize_team(candidate["dire_team_name"])
        if (
            {radiant_name, dire_name} != {team_one, team_two}
            or _normalize_tournament(candidate["league_name"]) != tournament
        ):
            continue
        team_ids = frozenset(
            {
                int(candidate["radiant_team_id"]),
                int(candidate["dire_team_id"]),
            }
        )
        if len(team_ids) != 2:
            continue
        key = (int(candidate["series_id"]), int(candidate["leagueid"]), team_ids)
        grouped.setdefault(key, []).append(candidate)

    valid: list[tuple[tuple[int, int, frozenset[int]], dict[int, dict[str, Any]]]] = []
    for key, candidates in grouped.items():
        assignment = _unique_map_assignment(map_times, candidates)
        if assignment is None:
            continue
        if not _raybet_winners_agree(
            payload,
            assignment,
            team_one=team_one,
            team_two=team_two,
        ):
            continue
        if not all(
            _verified_candidate_payload(connection, candidate)
            for candidate in assignment.values()
        ):
            continue
        valid.append((key, assignment))

    if not valid:
        return OfficialMapResolution(
            "unlinked", "exact_official_series_not_found", map_numbers
        )
    if len(valid) != 1:
        return OfficialMapResolution(
            "unlinked", "exact_official_series_ambiguous", map_numbers
        )

    (series_id, league_id, _team_ids), assignment = valid[0]
    first_candidate = next(iter(assignment.values()))
    team_name_evidence = _team_name_crosswalk_evidence(
        (str(row["team_one"]), str(row["team_two"])),
        (
            str(first_candidate["radiant_team_name"]),
            str(first_candidate["dire_team_name"]),
        ),
    )
    if len(team_name_evidence) != 2:
        return OfficialMapResolution(
            "unlinked", "exact_team_name_evidence_unavailable", map_numbers
        )
    links = tuple(
        ExactOfficialMapLink(
            map_number=map_number,
            dota_match_id=int(assignment[map_number]["dota_match_id"]),
            official_series_id=series_id,
            league_id=league_id,
            raybet_map_time=map_times[map_number],
            official_start_time=datetime.fromtimestamp(
                int(assignment[map_number]["start_time"]), timezone.utc
            ),
            delta_seconds=abs(
                int(assignment[map_number]["start_time"])
                - int(map_times[map_number].timestamp())
            ),
            team_name_evidence=team_name_evidence,
        )
        for map_number in map_numbers
    )
    return OfficialMapResolution(
        "confirmed",
        "raybet_explicit_map_time_unique",
        map_numbers,
        links,
    )


def resolve_verified_official_map_results(
    connection: PostgresSession,
    raybet_match_id: str,
) -> OfficialMapResultResolution:
    """Resolve result facts only after exact Map identity and raw bytes verify."""

    resolution = resolve_exact_official_map_links(connection, raybet_match_id)
    if resolution.status != "confirmed":
        return OfficialMapResultResolution(
            resolution.status,
            resolution.reason,
            resolution.map_numbers,
        )
    series = connection.execute(
        """SELECT team_one, team_two FROM raybet_matches
            WHERE raybet_match_id=?""",
        (raybet_match_id,),
    ).fetchone()
    if series is None:
        return OfficialMapResultResolution(
            "unlinked", "raybet_series_not_found", resolution.map_numbers
        )
    team_one = _normalize_team(series["team_one"])
    team_two = _normalize_team(series["team_two"])
    if not team_one or not team_two or team_one == team_two:
        return OfficialMapResultResolution(
            "unlinked", "raybet_series_identity_invalid", resolution.map_numbers
        )

    results: list[VerifiedOfficialMapResult] = []
    for link in resolution.links:
        result = _verified_official_result(
            connection,
            link,
            team_one=team_one,
            team_two=team_two,
        )
        if result is None:
            return OfficialMapResultResolution(
                "unlinked",
                f"verified_official_map_result_unavailable:map_{link.map_number}",
                resolution.map_numbers,
            )
        results.append(result)
    if (
        tuple(result.map_number for result in results) != resolution.map_numbers
        or len({result.dota_match_id for result in results}) != len(results)
    ):
        return OfficialMapResultResolution(
            "unlinked",
            "verified_official_map_result_identity_conflict",
            resolution.map_numbers,
        )
    return OfficialMapResultResolution(
        "confirmed",
        "verified_registered_opendota_result",
        resolution.map_numbers,
        tuple(results),
    )


def _verified_official_result(
    connection: PostgresSession,
    link: ExactOfficialMapLink,
    *,
    team_one: str,
    team_two: str,
) -> VerifiedOfficialMapResult | None:
    row = connection.execute(
        """SELECT match.match_id, match.series_id, match.leagueid,
                  match.start_time, match.duration, match.radiant_win,
                  match.radiant_team_id, match.dire_team_id,
                  match.radiant_score, match.dire_score,
                  radiant.name AS radiant_team_name,
                  dire.name AS dire_team_name,
                  artifact.artifact_id, artifact.content_hash,
                  observation.observation_id, observation.received_at,
                  observation.first_usable_at
             FROM matches AS match
             JOIN teams AS radiant ON radiant.team_id=match.radiant_team_id
             JOIN teams AS dire ON dire.team_id=match.dire_team_id
             JOIN LATERAL (
                  SELECT source.artifact_id, source.content_hash,
                         source.first_usable_at
                    FROM raw_source_artifacts AS source
                   WHERE source.source='opendota'
                     AND source.artifact_use='primary'
                     AND source.endpoint='/api/matches/' || match.match_id
                     AND source.match_id=match.match_id
                     AND source.first_usable_at IS NOT NULL
                   ORDER BY source.received_at DESC, source.artifact_id DESC
                   LIMIT 1
             ) AS artifact ON TRUE
             JOIN LATERAL (
                  SELECT source.observation_id, source.received_at,
                         source.first_usable_at
                    FROM raw_source_observations AS source
                   WHERE source.artifact_id=artifact.artifact_id
                     AND source.source='opendota'
                     AND source.artifact_use='primary'
                     AND source.endpoint='/api/matches/' || match.match_id
                     AND source.match_id=match.match_id
                     AND source.content_hash=artifact.content_hash
                     AND source.first_usable_at=artifact.first_usable_at
                   ORDER BY source.received_at, source.observation_id
                   LIMIT 1
             ) AS observation ON TRUE
            WHERE match.match_id=?""",
        (link.dota_match_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        artifact_id = str(row["artifact_id"])
        content_hash = str(row["content_hash"])
        path = verify_registered_raw_source_artifact(connection, artifact_id)
        payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        observed_at = _parse_utc(row["received_at"])
        first_usable_at = _parse_utc(row["first_usable_at"])
    except (
        OSError,
        EOFError,
        UnicodeError,
        TypeError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ):
        return None
    expected_start = int(link.official_start_time.timestamp())
    required = {
        "match_id": link.dota_match_id,
        "series_id": link.official_series_id,
        "leagueid": link.league_id,
        "start_time": expected_start,
        "duration": row["duration"],
        "radiant_win": row["radiant_win"],
        "radiant_team_id": row["radiant_team_id"],
        "dire_team_id": row["dire_team_id"],
    }
    if (
        not isinstance(payload, dict)
        or any(payload.get(field) != value for field, value in required.items())
        or row["match_id"] != link.dota_match_id
        or row["series_id"] != link.official_series_id
        or row["leagueid"] != link.league_id
        or row["start_time"] != expected_start
        or type(row["duration"]) is not int
        or int(row["duration"]) <= 0
        or type(row["radiant_win"]) is not bool
        or first_usable_at < observed_at
        or len(content_hash) != 64
        or not str(row["observation_id"] or "").strip()
    ):
        return None
    radiant = _normalize_team(row["radiant_team_name"])
    dire = _normalize_team(row["dire_team_name"])
    if {radiant, dire} != {team_one, team_two}:
        return None
    radiant_side = "team_one" if radiant == team_one else "team_two"
    winner_side = (
        radiant_side
        if bool(row["radiant_win"])
        else ("team_two" if radiant_side == "team_one" else "team_one")
    )
    radiant_score = _optional_nonnegative_integer(row["radiant_score"])
    dire_score = _optional_nonnegative_integer(row["dire_score"])
    missing_fields = tuple(
        field
        for field, value in (
            ("team_one_kills", radiant_score if radiant_side == "team_one" else dire_score),
            ("team_two_kills", dire_score if radiant_side == "team_one" else radiant_score),
        )
        if value is None
    )
    return VerifiedOfficialMapResult(
        map_number=link.map_number,
        dota_match_id=link.dota_match_id,
        official_series_id=link.official_series_id,
        league_id=link.league_id,
        winner_side=winner_side,
        team_one_kills=(radiant_score if radiant_side == "team_one" else dire_score),
        team_two_kills=(dire_score if radiant_side == "team_one" else radiant_score),
        duration_seconds=int(row["duration"]),
        observed_at=observed_at,
        first_usable_at=first_usable_at,
        artifact_id=artifact_id,
        observation_id=str(row["observation_id"]),
        content_hash=content_hash,
        missing_fields=missing_fields,
    )


def _parse_utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include an offset")
    return parsed.astimezone(timezone.utc)


def _optional_nonnegative_integer(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _unique_map_assignment(
    map_times: Mapping[int, datetime],
    candidates: list[dict[str, Any]],
) -> dict[int, dict[str, Any]] | None:
    if len(candidates) != len(map_times):
        return None
    assignment: dict[int, dict[str, Any]] = {}
    for map_number, map_time in map_times.items():
        eligible = [
            candidate
            for candidate in candidates
            if abs(
                int(candidate["start_time"]) - int(map_time.timestamp())
            )
            <= MAX_MAP_START_DELTA_SECONDS
        ]
        if len(eligible) != 1:
            return None
        assignment[map_number] = eligible[0]
    if len({int(row["dota_match_id"]) for row in assignment.values()}) != len(map_times):
        return None
    return assignment


def _raybet_winners_agree(
    payload: Mapping[str, Any],
    assignment: Mapping[int, Mapping[str, Any]],
    *,
    team_one: str,
    team_two: str,
) -> bool:
    for map_number, candidate in assignment.items():
        try:
            final = parse_raybet_map_final(dict(payload), map_number)
        except (TypeError, ValueError):
            continue
        if final.status != "confirmed" or final.winner_side is None:
            continue
        radiant = _normalize_team(candidate["radiant_team_name"])
        dire = _normalize_team(candidate["dire_team_name"])
        if {radiant, dire} != {team_one, team_two}:
            return False
        radiant_won = bool(candidate["radiant_win"])
        official_winner = radiant if radiant_won else dire
        expected_winner = team_one if final.winner_side == "team_one" else team_two
        if official_winner != expected_winner:
            return False
    return True


def _verified_candidate_payload(
    connection: PostgresSession,
    candidate: Mapping[str, Any],
) -> bool:
    try:
        path = verify_registered_raw_source_artifact(
            connection,
            str(candidate["artifact_id"]),
        )
        payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RuntimeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    expected = {
        "match_id": int(candidate["dota_match_id"]),
        "series_id": int(candidate["series_id"]),
        "start_time": int(candidate["start_time"]),
        "leagueid": int(candidate["leagueid"]),
        "radiant_team_id": int(candidate["radiant_team_id"]),
        "dire_team_id": int(candidate["dire_team_id"]),
    }
    return all(payload.get(field) == value for field, value in expected.items())


def _payload_team_names(payload: Mapping[str, Any]) -> set[str]:
    teams = payload.get("team")
    if not isinstance(teams, list) or len(teams) != 2:
        return set()
    names = {
        _normalize_team(team.get("team_name"))
        for team in teams
        if isinstance(team, Mapping)
    }
    return names if len(names) == 2 and all(names) else set()


def _normalize_team(value: object) -> str:
    normalized = _base_normalize_team(value)
    alias = _TEAM_NAME_ALIASES.get(normalized)
    return str(alias["canonical"]) if alias is not None else normalized


def _base_normalize_team(value: object) -> str:
    normalized = _letters_and_numbers(value)
    return normalized[: -len("esports")] if normalized.endswith("esports") else normalized


def _team_name_crosswalk_evidence(
    raybet_names: tuple[str, str],
    official_names: tuple[str, str],
) -> tuple[dict[str, object], ...]:
    output: list[dict[str, object]] = []
    for raybet_name in raybet_names:
        canonical = _normalize_team(raybet_name)
        matches = [
            official_name
            for official_name in official_names
            if _normalize_team(official_name) == canonical
        ]
        if len(matches) != 1:
            return ()
        official_name = matches[0]
        raybet_alias = _TEAM_NAME_ALIASES.get(_base_normalize_team(raybet_name))
        official_alias = _TEAM_NAME_ALIASES.get(_base_normalize_team(official_name))
        alias = raybet_alias or official_alias
        output.append(
            {
                "raybet_name": raybet_name,
                "official_name": official_name,
                "method": "sourced_alias" if alias is not None else "normalized_exact",
                "source_url": None if alias is None else alias["source_url"],
            }
        )
    return tuple(output)


def _normalize_tournament(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"\b(?:19|20)\d{2}\b", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _letters_and_numbers(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


__all__ = [
    "ExactOfficialMapLink",
    "MAX_MAP_START_DELTA_SECONDS",
    "OfficialMapResultResolution",
    "OfficialMapResolution",
    "VerifiedOfficialMapResult",
    "resolve_exact_official_map_links",
    "resolve_verified_official_map_results",
]
