"""Build versioned team states and causal profiles for strict formal maps."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Sequence

from database.session import PostgresSession


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_intelligence.raw_archive import canonical_json_bytes  # noqa: E402
from event_intelligence.storage import IntelligenceStorage  # noqa: E402
from event_intelligence.team_profiles import (  # noqa: E402
    AvailabilityMode,
    EvidenceRef,
    ProfileMap,
    TeamStyleProfile,
    build_team_style_profile,
    derive_causal_event_patch_priors,
)
from event_intelligence.team_states import (  # noqa: E402
    Side,
    TeamMapState,
    TeamObjective,
    build_team_map_states,
)


UTC = timezone.utc


@dataclass(frozen=True)
class StrictMap:
    match_id: int
    event_id: str
    start_time: int
    duration: int
    radiant_win: bool
    radiant_team_id: int
    dire_team_id: int
    patch: int | None
    first_usable_at: datetime | None
    source_version: str
    objective_source_complete: bool
    event_tier: str
    prize_pool_usd: int

    @property
    def completed_at(self) -> datetime:
        return datetime.fromtimestamp(self.start_time + self.duration, tz=UTC)


@dataclass(frozen=True)
class BuildReport:
    formal_maps: int
    state_rows: int
    unscorable_state_rows: int
    profile_rows: int
    cutoff: str


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored first_usable_at must be timezone-aware")
    return parsed.astimezone(UTC)


def _load_strict_maps(connection: PostgresSession) -> tuple[StrictMap, ...]:
    rows = connection.execute(
        """SELECT f.match_id, f.event_id, m.start_time, m.duration,
                  m.radiant_win, m.radiant_team_id, m.dire_team_id, m.patch,
                  s.latest_raw_content_hash, s.raw_artifact_version,
                  s.missing_fields_json, a.first_usable_at AS artifact_usable_at,
                  (SELECT MIN(o.first_usable_at)
                     FROM raw_source_observations AS o
                    WHERE o.artifact_id = a.artifact_id
                      AND o.content_hash = a.content_hash
                      AND o.first_usable_at IS NOT NULL) AS observation_usable_at,
                  e.tier AS event_tier, e.prize_pool_usd
           FROM formal_map_eligibility AS f
           JOIN match_ingest_status AS s ON s.match_id = f.match_id
           JOIN matches AS m ON m.match_id = f.match_id
           JOIN event_registry AS e ON e.event_id = f.event_id
           LEFT JOIN raw_source_artifacts AS a
             ON a.artifact_id = s.latest_raw_artifact_id
            AND a.content_hash = s.latest_raw_content_hash
          WHERE f.state_readiness IN ('ready', 'unscorable')
           ORDER BY m.start_time, f.match_id"""
    ).fetchall()
    result = []
    for row in rows:
        if any(
            row[name] is None
            for name in (
                "start_time",
                "duration",
                "radiant_win",
                "radiant_team_id",
                "dire_team_id",
            )
        ):
            continue
        if row["radiant_win"] not in (0, 1):
            continue
        missing = json.loads(row["missing_fields_json"] or "[]")
        result.append(
            StrictMap(
                match_id=int(row["match_id"]),
                event_id=str(row["event_id"]),
                start_time=int(row["start_time"]),
                duration=int(row["duration"]),
                radiant_win=bool(row["radiant_win"]),
                radiant_team_id=int(row["radiant_team_id"]),
                dire_team_id=int(row["dire_team_id"]),
                patch=None if row["patch"] is None else int(row["patch"]),
                first_usable_at=_parse_timestamp(
                    row["observation_usable_at"] or row["artifact_usable_at"]
                ),
                source_version=(
                    str(row["latest_raw_content_hash"])
                    if row["latest_raw_content_hash"]
                    else f"legacy-artifact-{int(row['raw_artifact_version'])}"
                ),
                objective_source_complete="objectives_incomplete" not in missing,
                event_tier=str(row["event_tier"]),
                prize_pool_usd=int(row["prize_pool_usd"]),
            )
        )
    return tuple(result)


def _gold_curve(
    connection: PostgresSession, match: StrictMap
) -> tuple[int | float | None, ...] | None:
    rows = connection.execute(
        """SELECT time_min, value FROM gold_advantage
           WHERE match_id=? ORDER BY time_min""",
        (match.match_id,),
    ).fetchall()
    if not rows:
        return None
    last_required = match.duration // 60 - 1
    last_observed = max(int(row["time_min"]) for row in rows)
    values: list[int | float | None] = [None] * (max(last_required, last_observed) + 1)
    conflicts: set[int] = set()
    for row in rows:
        minute = int(row["time_min"])
        value = row["value"]
        if minute < 0 or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if minute in conflicts:
            continue
        if values[minute] is not None and values[minute] != value:
            values[minute] = None
            conflicts.add(minute)
        else:
            values[minute] = value
    return tuple(values)


def _side_from_slot(player_slot: int | None) -> Side | None:
    if player_slot is None:
        return None
    if 0 <= player_slot <= 4:
        return Side.RADIANT
    if 128 <= player_slot <= 132:
        return Side.DIRE
    return None


def _objective_side(
    objective_type: str, key: str | None, player_slot: int | None
) -> Side | None:
    if objective_type == "building_kill" and key:
        if key.startswith("npc_dota_goodguys_"):
            return Side.DIRE
        if key.startswith("npc_dota_badguys_"):
            return Side.RADIANT
        return None
    return _side_from_slot(player_slot)


_IGNORED_OBJECTIVE_TYPES = {
    "CHAT_MESSAGE_COURIER_LOST",
    "CHAT_MESSAGE_FIRSTBLOOD",
    "CHAT_MESSAGE_MINIBOSS_KILL",
}
_ROSHAN_OBJECTIVE_TYPES = {
    "CHAT_MESSAGE_ROSHAN_KILL",
    "CHAT_MESSAGE_AEGIS",
    "CHAT_MESSAGE_AEGIS_STOLEN",
    "CHAT_MESSAGE_DENIED_AEGIS",
    "AEGIS_STOLEN",
}
_ROSHAN_PRIORITY = {
    "CHAT_MESSAGE_ROSHAN_KILL": 0,
    "CHAT_MESSAGE_AEGIS": 1,
    "CHAT_MESSAGE_AEGIS_STOLEN": 2,
    "AEGIS_STOLEN": 2,
}


_LOW_GROUND_BUILDING = re.compile(
    r"npc_dota_(?:goodguys|badguys)_tower[12]_(?:top|mid|bot)\Z"
)
_HIGH_GROUND_BUILDING = re.compile(
    r"npc_dota_(?:goodguys|badguys)_"
    r"(?:tower3_(?:top|mid|bot)|tower4|(?:melee|range)_rax_(?:top|mid|bot)|fort)\Z"
)


def _building_kind(key: str) -> str | None:
    if _LOW_GROUND_BUILDING.fullmatch(key):
        return "tower"
    if _HIGH_GROUND_BUILDING.fullmatch(key):
        return "high_ground"
    return None


def _deduplicated_roshans(
    rows: list[tuple[int | float, str, Side | None]],
) -> tuple[TeamObjective, ...] | None:
    clusters: list[list[tuple[int | float, str, Side | None]]] = []
    for row in sorted(rows, key=lambda value: (value[0], value[1])):
        if not clusters or row[0] - clusters[-1][0][0] > 300:
            clusters.append([row])
        else:
            clusters[-1].append(row)
    result = []
    for cluster in clusters:
        denied = any(value[1] == "CHAT_MESSAGE_DENIED_AEGIS" for value in cluster)
        acquired = [value for value in cluster if value[1] != "CHAT_MESSAGE_DENIED_AEGIS"]
        has_holder = any(_ROSHAN_PRIORITY[value[1]] > 0 for value in acquired)
        if denied and has_holder:
            return None
        if denied:
            continue
        top_priority = max(_ROSHAN_PRIORITY[value[1]] for value in acquired)
        candidates = [
            value for value in acquired if _ROSHAN_PRIORITY[value[1]] == top_priority
        ]
        sides = {value[2] for value in candidates}
        if len(sides) != 1 or None in sides:
            return None
        selected = min(candidates, key=lambda value: value[0])
        result.append(TeamObjective(selected[0], selected[2], "roshan"))
    return tuple(result)


def _objectives(
    connection: PostgresSession, match: StrictMap
) -> tuple[TeamObjective, ...] | None:
    if not match.objective_source_complete:
        return None
    rows = connection.execute(
        """SELECT time, type, key, player_slot FROM objectives
           WHERE match_id=? ORDER BY time, id""",
        (match.match_id,),
    ).fetchall()
    if not rows:
        return None
    result: list[TeamObjective] = []
    roshan_rows: list[tuple[int | float, str, Side | None]] = []
    for row in rows:
        objective_type = str(row["type"] or "")
        key = None if row["key"] is None else str(row["key"])
        slot = None if row["player_slot"] is None else int(row["player_slot"])
        if row["time"] is None:
            return None
        if objective_type in _IGNORED_OBJECTIVE_TYPES:
            continue
        if objective_type in _ROSHAN_OBJECTIVE_TYPES:
            roshan_rows.append(
                (row["time"], objective_type, _objective_side(objective_type, key, slot))
            )
            continue
        if objective_type != "building_kill" or key is None:
            return None
        side = _objective_side(objective_type, key, slot)
        kind = _building_kind(key)
        if side is None or kind is None:
            return None
        result.append(TeamObjective(row["time"], side, kind))
    roshans = _deduplicated_roshans(roshan_rows)
    if roshans is None:
        return None
    return tuple(sorted((*result, *roshans), key=lambda value: value.time_seconds))


def _event_strength(match: StrictMap) -> tuple[float, str]:
    if match.event_tier != "tier_1" or match.prize_pool_usd < 1_000_000:
        return 1.0, "neutral:missing_approved_event_strength"
    weight = min(1.25, max(0.75, math.sqrt(match.prize_pool_usd / 1_000_000)))
    return weight, f"registry:{match.event_tier}:prize_pool_usd={match.prize_pool_usd}"


def _opponent_strength(
    target: ProfileMap, all_rows: Sequence[ProfileMap], cutoff: datetime
) -> tuple[float, str, tuple[EvidenceRef, ...]]:
    if target.state.opponent_id is None:
        return 1.0, "neutral:opponent_missing", ()
    history = tuple(
        row
        for row in all_rows
        if row.state.team_id == target.state.opponent_id
        and row.state.match_id != target.state.match_id
        and row.state.won is not None
        and row.completed_at < cutoff
        and row.first_usable_at is not None
        and row.first_usable_at <= cutoff
    )
    if not history:
        return 1.0, "neutral:no_causal_opponent_maps", ()
    posterior_win_rate = (sum(bool(row.state.won) for row in history) + 1.0) / (
        len(history) + 2.0
    )
    evidence = tuple(
        sorted(
            (
                row.state.match_id,
                row.state.input_hash,
                row.first_usable_at.isoformat(),
            )
            for row in history
            if row.first_usable_at is not None
        )
    )
    return (
        0.5 + posterior_win_rate,
        f"causal_cutoff_beta_1_1:n={len(history)}",
        evidence,
    )


def _roster(
    connection: PostgresSession, match_id: int, team_id: int, side: Side
) -> tuple[int, ...]:
    rows = connection.execute(
        """SELECT DISTINCT account_id FROM match_players
           WHERE match_id=? AND account_id IS NOT NULL
             AND (team_id=? OR (team_id IS NULL AND is_radiant=?))
           ORDER BY account_id""",
        (match_id, team_id, side is Side.RADIANT),
    ).fetchall()
    return tuple(int(row[0]) for row in rows if int(row[0]) > 0)


def _json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _persist_state(
    connection: PostgresSession, state: TeamMapState, created_at: str
) -> None:
    conversion = asdict(state.objective_conversion)
    crossings = [asdict(value) for value in state.crossings]
    connection.execute(
        """INSERT INTO team_map_states
           (match_id, team_id, side, label, duration_seconds, max_lead,
            max_deficit, ahead_fraction, behind_fraction, even_fraction,
            signed_auc, absolute_auc, crossings_json,
            first_significant_lead_at, first_significant_deficit_at,
            closeout_seconds, objective_conversion_json, curve_coverage,
            source_versions_json, input_hash, label_version, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(match_id, side, label_version) DO UPDATE SET
             team_id=excluded.team_id, label=excluded.label,
             duration_seconds=excluded.duration_seconds, max_lead=excluded.max_lead,
             max_deficit=excluded.max_deficit,
             ahead_fraction=excluded.ahead_fraction,
             behind_fraction=excluded.behind_fraction,
             even_fraction=excluded.even_fraction, signed_auc=excluded.signed_auc,
             absolute_auc=excluded.absolute_auc,
             crossings_json=excluded.crossings_json,
             first_significant_lead_at=excluded.first_significant_lead_at,
             first_significant_deficit_at=excluded.first_significant_deficit_at,
             closeout_seconds=excluded.closeout_seconds,
             objective_conversion_json=excluded.objective_conversion_json,
             curve_coverage=excluded.curve_coverage,
             source_versions_json=excluded.source_versions_json,
             input_hash=excluded.input_hash, created_at=excluded.created_at
           WHERE team_map_states.input_hash <> excluded.input_hash""",
        (
            state.match_id,
            state.team_id,
            state.side.value,
            state.label.value,
            state.duration_seconds,
            state.max_lead,
            state.max_deficit,
            state.ahead_fraction,
            state.behind_fraction,
            state.even_fraction,
            state.signed_auc,
            state.absolute_auc,
            _json(crossings),
            state.first_significant_lead_at,
            state.first_significant_deficit_at,
            state.closeout_seconds,
            _json(conversion),
            state.curve_coverage,
            _json(state.source_versions),
            state.input_hash,
            state.label_version,
            created_at,
        ),
    )


def _persist_profile(
    connection: PostgresSession, profile: TeamStyleProfile, created_at: str
) -> None:
    weighting = {
        "availability_mode": profile.availability_mode.value,
        "maps": [asdict(value) for value in profile.weighting],
    }
    connection.execute(
        """INSERT INTO team_style_profiles
           (team_id, profile_cutoff, profile_version, opportunity_counts_json,
            posterior_rates_json, duration_quantiles_json, weighting_json,
            effective_sample_size, input_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(team_id, profile_cutoff, profile_version) DO UPDATE SET
             opportunity_counts_json=excluded.opportunity_counts_json,
             posterior_rates_json=excluded.posterior_rates_json,
             duration_quantiles_json=excluded.duration_quantiles_json,
             weighting_json=excluded.weighting_json,
             effective_sample_size=excluded.effective_sample_size,
             input_hash=excluded.input_hash, created_at=excluded.created_at
           WHERE team_style_profiles.input_hash <> excluded.input_hash""",
        (
            profile.team_id,
            profile.cutoff.isoformat(),
            profile.profile_version,
            _json(profile.opportunity_counts),
            _json([asdict(value) for value in profile.posterior_rates]),
            _json([asdict(value) for value in profile.duration_quantiles]),
            _json(weighting),
            profile.effective_sample_size,
            profile.input_hash,
            created_at,
        ),
    )


def build_strict_profiles(
    storage: IntelligenceStorage,
    cutoff: datetime,
    *,
    match_ids: Collection[int] | None = None,
) -> BuildReport:
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")
    cutoff = cutoff.astimezone(UTC)
    storage.init_schema(seed_events=False)
    connection = storage.connection
    maps = _load_strict_maps(connection)
    with connection.transaction():
        selected_ids = None if match_ids is None else {int(value) for value in match_ids}
        available_ids = {match.match_id for match in maps}
        missing_ids = set() if selected_ids is None else selected_ids - available_ids
        if missing_ids:
            raise ValueError(
                "formal ready match not found: "
                + ",".join(str(value) for value in sorted(missing_ids))
            )
        created_at = datetime.now(UTC).isoformat()
        profile_rows: dict[int, list[ProfileMap]] = {}
        affected_team_ids: set[int] = set()
        state_count = 0
        unscorable_count = 0
        with storage.transaction():
            for match in maps:
                states = build_team_map_states(
                    match_id=match.match_id,
                    duration_seconds=match.duration,
                    radiant_win=match.radiant_win,
                    radiant_team_id=match.radiant_team_id,
                    dire_team_id=match.dire_team_id,
                    radiant_gold_adv=_gold_curve(connection, match),
                    objectives=_objectives(connection, match),
                    source_versions={"opendota": match.source_version},
                )
                for state in states:
                    if selected_ids is None or match.match_id in selected_ids:
                        _persist_state(connection, state, created_at)
                        state_count += 1
                        unscorable_count += state.label.value == "state_unscorable"
                        if state.team_id is not None:
                            affected_team_ids.add(state.team_id)
                    if state.team_id is None:
                        continue
                    event_weight, event_scope = _event_strength(match)
                    profile_rows.setdefault(state.team_id, []).append(
                        ProfileMap(
                            state=state,
                            completed_at=match.completed_at,
                            first_usable_at=match.first_usable_at,
                            event_id=match.event_id,
                            patch=match.patch,
                            roster=_roster(
                                connection, match.match_id, state.team_id, state.side
                            ),
                            event_strength_weight=event_weight,
                            event_strength_scope=event_scope,
                        )
                    )

            all_rows = tuple(row for rows in profile_rows.values() for row in rows)
            weighted_rows: dict[int, list[ProfileMap]] = {}
            for team_id, rows in profile_rows.items():
                weighted_rows[team_id] = []
                for row in rows:
                    (
                        opponent_weight,
                        opponent_scope,
                        opponent_evidence,
                    ) = _opponent_strength(row, all_rows, cutoff)
                    weighted_rows[team_id].append(
                        replace(
                            row,
                            opponent_strength_weight=opponent_weight,
                            opponent_strength_scope=opponent_scope,
                            opponent_strength_evidence=opponent_evidence,
                        )
                    )
            profile_rows = weighted_rows
            all_rows = tuple(row for rows in profile_rows.values() for row in rows)
            profiles = []
            for team_id, rows in sorted(profile_rows.items()):
                if selected_ids is not None and team_id not in affected_team_ids:
                    continue
                prior = tuple(
                    row
                    for row in rows
                    if row.completed_at < cutoff
                    and row.first_usable_at is not None
                    and row.first_usable_at <= cutoff
                )
                if not prior:
                    continue
                latest = max(
                    prior, key=lambda row: (row.completed_at, row.state.match_id)
                )
                profile = build_team_style_profile(
                    team_id=team_id,
                    cutoff=cutoff,
                    maps=rows,
                    target_roster=latest.roster,
                    target_patch=latest.patch,
                    priors=derive_causal_event_patch_priors(
                        team_id=team_id,
                        cutoff=cutoff,
                        maps=all_rows,
                        target_event_id=latest.event_id,
                        target_patch=latest.patch,
                    ),
                    availability_mode=AvailabilityMode.PROSPECTIVE,
                )
                _persist_profile(connection, profile, created_at)
                profiles.append(profile)
    return BuildReport(
        formal_maps=len(maps),
        state_rows=state_count,
        unscorable_state_rows=unscorable_count,
        profile_rows=len(profiles),
        cutoff=cutoff.isoformat(),
    )


def _cutoff(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("cutoff must include a timezone")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="PostgreSQL URL (default: DATABASE_URL)")
    parser.add_argument(
        "--cutoff",
        type=_cutoff,
        default=None,
        help="profile cutoff as timezone-aware ISO-8601 (default: now)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    storage = IntelligenceStorage(args.database_url)
    try:
        storage.init_schema(seed_events=False)
        report = build_strict_profiles(
            storage,
            args.cutoff or datetime.now(UTC),
        )
    finally:
        storage.close()
    print(json.dumps(asdict(report), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
