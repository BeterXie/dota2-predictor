"""Canonical authority checks shared by prospective draft writers and readers."""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from typing import Sequence

from event_intelligence.backtest import draft_dependency_fingerprint
from event_intelligence.draft_artifacts import canonical_hash
from database.session import PostgresSession


OutcomeEvidenceRow = tuple[str, str, str, str, str, str]


@lru_cache(maxsize=32)
def _cached_dependency_fingerprint(
    connection: PostgresSession,
    dependency_revision: int,
) -> str:
    if dependency_revision < 1:
        raise ValueError("draft dependency revision must be positive")
    return draft_dependency_fingerprint(connection)


def draft_dependency_snapshot_reason(
    connection: PostgresSession,
    *,
    expected_revision: int,
    expected_fingerprint: str,
    cutoff: datetime,
) -> str | None:
    """Return why a stored draft dependency snapshot is no longer causal."""

    revision_row = connection.execute(
        """SELECT dependency_revision FROM draft_lineage_revisions
            WHERE singleton=1"""
    ).fetchone()
    if revision_row is None:
        return "draft_dependency_revision_unavailable"
    current_revision = int(revision_row[0])
    if current_revision < expected_revision:
        return "draft_dependency_revision_moved_backwards"
    if current_revision == expected_revision:
        if (
            _cached_dependency_fingerprint(connection, current_revision)
            != expected_fingerprint
        ):
            return "draft_dependency_fingerprint_changed"
        return None
    changes = connection.execute(
        """SELECT COUNT(*), MIN(dependency_revision), MAX(dependency_revision),
                  MAX(CASE
                        WHEN affected_from_unix IS NULL
                          OR affected_from_unix<=? THEN 1 ELSE 0 END)
             FROM draft_lineage_changes
            WHERE dependency_revision>? AND dependency_revision<=?""",
        (int(cutoff.timestamp()), expected_revision, current_revision),
    ).fetchone()
    expected_count = current_revision - expected_revision
    if (
        changes is None
        or int(changes[0]) != expected_count
        or int(changes[1]) != expected_revision + 1
        or int(changes[2]) != current_revision
    ):
        return "draft_dependency_change_log_incomplete"
    if int(changes[3] or 0) == 1:
        return "draft_dependencies_changed_before_cutoff"
    return None


def prospective_outcome_authority(
    *,
    curve_key: str,
    dota_match_id: int,
    winner_side: str,
    radiant_team_side: str,
    map_result_ref: str,
    reconciliation_observed_at: str,
    evidence_rows: Sequence[OutcomeEvidenceRow],
) -> tuple[int, str]:
    """Return the only valid radiant label and hash for exact settled evidence."""

    if winner_side not in {"team_one", "team_two"}:
        raise ValueError("prospective outcome winner side is invalid")
    if radiant_team_side not in {"team_one", "team_two"}:
        raise ValueError("prospective outcome radiant side is invalid")
    if not curve_key or not map_result_ref or not reconciliation_observed_at:
        raise ValueError("prospective outcome identity is incomplete")
    if isinstance(dota_match_id, bool) or not isinstance(dota_match_id, int):
        raise ValueError("prospective outcome match ID is invalid")

    normalized = tuple(sorted(evidence_rows, key=lambda row: row[0]))
    if len(normalized) != 2 or {row[0] for row in normalized} != {
        "opendota",
        "raybet",
    }:
        raise ValueError("prospective outcome requires exact two-source evidence")
    evidence = []
    for (
        source,
        status,
        evidence_winner,
        evidence_ref,
        facts_json,
        observed_at,
    ) in normalized:
        if status != "confirmed" or evidence_winner != winner_side or not evidence_ref:
            raise ValueError("prospective outcome evidence is not confirmed")
        if not observed_at:
            raise ValueError("prospective outcome evidence time is missing")
        try:
            facts = json.loads(facts_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("prospective outcome facts are invalid JSON") from error
        if not isinstance(facts, dict):
            raise ValueError("prospective outcome facts must be an object")
        claimed_winner = facts.get("winner_side")
        if claimed_winner is not None and claimed_winner != winner_side:
            raise ValueError("prospective outcome facts disagree with winner")
        claimed_match = facts.get("dota_match_id")
        if claimed_match is not None and claimed_match != dota_match_id:
            raise ValueError("prospective outcome facts disagree with match")
        evidence.append(
            {
                "source": source,
                "status": status,
                "winner_side": evidence_winner,
                "evidence_ref": evidence_ref,
                "facts_hash": canonical_hash(facts),
                "observed_at": observed_at,
            }
        )
    payload = {
        "curve_key": curve_key,
        "dota_match_id": dota_match_id,
        "winner_side": winner_side,
        "map_result_ref": map_result_ref,
        "reconciliation_observed_at": reconciliation_observed_at,
        "evidence": evidence,
    }
    return int(winner_side == radiant_team_side), canonical_hash(payload)


__all__ = [
    "OutcomeEvidenceRow",
    "draft_dependency_snapshot_reason",
    "prospective_outcome_authority",
]
