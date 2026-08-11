"""Stable match identifiers and human-readable match labels."""

from __future__ import annotations

from typing import Any

from database.session import PostgresSession


def observation_file_name(raybet_match_id: str) -> str:
    match_id = str(raybet_match_id).strip()
    if not match_id or any(character in match_id for character in ("/", "\\")):
        raise ValueError("raybet_match_id must be a safe non-empty identifier")
    return f"{match_id}.jsonl"


def match_display_name(
    *,
    raybet_match_id: str,
    team_one: str | None,
    team_two: str | None,
    tournament: str | None,
) -> str:
    external_id = f"RayBet Series {raybet_match_id}"
    teams = f"{team_one or '队伍一'} vs {team_two or '队伍二'}"
    return " · ".join(part for part in (external_id, teams, tournament) if part)


def observation_file_metadata(
    connection: PostgresSession,
    name: str,
) -> dict[str, Any] | None:
    """Resolve a stable RayBet-named corpus to human-facing match metadata."""
    raybet_match_id = name[:-6] if name.endswith(".jsonl") else None
    if not raybet_match_id:
        return None
    row = connection.execute(
        """SELECT match_row.raybet_match_id,
                  match_row.team_one, match_row.team_two, match_row.tournament
             FROM raybet_matches AS match_row
            WHERE match_row.raybet_match_id=?
            LIMIT 1""",
        (raybet_match_id,),
    ).fetchone()
    if row is None:
        return None
    resolved_raybet_id = str(row["raybet_match_id"])
    return {
        "raybet_match_id": resolved_raybet_id,
        "official_match_id": None,
        "display_name": match_display_name(
            raybet_match_id=resolved_raybet_id,
            team_one=str(row["team_one"] or "") or None,
            team_two=str(row["team_two"] or "") or None,
            tournament=str(row["tournament"] or "") or None,
        ),
    }


__all__ = [
    "match_display_name",
    "observation_file_metadata",
    "observation_file_name",
]
