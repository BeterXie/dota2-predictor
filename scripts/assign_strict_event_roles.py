"""Assign causal positions for strictly eligible Dota 2 event maps."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import sqlite3
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Collection, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_intelligence.roles import (  # noqa: E402
    ASSIGNMENT_VERSION,
    HistoricalPositionEvidence,
    RoleAssignment,
    SingleMapRoleEvidence,
    assign_expected_positions,
    assign_observed_positions,
)


UTC = timezone.utc


class AvailabilityMode(str, Enum):
    RECONSTRUCTED_WALK_FORWARD = "reconstructed_walk_forward"
    PROSPECTIVE = "prospective"


ASSIGNMENT_VERSIONS = {
    AvailabilityMode.RECONSTRUCTED_WALK_FORWARD: (
        f"{ASSIGNMENT_VERSION}-reconstructed-walk-forward"
    ),
    AvailabilityMode.PROSPECTIVE: f"{ASSIGNMENT_VERSION}-prospective",
}


@dataclass(frozen=True)
class StrictRolePlayer:
    player_slot: int
    account_id: int | None
    team_id: int | None
    is_radiant: bool
    first_usable_at: datetime
    evidence: SingleMapRoleEvidence


@dataclass(frozen=True)
class StrictRoleMap:
    match_id: int
    started_at: datetime
    completed_at: datetime
    players: tuple[StrictRolePlayer, ...]


@dataclass(frozen=True)
class PersistedAssignment:
    player_slot: int
    account_id: int | None
    team_id: int | None
    assignment: RoleAssignment


@dataclass(frozen=True)
class AssignmentReport:
    assignment_version: str
    availability_mode: str
    dry_run: bool
    formal_maps: int
    selected_maps: int
    assignments: int
    inserted: int
    updated: int
    unchanged: int


def _utc(value: str, field: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> int | float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        return value
    return None


def _player_id(match_id: int, account_id: int | None, player_slot: int) -> int:
    if account_id is not None and account_id > 0:
        return account_id
    return -(match_id * 256 + player_slot + 1)


def _artifact_payload(path: Path, expected_hash: str) -> dict[str, Any]:
    try:
        content = gzip.decompress(path.read_bytes())
    except (OSError, EOFError) as error:
        raise RuntimeError(f"cannot read raw artifact {path}") from error
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise RuntimeError(f"raw artifact hash mismatch: {path}")
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError(f"raw artifact must contain an object: {path}")
    return payload


def _role_evidence(
    facts: Mapping[str, object],
    *,
    player_id: int,
    first_usable_at: datetime,
) -> SingleMapRoleEvidence:
    roaming = facts.get("is_roaming")
    return SingleMapRoleEvidence(
        player_id=player_id,
        first_usable_at=first_usable_at,
        lane_role=_integer(facts.get("lane_role")),
        gold_at_10=_number(facts.get("gold_at_10")),
        last_hits_at_10=_number(facts.get("last_hits_at_10")),
        is_roaming=roaming if isinstance(roaming, bool) else None,
        observer_wards_at_10=_integer(facts.get("observer_wards_at_10")),
        sentry_wards_at_10=_integer(facts.get("sentry_wards_at_10")),
        # OpenDota exposes only the terminal stack total in this archive.
        stacks_at_10=None,
        final_gpm=None,
    )


def load_strict_maps(
    connection: sqlite3.Connection, *, database_path: Path
) -> tuple[StrictRoleMap, ...]:
    rows = connection.execute(
        """SELECT f.match_id, f.player_slot, f.account_id, f.team_id, f.is_radiant,
                  f.facts_json, f.first_usable_at, f.source_content_hash,
                  a.storage_path
           FROM formal_map_eligibility AS eligible
           JOIN match_ingest_status AS status ON status.match_id=eligible.match_id
           JOIN player_map_facts AS f
             ON f.match_id=eligible.match_id
            AND f.source_content_hash=status.latest_raw_content_hash
           JOIN raw_source_artifacts AS a ON a.artifact_id=f.source_artifact_id
           WHERE eligible.player_readiness='ready'
             AND f.first_usable_at IS NOT NULL
             AND f.is_radiant IS NOT NULL
             AND f.fact_version='opendota-exact-v1:' || status.latest_raw_content_hash
             AND a.source='opendota'
           ORDER BY f.match_id, f.player_slot"""
    ).fetchall()

    grouped: dict[int, list[StrictRolePlayer]] = {}
    artifacts: dict[int, tuple[Path, str]] = {}
    for row in rows:
        facts = json.loads(row["facts_json"])
        if not isinstance(facts, dict):
            raise ValueError("player_map_facts.facts_json must contain an object")
        slot = int(row["player_slot"])
        account_id = _integer(row["account_id"])
        first_usable_at = _utc(row["first_usable_at"], "first_usable_at")
        path = Path(row["storage_path"])
        if not path.is_absolute():
            path = database_path.resolve().parent / path
        artifact = (path, str(row["source_content_hash"]))
        current = artifacts.setdefault(int(row["match_id"]), artifact)
        if current != artifact:
            raise ValueError(f"match {row['match_id']} has inconsistent fact artifacts")
        grouped.setdefault(int(row["match_id"]), []).append(
            StrictRolePlayer(
                player_slot=slot,
                account_id=account_id if account_id and account_id > 0 else None,
                team_id=_integer(row["team_id"]),
                is_radiant=bool(row["is_radiant"]),
                first_usable_at=first_usable_at,
                evidence=_role_evidence(
                    facts,
                    player_id=_player_id(int(row["match_id"]), account_id, slot),
                    first_usable_at=first_usable_at,
                ),
            )
        )

    maps = []
    for match_id, players in grouped.items():
        if len(players) != 10:
            continue
        sides = {
            side: [row for row in players if row.is_radiant is side]
            for side in (True, False)
        }
        if any(len(side_players) != 5 for side_players in sides.values()):
            continue
        if any(
            len({row.evidence.player_id for row in side_players}) != 5
            for side_players in sides.values()
        ):
            raise ValueError(f"match {match_id} has duplicate player identities")
        account_ids = [row.account_id for row in players if row.account_id is not None]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError(f"match {match_id} has duplicate positive account IDs")
        team_ids = {
            side: {row.team_id for row in side_players if row.team_id is not None}
            for side, side_players in sides.items()
        }
        if any(len(ids) > 1 for ids in team_ids.values()):
            raise ValueError(f"match {match_id} has mixed team IDs on one side")
        radiant_team = next(iter(team_ids[True]), None)
        dire_team = next(iter(team_ids[False]), None)
        if radiant_team is not None and radiant_team == dire_team:
            raise ValueError(f"match {match_id} has the same team ID on both sides")
        payload = _artifact_payload(*artifacts[match_id])
        start_time = _integer(payload.get("start_time"))
        duration = _integer(payload.get("duration"))
        if payload.get("match_id") != match_id or not start_time or not duration:
            raise ValueError(f"raw artifact identity/timing mismatch for match {match_id}")
        started_at = datetime.fromtimestamp(start_time, UTC)
        maps.append(
            StrictRoleMap(
                match_id,
                started_at,
                started_at + timedelta(seconds=duration),
                tuple(sorted(players, key=lambda row: row.player_slot)),
            )
        )
    return tuple(sorted(maps, key=lambda row: (row.started_at, row.match_id)))


def build_assignments(
    games: Sequence[StrictRoleMap],
    *,
    availability_mode: AvailabilityMode,
    match_id: int | None = None,
    match_ids: Collection[int] | None = None,
) -> tuple[PersistedAssignment, ...]:
    if match_id is not None and match_ids is not None:
        raise ValueError("match_id and match_ids are mutually exclusive")
    selected_ids = None if match_ids is None else {int(value) for value in match_ids}
    history: list[HistoricalPositionEvidence] = []
    output: list[PersistedAssignment] = []
    for game in games:
        map_rows: list[PersistedAssignment] = []
        for is_radiant in (True, False):
            players = tuple(row for row in game.players if row.is_radiant is is_radiant)
            target = tuple(row.evidence for row in players)
            expected = assign_expected_positions(
                match_id=game.match_id,
                target_started_at=game.started_at,
                cutoff=game.started_at,
                player_ids=tuple(row.player_id for row in target),
                history=history,
            )
            observed_cutoff = max(row.first_usable_at for row in players)
            observed = assign_observed_positions(
                match_id=game.match_id,
                target_started_at=game.started_at,
                cutoff=observed_cutoff,
                players=target,
                history=history,
            )
            version = ASSIGNMENT_VERSIONS[availability_mode]
            expected = tuple(
                replace(
                    row,
                    version=version,
                    input_hash=hashlib.sha256(
                        f"{version}:{row.input_hash}".encode("ascii")
                    ).hexdigest(),
                )
                for row in expected
            )
            observed = tuple(
                replace(
                    row,
                    version=version,
                    input_hash=hashlib.sha256(
                        f"{version}:{row.input_hash}".encode("ascii")
                    ).hexdigest(),
                )
                for row in observed
            )
            by_id = {row.evidence.player_id: row for row in players}
            for assignment in (*expected, *observed):
                player = by_id[assignment.player_id]
                map_rows.append(
                    PersistedAssignment(
                        player.player_slot,
                        player.account_id,
                        player.team_id,
                        assignment,
                    )
                )
            for assignment in observed:
                if assignment.position is not None:
                    player = by_id[assignment.player_id]
                    history.append(
                        HistoricalPositionEvidence(
                            player_id=assignment.player_id,
                            match_id=game.match_id,
                            position=assignment.position,
                            confidence=assignment.confidence,
                            completed_at=game.completed_at,
                            first_usable_at=(
                                game.completed_at
                                if availability_mode
                                is AvailabilityMode.RECONSTRUCTED_WALK_FORWARD
                                else observed_cutoff
                            ),
                        )
                    )
        if (
            (match_id is None and selected_ids is None)
            or game.match_id == match_id
            or (selected_ids is not None and game.match_id in selected_ids)
        ):
            output.extend(map_rows)
    return tuple(output)


def persist_assignments(
    connection: sqlite3.Connection,
    rows: Sequence[PersistedAssignment],
    *,
    dry_run: bool,
) -> tuple[int, int, int]:
    inserted = updated = unchanged = 0
    now = datetime.now(UTC).isoformat()
    if not dry_run:
        connection.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            assignment = row.assignment
            existing = connection.execute(
                """SELECT account_id, team_id, position, assignment_source,
                          confidence, input_cutoff, input_hash
                   FROM player_role_assignments
                   WHERE match_id=? AND player_slot=? AND purpose=?
                     AND assignment_version=?""",
                (
                    assignment.match_id,
                    row.player_slot,
                    assignment.purpose.value,
                    assignment.version,
                ),
            ).fetchone()
            expected = (
                row.account_id,
                row.team_id,
                assignment.position,
                assignment.source.value,
                assignment.confidence,
                assignment.cutoff.isoformat(),
                assignment.input_hash,
            )
            if existing is not None and tuple(existing) == expected:
                unchanged += 1
                continue
            inserted += existing is None
            updated += existing is not None
            if dry_run:
                continue
            connection.execute(
                """INSERT INTO player_role_assignments
                   (match_id, player_slot, account_id, team_id, purpose, position,
                    assignment_source, confidence, input_cutoff, input_hash,
                    assignment_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(match_id, player_slot, purpose, assignment_version)
                   DO UPDATE SET account_id=excluded.account_id,
                     team_id=excluded.team_id, position=excluded.position,
                     assignment_source=excluded.assignment_source,
                     confidence=excluded.confidence, input_cutoff=excluded.input_cutoff,
                     input_hash=excluded.input_hash, created_at=excluded.created_at""",
                (
                    assignment.match_id,
                    row.player_slot,
                    row.account_id,
                    row.team_id,
                    assignment.purpose.value,
                    assignment.position,
                    assignment.source.value,
                    assignment.confidence,
                    assignment.cutoff.isoformat(),
                    assignment.input_hash,
                    assignment.version,
                    now,
                ),
            )
        if not dry_run:
            connection.commit()
    except BaseException:
        if not dry_run:
            connection.rollback()
        raise
    return inserted, updated, unchanged


def run_assignment(
    database: Path,
    *,
    dry_run: bool = False,
    match_id: int | None = None,
    match_ids: Collection[int] | None = None,
    availability_mode: AvailabilityMode = AvailabilityMode.RECONSTRUCTED_WALK_FORWARD,
) -> AssignmentReport:
    if match_id is not None and match_ids is not None:
        raise ValueError("match_id and match_ids are mutually exclusive")
    selected_ids = None if match_ids is None else {int(value) for value in match_ids}
    database = database.resolve()
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro" if dry_run else database,
        uri=dry_run,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        games = load_strict_maps(connection, database_path=database)
        available_ids = {game.match_id for game in games}
        requested_ids = {match_id} if match_id is not None else selected_ids
        missing_ids = set() if requested_ids is None else requested_ids - available_ids
        if missing_ids:
            raise ValueError(
                "formal ready match not found: "
                + ",".join(str(value) for value in sorted(missing_ids))
            )
        rows = build_assignments(
            games,
            availability_mode=availability_mode,
            match_id=match_id,
            match_ids=selected_ids,
        )
        inserted, updated, unchanged = persist_assignments(
            connection, rows, dry_run=dry_run
        )
        return AssignmentReport(
            ASSIGNMENT_VERSIONS[availability_mode],
            availability_mode.value,
            dry_run,
            len(games),
            len(games) if requested_ids is None else len(requested_ids),
            len(rows),
            inserted,
            updated,
            unchanged,
        )
    finally:
        connection.close()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--match", type=_positive_int, help="one formal match ID")
    parser.add_argument("--dry-run", action="store_true", help="compute without writes")
    parser.add_argument(
        "--availability-mode",
        choices=tuple(mode.value for mode in AvailabilityMode),
        default=AvailabilityMode.RECONSTRUCTED_WALK_FORWARD.value,
        help="historical evidence availability policy",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_assignment(
        args.database,
        dry_run=args.dry_run,
        match_id=args.match,
        availability_mode=AvailabilityMode(args.availability_mode),
    )
    print(json.dumps(report.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
