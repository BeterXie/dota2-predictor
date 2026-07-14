"""Durable, idempotent derived-data completion for strict formal maps."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Collection, Iterator

from .player_scoring import score_version_for_role
from .team_profiles import PROFILE_VERSION
from .team_states import LABEL_VERSION


UTC = timezone.utc
ROLE_VERSION = "role-assignment-v1-reconstructed-walk-forward"
SCORE_VERSION = score_version_for_role(ROLE_VERSION)


@dataclass(frozen=True)
class DerivedRunReport:
    requested_maps: int
    pending_maps: int
    derived_maps: int
    assignment_rows: int
    score_rows: int
    state_rows: int
    profile_rows: int
    profile_cutoff: str | None


class StrictDerivedPipeline:
    """Complete only maps whose current source version lacks derived lineage."""

    def __init__(self, database: Path) -> None:
        self.database = database.resolve()

    def run(
        self,
        match_ids: Collection[int] = (),
        *,
        force: bool = False,
    ) -> DerivedRunReport:
        requested = {int(value) for value in match_ids}
        with self._connection() as connection:
            self._bootstrap_existing(connection)
            pending = self._pending_ids(connection)
            if force:
                pending.update(requested)
            self._require_formal_ids(connection, pending)

        if not pending:
            return DerivedRunReport(len(requested), 0, 0, 0, 0, 0, 0, None)

        # These functions read the complete earlier-only history, but persist only
        # the selected maps and teams.
        from scripts.assign_strict_event_roles import (
            AvailabilityMode,
            run_assignment,
        )
        from scripts.build_strict_team_profiles import build_strict_profiles
        from scripts.score_strict_event_players import run_scoring

        ordered = tuple(sorted(pending))
        assignments = run_assignment(
            self.database,
            match_ids=ordered,
            availability_mode=AvailabilityMode.RECONSTRUCTED_WALK_FORWARD,
        )
        scores = run_scoring(
            self.database,
            match_ids=ordered,
            assignment_version=ROLE_VERSION,
        )
        cutoff = self._profile_cutoff(ordered)
        profiles = build_strict_profiles(
            self.database,
            cutoff,
            match_ids=ordered,
        )
        with self._connection() as connection:
            self._verify_derived(connection, ordered)
            now = datetime.now(UTC).isoformat()
            with connection:
                for match_id in ordered:
                    content_hash = connection.execute(
                        "SELECT latest_raw_content_hash FROM match_ingest_status "
                        "WHERE match_id=?",
                        (match_id,),
                    ).fetchone()[0]
                    connection.execute(
                        """INSERT INTO strict_derived_status
                           (match_id, source_content_hash, role_assignment_version,
                            score_version, team_state_version, profile_version,
                            profile_cutoff, derived_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(match_id) DO UPDATE SET
                             source_content_hash=excluded.source_content_hash,
                             role_assignment_version=excluded.role_assignment_version,
                             score_version=excluded.score_version,
                             team_state_version=excluded.team_state_version,
                             profile_version=excluded.profile_version,
                             profile_cutoff=excluded.profile_cutoff,
                             derived_at=excluded.derived_at""",
                        (
                            match_id,
                            content_hash,
                            ROLE_VERSION,
                            SCORE_VERSION,
                            LABEL_VERSION,
                            PROFILE_VERSION,
                            cutoff.isoformat(),
                            now,
                        ),
                    )
        return DerivedRunReport(
            len(requested),
            len(ordered),
            len(ordered),
            assignments.assignments,
            scores.scored_players,
            profiles.state_rows,
            profiles.profile_rows,
            cutoff.isoformat(),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _require_formal_ids(
        connection: sqlite3.Connection, match_ids: Collection[int]
    ) -> None:
        if not match_ids:
            return
        placeholders = ",".join("?" for _ in match_ids)
        found = {
            int(row[0])
            for row in connection.execute(
                f"SELECT match_id FROM formal_map_eligibility "
                f"WHERE match_id IN ({placeholders})",
                tuple(match_ids),
            )
        }
        missing = set(match_ids) - found
        if missing:
            raise ValueError(
                "formal ready match not found: "
                + ",".join(str(value) for value in sorted(missing))
            )

    def _pending_ids(self, connection: sqlite3.Connection) -> set[int]:
        return {
            int(row[0])
            for row in connection.execute(
                """SELECT eligible.match_id
                     FROM formal_map_eligibility AS eligible
                     JOIN match_ingest_status AS status
                       ON status.match_id=eligible.match_id
                     LEFT JOIN strict_derived_status AS derived
                       ON derived.match_id=eligible.match_id
                    WHERE eligible.player_readiness='ready'
                      AND eligible.state_readiness='ready'
                      AND status.latest_raw_content_hash IS NOT NULL
                      AND (derived.match_id IS NULL
                           OR derived.source_content_hash<>status.latest_raw_content_hash
                           OR derived.role_assignment_version<>?
                           OR derived.score_version<>?
                           OR derived.team_state_version<>?
                           OR derived.profile_version<>?)""",
                (ROLE_VERSION, SCORE_VERSION, LABEL_VERSION, PROFILE_VERSION),
            )
        }

    def _bootstrap_existing(self, connection: sqlite3.Connection) -> None:
        """Adopt already-complete rows once without recomputing the whole archive."""
        rows = connection.execute(
            """SELECT eligible.match_id, status.latest_raw_content_hash,
                      MAX(facts.created_at) AS facts_created_at,
                      matches.radiant_team_id, matches.dire_team_id
                 FROM formal_map_eligibility AS eligible
                 JOIN match_ingest_status AS status USING(match_id)
                 JOIN matches USING(match_id)
                 JOIN player_map_facts AS facts
                   ON facts.match_id=eligible.match_id
                  AND facts.fact_version='opendota-exact-v1:' ||
                                         status.latest_raw_content_hash
                LEFT JOIN strict_derived_status AS derived
                  ON derived.match_id=eligible.match_id
                WHERE derived.match_id IS NULL
                GROUP BY eligible.match_id
               HAVING COUNT(facts.fact_id)=10"""
        ).fetchall()
        now = datetime.now(UTC).isoformat()
        adopted: list[tuple[object, ...]] = []
        for row in rows:
            match_id = int(row["match_id"])
            if not self._existing_map_is_complete(connection, row):
                continue
            cutoffs = []
            for team_id in (row["radiant_team_id"], row["dire_team_id"]):
                profile = connection.execute(
                    """SELECT profile_cutoff FROM team_style_profiles
                        WHERE team_id=? AND profile_version=?
                        ORDER BY profile_cutoff DESC LIMIT 1""",
                    (team_id, PROFILE_VERSION),
                ).fetchone()
                if profile is None:
                    break
                cutoffs.append(str(profile[0]))
            if len(cutoffs) != 2:
                continue
            adopted.append(
                (
                    match_id,
                    row["latest_raw_content_hash"],
                    ROLE_VERSION,
                    SCORE_VERSION,
                    LABEL_VERSION,
                    PROFILE_VERSION,
                    max(cutoffs),
                    now,
                )
            )
        if adopted:
            with connection:
                connection.executemany(
                    """INSERT OR IGNORE INTO strict_derived_status
                       (match_id, source_content_hash, role_assignment_version,
                        score_version, team_state_version, profile_version,
                        profile_cutoff, derived_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    adopted,
                )

    @staticmethod
    def _existing_map_is_complete(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> bool:
        match_id = int(row["match_id"])
        role_count = connection.execute(
            """SELECT COUNT(*) FROM player_role_assignments
                WHERE match_id=? AND purpose='observed_position'
                  AND assignment_version=? AND position BETWEEN 1 AND 5""",
            (match_id, ROLE_VERSION),
        ).fetchone()[0]
        score_row = connection.execute(
            """SELECT COUNT(*), MIN(created_at) FROM player_map_scores
                WHERE match_id=? AND score_version=?""",
            (match_id, SCORE_VERSION),
        ).fetchone()
        states = connection.execute(
            """SELECT source_versions_json FROM team_map_states
                WHERE match_id=? AND label_version=?""",
            (match_id, LABEL_VERSION),
        ).fetchall()
        state_hashes = []
        for state in states:
            try:
                state_hashes.append(dict(json.loads(state[0])).get("opendota"))
            except (TypeError, ValueError):
                return False
        return (
            role_count == 10
            and score_row[0] == 10
            and score_row[1] is not None
            and str(score_row[1]) >= str(row["facts_created_at"])
            and len(states) == 2
            and all(value == row["latest_raw_content_hash"] for value in state_hashes)
        )

    def _profile_cutoff(self, match_ids: Collection[int]) -> datetime:
        with self._connection() as connection:
            placeholders = ",".join("?" for _ in match_ids)
            row = connection.execute(
                f"""SELECT MAX(artifact.received_at)
                      FROM match_ingest_status AS status
                      JOIN raw_source_artifacts AS artifact
                        ON artifact.artifact_id=status.latest_raw_artifact_id
                     WHERE status.match_id IN ({placeholders})""",
                tuple(match_ids),
            ).fetchone()
        if row is None or row[0] is None:
            raise ValueError("affected maps have no current raw artifact receipt time")
        value = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("raw artifact receipt time must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _verify_derived(
        connection: sqlite3.Connection, match_ids: Collection[int]
    ) -> None:
        for match_id in match_ids:
            role_count = connection.execute(
                """SELECT COUNT(*) FROM player_role_assignments
                    WHERE match_id=? AND purpose='observed_position'
                      AND assignment_version=? AND position BETWEEN 1 AND 5""",
                (match_id, ROLE_VERSION),
            ).fetchone()[0]
            score_count = connection.execute(
                "SELECT COUNT(*) FROM player_map_scores "
                "WHERE match_id=? AND score_version=?",
                (match_id, SCORE_VERSION),
            ).fetchone()[0]
            state_count = connection.execute(
                "SELECT COUNT(*) FROM team_map_states "
                "WHERE match_id=? AND label_version=?",
                (match_id, LABEL_VERSION),
            ).fetchone()[0]
            if (role_count, score_count, state_count) != (10, 10, 2):
                raise RuntimeError(
                    f"derived cardinality mismatch for {match_id}: "
                    f"roles={role_count}, scores={score_count}, states={state_count}"
                )


__all__ = [
    "DerivedRunReport",
    "ROLE_VERSION",
    "SCORE_VERSION",
    "StrictDerivedPipeline",
]
