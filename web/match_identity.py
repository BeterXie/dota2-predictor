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
    official_match_id: str | None,
    team_one: str | None,
    team_two: str | None,
    tournament: str | None,
) -> str:
    external_id = (
        f"官方 Match ID {official_match_id}"
        if official_match_id
        else f"RayBet {raybet_match_id}"
    )
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
                  official.provider_match_id AS official_match_id,
                  match_row.team_one, match_row.team_two, match_row.tournament
             FROM raybet_matches AS match_row
             LEFT JOIN LATERAL (
                 SELECT link.provider_match_id
                   FROM match_links AS link
                  WHERE link.raybet_match_id=match_row.raybet_match_id
                    AND link.provider IN ('opendota', 'stratz')
                    AND link.status='accepted'
                  ORDER BY CASE link.provider
                             WHEN 'stratz' THEN 0
                             WHEN 'opendota' THEN 1
                             ELSE 2
                           END
                  LIMIT 1
             ) AS official ON TRUE
            WHERE match_row.raybet_match_id=?
            LIMIT 1""",
        (raybet_match_id,),
    ).fetchone()
    if row is None:
        return None
    official_match_id = (
        str(row["official_match_id"])
        if row["official_match_id"] is not None
        else None
    )
    resolved_raybet_id = str(row["raybet_match_id"])
    return {
        "raybet_match_id": resolved_raybet_id,
        "official_match_id": official_match_id,
        "display_name": match_display_name(
            raybet_match_id=resolved_raybet_id,
            official_match_id=official_match_id,
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
