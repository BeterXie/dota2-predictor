"""Causal strict-scope walk-forward evaluation for draft models."""

from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .draft_features import (
    AvailabilityMode,
    DerivedFactProvenance,
    DraftFeatureSnapshot,
    DraftHeroMapEvidence,
    DraftMapEvidence,
    DraftPlayer,
    DraftStyleRateSnapshot,
    DraftStyleSnapshot,
    DraftTarget,
    DraftTeam,
    DraftTeamMapEvidence,
    ExpectedRoleAssignment,
    build_draft_feature_snapshot,
)
from .draft_model import (
    DEFAULT_L2_REGULARIZATION,
    DEFAULT_MIN_SAMPLES,
    DraftTrainingRow,
    FeatureSchema,
    _equal_count_calibration_bins,
    evaluate_binary_predictions,
    fit_draft_model,
    passes_calibration_gate,
    predict_draft,
)
from .player_scoring import score_version_for_role
from .raw_archive import canonical_json_bytes
from .models import RolePurpose
from .roles import RoleSource
from .team_profiles import (
    CLOSEOUT_5K_RATE,
    PROFILE_VERSION,
    AvailabilityMode as ProfileAvailabilityMode,
    ProfileMap,
    build_team_style_profile,
    comeback_metric,
    derive_causal_event_patch_priors,
    throw_metric,
)
from .team_states import (
    LABEL_VERSION,
    CurveCrossing,
    ObjectiveConversionFacts,
    Side,
    TeamMapState,
    TeamStateLabel,
    ThresholdFacts,
)


UTC = timezone.utc
HORIZONS = (10, 20, 30, 40, 50)
MODEL_KINDS = ("pure_draft", "context_adjusted")
BACKTEST_VERSION = "strict-draft-walk-forward-v1"
BOOTSTRAP_SAMPLES = 1_000
CALIBRATION_BINS = 5
DRAFT_VALIDATION_VERSION = "draft-input-lineage-v4"


class DraftDependencyLimitError(RuntimeError):
    """Raised before runtime materializes an oversized draft dependency set."""


def _qualified_columns(alias: str, columns: Sequence[str]) -> str:
    return ", ".join(f"{alias}.{column}" for column in columns)


_CORPUS_FACT_COLUMNS = (
    "fact_id", "match_id", "player_slot", "account_id", "team_id",
    "hero_id", "is_radiant", "facts_json", "missing_fields_json", "coverage",
    "source_artifact_id", "source_content_hash", "fact_version",
    "first_usable_at", "created_at",
)
_CORPUS_ROLE_COLUMNS = (
    "assignment_id", "match_id", "player_slot", "account_id", "team_id",
    "purpose", "position", "assignment_source", "confidence", "input_cutoff",
    "input_hash", "assignment_version", "created_at",
)
_CORPUS_SCORE_COLUMNS = (
    "score_id", "match_id", "player_slot", "account_id", "position",
    "execution_score", "result_adjusted_score", "component_facts_json",
    "component_scores_json", "weights_json", "coverage", "role_confidence",
    "benchmark_cutoff", "benchmark_hash", "input_hash", "score_version",
    "explanation_json", "created_at",
)
_CORPUS_PLAYER_COLUMNS = (
    "id", "match_id", "account_id", "player_slot", "hero_id", "is_radiant",
    "team_id", "kills", "deaths", "assists", "gold_per_min", "xp_per_min",
    "net_worth", "last_hits", "denies", "hero_damage", "hero_healing",
    "tower_damage", "level", "item_0", "item_1", "item_2", "item_3", "item_4",
    "item_5", "backpack_0", "backpack_1", "backpack_2", "item_neutral",
    "firstblood_claimed", "gold_10min", "lh_10min", "xp_10min", "kills_10min",
    "deaths_10min", "assists_10min", "obs_placed_10min", "sen_placed_10min",
    "lane_efficiency", "lane_role", "is_roaming", "kda",
    "observer_kills_10min", "sentry_kills_10min",
)
_CORPUS_PICK_COLUMNS = (
    "id", "match_id", "hero_id", "is_pick", "team", "ord",
)
_CORPUS_STATE_COLUMNS = (
    "state_id", "match_id", "team_id", "side", "label", "duration_seconds",
    "max_lead", "max_deficit", "ahead_fraction", "behind_fraction",
    "even_fraction", "signed_auc", "absolute_auc", "crossings_json",
    "first_significant_lead_at", "first_significant_deficit_at",
    "closeout_seconds", "objective_conversion_json", "curve_coverage",
    "source_versions_json", "input_hash", "label_version", "created_at",
)
_CORPUS_FACT_QUERY = f"""SELECT {_qualified_columns('facts', _CORPUS_FACT_COLUMNS)}
   FROM formal_map_eligibility AS eligible
   JOIN match_ingest_status AS status ON status.match_id=eligible.match_id
   JOIN player_map_facts AS facts
     ON facts.match_id=eligible.match_id
    AND facts.source_content_hash=status.latest_raw_content_hash
    AND facts.fact_version=status.normalizer_version || ':' ||
                           status.latest_raw_content_hash
   WHERE eligible.draft_readiness='ready'
   ORDER BY facts.match_id, facts.player_slot"""
_CORPUS_ROLE_QUERY = f"""SELECT {_qualified_columns('roles', _CORPUS_ROLE_COLUMNS)}
   FROM player_role_assignments AS roles
   JOIN formal_map_eligibility AS eligible
     ON eligible.match_id=roles.match_id
   WHERE eligible.draft_readiness='ready'
     AND roles.assignment_version=?
     AND roles.purpose IN ('expected_position', 'observed_position')"""
_CORPUS_SCORE_QUERY = f"""SELECT {_qualified_columns('score', _CORPUS_SCORE_COLUMNS)}
   FROM player_map_scores AS score
   JOIN formal_map_eligibility AS eligible
     ON eligible.match_id=score.match_id
   WHERE eligible.draft_readiness='ready' AND score.score_version=?"""
_CORPUS_PLAYER_QUERY = f"""SELECT {_qualified_columns('player', _CORPUS_PLAYER_COLUMNS)}
   FROM formal_map_eligibility AS eligible
   JOIN match_players AS player ON player.match_id=eligible.match_id
   WHERE eligible.draft_readiness='ready'
   ORDER BY player.match_id, player.player_slot"""
_CORPUS_PICK_QUERY = f"""SELECT {_qualified_columns('pick', _CORPUS_PICK_COLUMNS)}
   FROM formal_map_eligibility AS eligible
   JOIN picks_bans AS pick ON pick.match_id=eligible.match_id
   WHERE eligible.draft_readiness='ready' AND pick.is_pick=1
   ORDER BY pick.match_id, pick.ord, pick.id"""
_CORPUS_STATE_QUERY = f"""SELECT {_qualified_columns('state', _CORPUS_STATE_COLUMNS)}
   FROM team_map_states AS state
   JOIN formal_map_eligibility AS eligible
     ON eligible.match_id=state.match_id
   WHERE eligible.draft_readiness='ready' AND state.label_version=?"""

_DRAFT_DEPENDENCY_QUERIES = (
    (
        "formal_events",
        """SELECT event_id, canonical_name, main_event_start_at
             FROM formal_events""",
    ),
    (
        "formal_map_eligibility",
        """SELECT eligible.match_id, eligible.event_id,
                  eligible.draft_readiness
             FROM formal_map_eligibility AS eligible
             WHERE eligible.draft_readiness='ready'""",
    ),
    (
        "match_ingest_status",
        """SELECT status.match_id, status.event_id, status.series_id,
                  status.map_number, status.normalizer_version,
                  status.latest_raw_artifact_id,
                  status.latest_raw_content_hash
             FROM match_ingest_status AS status
             JOIN formal_map_eligibility AS eligible USING(match_id)
            WHERE eligible.draft_readiness='ready'""",
    ),
    (
        "matches",
        """SELECT match.match_id, match.start_time, match.duration,
                  match.radiant_win, match.radiant_team_id,
                  match.dire_team_id, match.patch
             FROM matches AS match
             JOIN formal_map_eligibility AS eligible USING(match_id)
            WHERE eligible.draft_readiness='ready'""",
    ),
    (
        "raw_source_artifacts",
        """SELECT artifact.artifact_id, artifact.content_hash,
                  artifact.source, artifact.first_usable_at
             FROM raw_source_artifacts AS artifact
             JOIN match_ingest_status AS status
               ON status.latest_raw_artifact_id=artifact.artifact_id
              AND status.latest_raw_content_hash=artifact.content_hash
             JOIN formal_map_eligibility AS eligible
               ON eligible.match_id=status.match_id
            WHERE eligible.draft_readiness='ready'""",
    ),
    (
        "raw_source_observations",
        """SELECT observation.observation_id, observation.artifact_id,
                  observation.content_hash, observation.first_usable_at
             FROM raw_source_observations AS observation
             JOIN match_ingest_status AS status
               ON status.latest_raw_artifact_id=observation.artifact_id
              AND status.latest_raw_content_hash=observation.content_hash
             JOIN formal_map_eligibility AS eligible
               ON eligible.match_id=status.match_id
            WHERE eligible.draft_readiness='ready'""",
    ),
    (
        "player_map_facts",
        """SELECT facts.match_id, facts.player_slot, facts.account_id,
                  facts.team_id, facts.hero_id, facts.is_radiant,
                  facts.facts_json, facts.source_content_hash,
                  facts.fact_version, facts.first_usable_at
             FROM player_map_facts AS facts
             JOIN formal_map_eligibility AS eligible USING(match_id)
            WHERE eligible.draft_readiness='ready'""",
    ),
    (
        "match_players",
        """SELECT player.match_id, player.account_id, player.player_slot,
                  player.hero_id, player.is_radiant, player.team_id
             FROM match_players AS player
             JOIN formal_map_eligibility AS eligible USING(match_id)
            WHERE eligible.draft_readiness='ready'""",
    ),
    (
        "picks_bans",
        """SELECT pick.id, pick.match_id, pick.hero_id, pick.is_pick,
                  pick.team, pick.ord
             FROM picks_bans AS pick
             JOIN formal_map_eligibility AS eligible USING(match_id)
            WHERE eligible.draft_readiness='ready'""",
    ),
    (
        "player_role_assignments",
        """SELECT role.match_id, role.player_slot, role.account_id,
                  role.team_id, role.purpose, role.position,
                  role.assignment_source, role.confidence, role.input_cutoff,
                  role.input_hash, role.assignment_version, role.created_at
             FROM player_role_assignments AS role
             JOIN formal_map_eligibility AS eligible USING(match_id)
            WHERE eligible.draft_readiness='ready'""",
    ),
    (
        "player_map_scores",
        """SELECT score.match_id, score.player_slot, score.execution_score,
                  score.benchmark_cutoff, score.input_hash,
                  score.score_version, score.created_at
             FROM player_map_scores AS score
             JOIN formal_map_eligibility AS eligible USING(match_id)
            WHERE eligible.draft_readiness='ready'""",
    ),
    (
        "team_map_states",
        """SELECT state.match_id, state.side, state.label,
                  state.duration_seconds, state.max_lead, state.max_deficit,
                  state.ahead_fraction, state.behind_fraction,
                  state.even_fraction, state.signed_auc, state.absolute_auc,
                  state.crossings_json, state.first_significant_lead_at,
                  state.first_significant_deficit_at, state.closeout_seconds,
                  state.objective_conversion_json, state.curve_coverage,
                  state.source_versions_json, state.input_hash,
                  state.label_version, state.created_at
             FROM team_map_states AS state
             JOIN formal_map_eligibility AS eligible USING(match_id)
            WHERE eligible.draft_readiness='ready'""",
    ),
)
_DRAFT_DEPENDENCY_TABLES = (
    "event_registry",
    *(
        relation
        for relation, _ in _DRAFT_DEPENDENCY_QUERIES
        if relation not in {"formal_events", "formal_map_eligibility"}
    ),
)
_DRAFT_ARTIFACT_TABLES = ("draft_model_runs", "draft_predictions")
_DRAFT_TRACKED_COLUMNS: dict[str, tuple[str, ...]] = {
    "event_registry": (
        "event_id",
        "canonical_name",
        "tier",
        "prize_pool_usd",
        "main_event_start_at",
        "scope_policy_version",
        "scope",
        "evidence_status",
        "approval_status",
        "included_stages_json",
        "excluded_categories_json",
        "include_internal_lcq",
        "excludes_qualifiers",
        "excludes_division_2",
        "excludes_exhibitions",
        "excludes_forfeits",
        "excludes_void_remakes",
    ),
    "match_ingest_status": (
        "match_id",
        "event_id",
        "series_id",
        "map_number",
        "stage_scope",
        "stage_in_scope",
        "has_valid_result",
        "is_exhibition",
        "is_forfeit",
        "is_void_remake",
        "draft_readiness",
        "latest_raw_artifact_id",
        "latest_raw_content_hash",
        "normalizer_version",
    ),
    "matches": (
        "match_id",
        "start_time",
        "duration",
        "radiant_win",
        "radiant_team_id",
        "dire_team_id",
        "patch",
    ),
    "raw_source_artifacts": (
        "artifact_id",
        "content_hash",
        "source",
        "first_usable_at",
    ),
    "raw_source_observations": (
        "observation_id",
        "artifact_id",
        "content_hash",
        "first_usable_at",
    ),
    "match_players": (
        "match_id",
        "account_id",
        "player_slot",
        "hero_id",
        "is_radiant",
        "team_id",
    ),
    "picks_bans": ("id", "match_id", "hero_id", "is_pick", "team", "ord"),
    "player_map_facts": (
        "match_id",
        "player_slot",
        "account_id",
        "team_id",
        "hero_id",
        "is_radiant",
        "facts_json",
        "source_content_hash",
        "fact_version",
        "first_usable_at",
    ),
    "player_role_assignments": (
        "match_id",
        "player_slot",
        "account_id",
        "team_id",
        "purpose",
        "position",
        "assignment_source",
        "confidence",
        "input_cutoff",
        "input_hash",
        "assignment_version",
        "created_at",
    ),
    "player_map_scores": (
        "match_id",
        "player_slot",
        "execution_score",
        "benchmark_cutoff",
        "input_hash",
        "score_version",
        "created_at",
    ),
    "team_map_states": (
        "match_id",
        "side",
        "label",
        "duration_seconds",
        "max_lead",
        "max_deficit",
        "ahead_fraction",
        "behind_fraction",
        "even_fraction",
        "signed_auc",
        "absolute_auc",
        "crossings_json",
        "first_significant_lead_at",
        "first_significant_deficit_at",
        "closeout_seconds",
        "objective_conversion_json",
        "curve_coverage",
        "source_versions_json",
        "input_hash",
        "label_version",
        "created_at",
    ),
}
_DRAFT_TRACKING_RELATIONS = frozenset(
    {
        *_DRAFT_DEPENDENCY_TABLES,
        *_DRAFT_ARTIFACT_TABLES,
        "draft_lineage_changes",
        "draft_lineage_revisions",
        "draft_prediction_validations",
        "formal_events",
        "formal_map_eligibility",
    }
)
_DRAFT_VIEW_DEFINITIONS = {
    "formal_events": """CREATE VIEW formal_events AS
SELECT *
FROM event_registry
WHERE scope = 'formal_main_event'
  AND approval_status = 'approved'
  AND evidence_status = 'manually_audited'
  AND tier = 'tier_1'
  AND prize_pool_usd >= 1000000""",
    "formal_map_eligibility": """CREATE VIEW formal_map_eligibility AS
SELECT
    m.match_id,
    m.event_id,
    e.opendota_league_id,
    m.stage_scope,
    m.ingest_state,
    m.player_readiness,
    m.state_readiness,
    m.draft_readiness
FROM match_ingest_status AS m
JOIN formal_events AS e ON e.event_id = m.event_id
WHERE m.stage_in_scope = 1
  AND m.has_valid_result = 1
  AND m.is_exhibition = 0
  AND m.is_forfeit = 0
  AND m.is_void_remake = 0
  AND (
      m.stage_scope = 'main_event'
      OR (m.stage_scope = 'internal_lcq' AND e.include_internal_lcq = 1)
    )""",
}
_DRAFT_REVISION_TABLE_SQL = """CREATE TABLE draft_lineage_revisions (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    dependency_revision INTEGER NOT NULL CHECK (dependency_revision >= 1),
    artifact_revision INTEGER NOT NULL CHECK (artifact_revision >= 1),
    updated_at TEXT NOT NULL
)"""
_DRAFT_CHANGE_TABLE_SQL = """CREATE TABLE draft_lineage_changes (
    dependency_revision INTEGER PRIMARY KEY CHECK (dependency_revision >= 1),
    affected_from_unix INTEGER
        CHECK (affected_from_unix IS NULL OR affected_from_unix > 0),
    source_relation TEXT NOT NULL,
    operation TEXT NOT NULL
        CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE', 'REPAIR', 'INITIALIZE')),
    changed_at TEXT NOT NULL
)"""
_DRAFT_CHANGE_GUARD_TRIGGERS = {
    "draft_lineage_changes_no_update": """CREATE TRIGGER "draft_lineage_changes_no_update"
BEFORE UPDATE ON draft_lineage_changes
BEGIN
  SELECT RAISE(ABORT, 'draft lineage changes are immutable');
END""",
    "draft_lineage_changes_no_delete": """CREATE TRIGGER "draft_lineage_changes_no_delete"
BEFORE DELETE ON draft_lineage_changes
BEGIN
  SELECT RAISE(ABORT, 'draft lineage changes are immutable');
END""",
    "draft_lineage_changes_append_only": """CREATE TRIGGER "draft_lineage_changes_append_only"
BEFORE INSERT ON draft_lineage_changes
WHEN EXISTS (
    SELECT 1 FROM draft_lineage_changes AS existing
     WHERE existing.dependency_revision=NEW.dependency_revision
) OR NEW.dependency_revision IS NOT (
    SELECT dependency_revision FROM draft_lineage_revisions WHERE singleton=1
)
BEGIN
  SELECT RAISE(ABORT, 'draft lineage changes are append-only');
END""",
}
_DRAFT_NO_IMPACT_UNIX = 9_223_372_036_854_775_807


def _normalized_sql(value: object) -> str:
    return " ".join(str(value or "").split())


def _drop_schema_relation(connection: sqlite3.Connection, name: str) -> None:
    row = connection.execute(
        """SELECT type FROM sqlite_master
             WHERE type IN ('table', 'view') AND name=?""",
        (name,),
    ).fetchone()
    if row is not None:
        kind = str(row[0]).upper()
        connection.execute(f'DROP {kind} "{name}"')


def _draft_views_are_current(connection: sqlite3.Connection) -> bool:
    installed = {
        str(row[0]): _normalized_sql(row[1])
        for row in connection.execute(
            """SELECT name, sql FROM sqlite_master
                 WHERE type='view' AND name IN
                       ('formal_events', 'formal_map_eligibility')"""
        )
    }
    return all(
        installed.get(name) == _normalized_sql(sql)
        for name, sql in _DRAFT_VIEW_DEFINITIONS.items()
    )


def _draft_revision_state_is_current(connection: sqlite3.Connection) -> bool:
    schemas = {
        str(row[0]): _normalized_sql(row[1])
        for row in connection.execute(
            """SELECT name, sql FROM sqlite_master
                 WHERE type='table' AND name IN
                       ('draft_lineage_revisions', 'draft_lineage_changes')"""
        )
    }
    if (
        schemas.get("draft_lineage_revisions")
        != _normalized_sql(_DRAFT_REVISION_TABLE_SQL)
        or schemas.get("draft_lineage_changes")
        != _normalized_sql(_DRAFT_CHANGE_TABLE_SQL)
    ):
        return False
    try:
        revisions = connection.execute(
            """SELECT singleton, dependency_revision, artifact_revision
                 FROM draft_lineage_revisions"""
        )
        revision = revisions.fetchone()
        extra_revision = revisions.fetchone()
        if not (
            revision is not None
            and extra_revision is None
            and type(revision[0]) is int
            and int(revision[0]) == 1
            and type(revision[1]) is int
            and int(revision[1]) >= 1
            and type(revision[2]) is int
            and int(revision[2]) >= 1
        ):
            return False
        dependency_revision = int(revision[1])
        changes = connection.execute(
            """SELECT dependency_revision, affected_from_unix,
                      source_relation, operation, changed_at
                 FROM draft_lineage_changes
                ORDER BY dependency_revision"""
        )
        first_change: tuple[Any, ...] | None = None
        change_count = 0
        for expected_revision, raw in enumerate(changes, start=1):
            row = tuple(raw)
            if first_change is None:
                first_change = row
            affected_from = row[1]
            if (
                type(row[0]) is not int
                or int(row[0]) != expected_revision
                or (
                    affected_from is not None
                    and (type(affected_from) is not int or int(affected_from) <= 0)
                )
                or not isinstance(row[2], str)
                or not str(row[2])
                or row[3]
                not in {"INSERT", "UPDATE", "DELETE", "REPAIR", "INITIALIZE"}
                or not isinstance(row[4], str)
                or not str(row[4])
            ):
                return False
            change_count = expected_revision
    except sqlite3.Error:
        return False
    return (
        change_count == dependency_revision
        and first_change is not None
        and first_change[1] is None
        and first_change[2] == "__tracking__"
        and first_change[3] == "INITIALIZE"
    )


def _draft_trigger_name(kind: str, table: str, operation: str) -> str:
    return f"draft_lineage_{kind}_{table}_{operation.lower()}"


def draft_lineage_trigger_names() -> frozenset[str]:
    return frozenset(_DRAFT_CHANGE_GUARD_TRIGGERS) | frozenset(
        _draft_trigger_name(kind, table, operation)
        for kind, tables in (
            ("dependency", _DRAFT_DEPENDENCY_TABLES),
            ("artifact", _DRAFT_ARTIFACT_TABLES),
        )
        for table in tables
        for operation in ("INSERT", "UPDATE", "DELETE")
    )


def _draft_trigger_scope(table: str, operation: str) -> str:
    def formal_event(reference: str) -> str:
        return (
            f"({reference}.scope='formal_main_event' "
            f"AND {reference}.approval_status='approved' "
            f"AND {reference}.evidence_status='manually_audited' "
            f"AND {reference}.tier='tier_1' "
            f"AND {reference}.prize_pool_usd>=1000000)"
        )

    def formal_status(reference: str) -> str:
        return (
            "EXISTS (SELECT 1 FROM event_registry AS event "
            f"WHERE event.event_id={reference}.event_id "
            "AND event.scope='formal_main_event' "
            "AND event.approval_status='approved' "
            "AND event.evidence_status='manually_audited' "
            "AND event.tier='tier_1' AND event.prize_pool_usd>=1000000 "
            f"AND {reference}.stage_in_scope=1 "
            f"AND {reference}.has_valid_result=1 "
            f"AND {reference}.is_exhibition=0 "
            f"AND {reference}.is_forfeit=0 "
            f"AND {reference}.is_void_remake=0 "
            f"AND {reference}.draft_readiness='ready' "
            f"AND ({reference}.stage_scope='main_event' OR "
            f"({reference}.stage_scope='internal_lcq' "
            "AND event.include_internal_lcq=1)))"
        )

    def eligible(reference: str) -> str:
        return (
            "EXISTS (SELECT 1 FROM formal_map_eligibility AS eligible "
            f"WHERE eligible.match_id={reference}.match_id "
            "AND eligible.draft_readiness='ready')"
        )

    def current_raw(reference: str) -> str:
        return (
            "EXISTS (SELECT 1 FROM match_ingest_status AS status "
            "JOIN formal_map_eligibility AS eligible "
            "ON eligible.match_id=status.match_id "
            f"WHERE status.latest_raw_artifact_id={reference}.artifact_id "
            f"AND status.latest_raw_content_hash={reference}.content_hash "
            "AND eligible.draft_readiness='ready')"
        )

    if table in _DRAFT_ARTIFACT_TABLES:
        return "1"
    if table == "event_registry":
        scoped = formal_event
    elif table == "match_ingest_status":
        scoped = formal_status
    elif table in {"raw_source_artifacts", "raw_source_observations"}:
        scoped = current_raw
    else:
        scoped = eligible
    if operation == "INSERT":
        return scoped("NEW")
    if operation == "DELETE":
        return scoped("OLD")
    return f"({scoped('OLD')} OR {scoped('NEW')})"


def _draft_change_impact(table: str, reference: str) -> str:
    def valid_start(value: str) -> str:
        return (
            f"(CASE WHEN typeof({value})='integer' AND {value}>0 "
            f"THEN {value} END)"
        )

    def match_cutoff(match_id: str, stored_start: str) -> str:
        return (
            "(SELECT MIN(candidate.affected_from) FROM ("
            f"SELECT {valid_start(stored_start)} AS affected_from "
            "UNION ALL "
            "SELECT CAST(strftime('%s', prediction.prediction_cutoff) AS INTEGER) "
            "FROM draft_predictions AS prediction "
            f"WHERE prediction.match_id={match_id} "
            "AND CAST(strftime('%s', prediction.prediction_cutoff) AS INTEGER)>0"
            ") AS candidate WHERE candidate.affected_from>0)"
        )

    def status_matches_cutoff(predicate: str) -> str:
        return (
            "(SELECT MIN(candidate.affected_from) FROM ("
            "SELECT match.start_time AS affected_from "
            "FROM match_ingest_status AS status "
            "JOIN matches AS match ON match.match_id=status.match_id "
            f"WHERE {predicate} AND typeof(match.start_time)='integer' "
            "AND match.start_time>0 "
            "UNION ALL "
            "SELECT CAST(strftime('%s', prediction.prediction_cutoff) AS INTEGER) "
            "FROM match_ingest_status AS status "
            "JOIN draft_predictions AS prediction "
            "ON prediction.match_id=status.match_id "
            f"WHERE {predicate} AND CAST(strftime('%s', "
            "prediction.prediction_cutoff) AS INTEGER)>0"
            ") AS candidate WHERE candidate.affected_from>0)"
        )

    if table == "event_registry":
        related = status_matches_cutoff(
            f"status.event_id={reference}.event_id "
            "AND status.draft_readiness='ready' "
            "AND status.stage_in_scope=1 "
            "AND status.has_valid_result=1 "
            "AND status.is_exhibition=0 "
            "AND status.is_forfeit=0 "
            "AND status.is_void_remake=0 "
            "AND status.stage_scope IN ('main_event', 'internal_lcq')"
        )
        return f"COALESCE({related}, {_DRAFT_NO_IMPACT_UNIX})"
    if table == "matches":
        return match_cutoff(
            f"{reference}.match_id",
            f"{reference}.start_time",
        )
    if table == "match_ingest_status":
        stored_start = (
            "(SELECT match.start_time FROM matches AS match "
            f"WHERE match.match_id={reference}.match_id "
            "AND typeof(match.start_time)='integer' AND match.start_time>0)"
        )
        return match_cutoff(
            f"{reference}.match_id",
            f"COALESCE({stored_start}, {valid_start(f'{reference}.start_time')})",
        )
    if table in {"raw_source_artifacts", "raw_source_observations"}:
        return status_matches_cutoff(
            f"status.latest_raw_artifact_id={reference}.artifact_id "
            f"AND status.latest_raw_content_hash={reference}.content_hash"
        )
    stored_start = (
        "(SELECT match.start_time FROM matches AS match "
        f"WHERE match.match_id={reference}.match_id "
        "AND typeof(match.start_time)='integer' AND match.start_time>0)"
    )
    return match_cutoff(f"{reference}.match_id", stored_start)


def _draft_trigger_impact(table: str, operation: str) -> str:
    if operation == "INSERT":
        return _draft_change_impact(table, "NEW")
    if operation == "DELETE":
        return _draft_change_impact(table, "OLD")
    old_scope = _draft_trigger_scope(table, "DELETE")
    new_scope = _draft_trigger_scope(table, "INSERT")
    old_impact = _draft_change_impact(table, "OLD")
    new_impact = _draft_change_impact(table, "NEW")
    return (
        f"CASE WHEN ({old_scope}) AND ({new_scope}) THEN "
        f"CASE WHEN ({old_impact}) IS NULL OR ({new_impact}) IS NULL "
        f"THEN NULL ELSE min(({old_impact}), ({new_impact})) END "
        f"WHEN ({old_scope}) THEN ({old_impact}) ELSE ({new_impact}) END"
    )


def _draft_trigger_definitions(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, str]]:
    definitions: dict[str, tuple[str, str]] = {}
    for kind, tables, revision_column in (
        ("dependency", _DRAFT_DEPENDENCY_TABLES, "dependency_revision"),
        ("artifact", _DRAFT_ARTIFACT_TABLES, "artifact_revision"),
    ):
        for table in tables:
            available_columns = tuple(
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                )
            )
            columns = _DRAFT_TRACKED_COLUMNS.get(table, available_columns)
            if not available_columns or not set(columns).issubset(available_columns):
                raise RuntimeError(f"draft lineage table has no columns: {table}")
            changed = " OR ".join(
                f'OLD."{column}" IS NOT NEW."{column}"' for column in columns
            )
            for operation in ("INSERT", "UPDATE", "DELETE"):
                condition = _draft_trigger_scope(table, operation)
                if operation == "UPDATE":
                    condition = f"({condition}) AND ({changed})"
                name = _draft_trigger_name(kind, table, operation)
                impact = (
                    _draft_trigger_impact(table, operation)
                    if kind == "dependency"
                    else None
                )
                change_insert = (
                    ""
                    if impact is None
                    else f"""
                           INSERT INTO draft_lineage_changes
                               (dependency_revision, affected_from_unix,
                                source_relation, operation, changed_at)
                           SELECT dependency_revision, ({impact}),
                                  '{table}', '{operation}', updated_at
                             FROM draft_lineage_revisions
                            WHERE singleton=1;"""
                )
                definitions[name] = (
                    revision_column,
                    f'''CREATE TRIGGER "{name}"
                         AFTER {operation} ON "{table}"
                         WHEN {condition}
                         BEGIN
                           UPDATE draft_lineage_revisions
                              SET {revision_column}={revision_column}+1,
                                  updated_at=strftime(
                                      '%Y-%m-%dT%H:%M:%fZ', 'now'
                                  )
                            WHERE singleton=1;
                           {change_insert}
                         END''',
                )
    return definitions


def _advance_draft_revision(
    connection: sqlite3.Connection,
    revision_column: str,
) -> None:
    connection.execute(
        f"""UPDATE draft_lineage_revisions
               SET {revision_column}={revision_column}+1,
                   updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE singleton=1"""
    )
    if revision_column == "dependency_revision":
        connection.execute(
            """INSERT INTO draft_lineage_changes
                   (dependency_revision, affected_from_unix,
                    source_relation, operation, changed_at)
               SELECT dependency_revision, NULL, '__schema__', 'REPAIR', updated_at
                 FROM draft_lineage_revisions
                WHERE singleton=1"""
        )


def ensure_draft_lineage_tracking(connection: sqlite3.Connection) -> None:
    """Install narrow revision triggers once all cross-owned tables exist."""
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        existing = {
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                     WHERE type IN ('table', 'view')"""
            )
        }
        repairable = {
            "draft_lineage_changes",
            "draft_lineage_revisions",
            "formal_events",
            "formal_map_eligibility",
        }
        missing = sorted((_DRAFT_TRACKING_RELATIONS - repairable) - existing)
        if missing:
            raise RuntimeError(f"draft lineage tracking lacks relations: {missing}")

        revision_repaired = not _draft_revision_state_is_current(connection)
        if revision_repaired:
            connection.execute("DELETE FROM draft_prediction_validations")
            for name in draft_lineage_trigger_names():
                connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
            _drop_schema_relation(connection, "draft_lineage_changes")
            _drop_schema_relation(connection, "draft_lineage_revisions")
            connection.execute(_DRAFT_REVISION_TABLE_SQL)
            connection.execute(_DRAFT_CHANGE_TABLE_SQL)
            connection.execute(
                """INSERT INTO draft_lineage_revisions
                   (singleton, dependency_revision, artifact_revision, updated_at)
                   VALUES (1, 1, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"""
            )
            connection.execute(
                """INSERT INTO draft_lineage_changes
                       (dependency_revision, affected_from_unix,
                        source_relation, operation, changed_at)
                   SELECT 1, NULL, '__tracking__', 'INITIALIZE', updated_at
                     FROM draft_lineage_revisions WHERE singleton=1"""
            )

        views_repaired = not _draft_views_are_current(connection)
        if views_repaired:
            _drop_schema_relation(connection, "formal_map_eligibility")
            _drop_schema_relation(connection, "formal_events")
            connection.execute(_DRAFT_VIEW_DEFINITIONS["formal_events"])
            connection.execute(_DRAFT_VIEW_DEFINITIONS["formal_map_eligibility"])

        definitions = _draft_trigger_definitions(connection)
        installed = {
            str(row[0]): "" if row[1] is None else str(row[1]).strip()
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            )
        }
        repaired_revisions = (
            {"dependency_revision"} if views_repaired else set()
        )
        for name, (revision_column, sql) in definitions.items():
            if installed.get(name) == sql.strip():
                continue
            connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
            connection.execute(sql)
            repaired_revisions.add(revision_column)
        for name, sql in _DRAFT_CHANGE_GUARD_TRIGGERS.items():
            if installed.get(name) == sql.strip():
                continue
            connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
            connection.execute(sql)
            repaired_revisions.add("dependency_revision")
        for revision_column in sorted(repaired_revisions):
            _advance_draft_revision(connection, revision_column)
        if owns_transaction:
            connection.commit()
    except BaseException:
        if owns_transaction:
            connection.rollback()
        raise


def draft_lineage_tracking_is_current(connection: sqlite3.Connection) -> bool:
    try:
        existing = {
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                     WHERE type IN ('table', 'view')"""
            )
        }
        if not _DRAFT_TRACKING_RELATIONS.issubset(existing):
            return False
        if not _draft_revision_state_is_current(connection):
            return False
        if not _draft_views_are_current(connection):
            return False
        definitions = _draft_trigger_definitions(connection)
        installed = {
            str(row[0]): "" if row[1] is None else str(row[1]).strip()
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            )
        }
    except (RuntimeError, sqlite3.Error):
        return False
    return all(
        installed.get(name) == sql.strip()
        for name, (_, sql) in definitions.items()
    ) and all(
        installed.get(name) == sql.strip()
        for name, sql in _DRAFT_CHANGE_GUARD_TRIGGERS.items()
    )

_DRAFT_ARTIFACT_FIELDS = (
    "run_id",
    "model_version",
    "model_kind",
    "horizon_minutes",
    "availability_mode",
    "training_cutoff",
    "feature_schema_hash",
    "configuration_json",
    "metrics_json",
    "run_status",
    "match_id",
    "prediction_cutoff",
    "cutoff_source",
    "input_snapshot_hash",
    "probability",
    "uncertainty",
    "support",
    "eventual_radiant_win",
    "prediction_status",
)


def _fingerprint_value(value: object) -> bytes:
    if value is None:
        return b"N"
    if isinstance(value, bytes):
        return b"B" + value
    if isinstance(value, int):
        return b"I" + str(value).encode("ascii")
    if isinstance(value, float):
        return b"F" + value.hex().encode("ascii")
    if isinstance(value, str):
        return b"S" + value.encode("utf-8")
    raise TypeError(f"unsupported SQLite value for draft fingerprint: {type(value)!r}")


def _fingerprint_row(row: Sequence[object]) -> bytes:
    encoded = bytearray()
    for value in row:
        field = _fingerprint_value(value)
        encoded.extend(len(field).to_bytes(8, "big"))
        encoded.extend(field)
    return bytes(encoded)


def draft_prediction_artifact_fingerprint(values: Mapping[str, object]) -> str:
    payload = _fingerprint_row(
        (*_DRAFT_ARTIFACT_FIELDS, *(values[field] for field in _DRAFT_ARTIFACT_FIELDS))
    )
    return hashlib.sha256(payload).hexdigest()


def draft_prediction_artifacts(
    connection: sqlite3.Connection,
) -> dict[tuple[str, int], tuple[str, str]]:
    rows = connection.execute(
        """SELECT run.run_id, run.model_version, run.model_kind,
                  run.horizon_minutes, run.availability_mode,
                  run.training_cutoff, run.feature_schema_hash,
                  run.configuration_json, run.metrics_json,
                  run.status AS run_status, prediction.match_id,
                  prediction.prediction_cutoff, prediction.cutoff_source,
                  prediction.input_snapshot_hash, prediction.probability,
                  prediction.uncertainty, prediction.support,
                  prediction.eventual_radiant_win,
                  prediction.status AS prediction_status
             FROM draft_predictions AS prediction
             JOIN draft_model_runs AS run ON run.run_id=prediction.run_id"""
    ).fetchall()
    result: dict[tuple[str, int], tuple[str, str]] = {}
    for row in rows:
        payload = dict(row)
        key = (str(payload["run_id"]), int(payload["match_id"]))
        result[key] = (
            str(payload["input_snapshot_hash"]),
            draft_prediction_artifact_fingerprint(payload),
        )
    return result


def persisted_draft_artifact_fingerprint(row: PersistedRun) -> str:
    return draft_prediction_artifact_fingerprint(
        {
            "run_id": row.run_id,
            "model_version": row.model_version,
            "model_kind": row.model_kind,
            "horizon_minutes": row.horizon_minutes,
            "availability_mode": row.availability_mode,
            "training_cutoff": row.training_cutoff,
            "feature_schema_hash": row.feature_schema_hash,
            "configuration_json": row.configuration_json,
            "metrics_json": row.metrics_json,
            "run_status": row.status,
            "match_id": row.match_id,
            "prediction_cutoff": row.prediction_cutoff,
            "cutoff_source": row.cutoff_source,
            "input_snapshot_hash": row.input_snapshot_hash,
            "probability": row.probability,
            "uncertainty": row.uncertainty,
            "support": row.support,
            "eventual_radiant_win": row.eventual_radiant_win,
            "prediction_status": row.prediction_status,
        }
    )


def _dependency_fingerprint(
    connection: sqlite3.Connection,
    queries: Sequence[tuple[str, str]],
) -> str:
    digest = hashlib.sha256()
    for relation, query in queries:
        cursor = connection.execute(query)
        columns = tuple(str(item[0]) for item in cursor.description or ())
        header = _fingerprint_row((relation, *columns))
        digest.update(len(header).to_bytes(8, "big"))
        digest.update(header)
        rows = sorted(_fingerprint_row(tuple(row)) for row in cursor.fetchall())
        digest.update(len(rows).to_bytes(8, "big"))
        for row in rows:
            digest.update(len(row).to_bytes(8, "big"))
            digest.update(row)
    return digest.hexdigest()


def draft_dependency_fingerprint(connection: sqlite3.Connection) -> str:
    """Hash every current database row that can affect a draft snapshot."""
    return _dependency_fingerprint(connection, _DRAFT_DEPENDENCY_QUERIES)


def _bounded_draft_dependency_usage(
    connection: sqlite3.Connection,
    *,
    max_rows: int,
    max_bytes: int,
    additional_queries: Sequence[
        tuple[str, str, tuple[object, ...]]
    ] = (),
) -> None:
    if max_rows < 1 or max_bytes < 1:
        raise ValueError("draft dependency limits must be positive")
    total_rows = 0
    projections: list[
        tuple[str, str, tuple[object, ...], tuple[str, ...]]
    ] = []
    queries = (
        *((relation, query, ()) for relation, query in _DRAFT_DEPENDENCY_QUERIES),
        *additional_queries,
    )
    for relation, query, params in queries:
        probe = connection.execute(f"{query} LIMIT 0", params)
        columns = tuple(str(item[0]) for item in probe.description or ())
        if not columns:
            raise RuntimeError(f"draft dependency query has no columns: {relation}")
        count_row = connection.execute(
            f"SELECT COUNT(*) FROM ({query})",
            params,
        ).fetchone()
        if count_row is None:
            raise RuntimeError(f"draft dependency count failed: {relation}")
        total_rows += int(count_row[0])
        if total_rows > max_rows:
            raise DraftDependencyLimitError("draft dependency row limit exceeded")
        projections.append((relation, query, params, columns))

    total_bytes = 0
    for relation, query, params, columns in projections:
        byte_terms = "+".join(
            "COALESCE(length(CAST(\""
            + column.replace('"', '""')
            + "\" AS BLOB)),0)"
            for column in columns
        )
        bytes_row = connection.execute(
            f"SELECT COALESCE(SUM({byte_terms}),0) FROM ({query})",
            params,
        ).fetchone()
        if bytes_row is None:
            raise RuntimeError(f"draft dependency byte count failed: {relation}")
        total_bytes += int(bytes_row[0])
        if total_bytes > max_bytes:
            raise DraftDependencyLimitError("draft dependency byte limit exceeded")


def persist_draft_prediction_validations(
    connection: sqlite3.Connection,
    runs: Sequence[PersistedRun],
    *,
    expected_dependency_fingerprint: str,
    expected_dependency_revision: int,
) -> None:
    if (
        not isinstance(expected_dependency_fingerprint, str)
        or len(expected_dependency_fingerprint) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_dependency_fingerprint
        )
    ):
        raise ValueError("expected dependency fingerprint must be lowercase SHA-256")
    if (
        isinstance(expected_dependency_revision, bool)
        or not isinstance(expected_dependency_revision, int)
        or expected_dependency_revision < 1
    ):
        raise ValueError("expected dependency revision must be a positive integer")
    if not runs:
        return
    ensure_draft_lineage_tracking(connection)
    now = datetime.now(UTC).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        if not draft_lineage_tracking_is_current(connection):
            raise RuntimeError("draft lineage tracking changed while runs were rebuilding")
        dependency_fingerprint = draft_dependency_fingerprint(connection)
        revision_row = connection.execute(
            """SELECT dependency_revision FROM draft_lineage_revisions
                 WHERE singleton=1"""
        ).fetchone()
        if revision_row is None:
            raise RuntimeError("draft dependency revision is unavailable")
        dependency_revision = int(revision_row[0])
        if dependency_revision < expected_dependency_revision:
            raise RuntimeError("draft dependency revision moved backwards")
        cutoffs = (
            _parse_utc(row.prediction_cutoff, "prediction cutoff") for row in runs
        )
        cutoff_values = tuple(value for value in cutoffs if value is not None)
        if len(cutoff_values) != len(runs):
            raise RuntimeError("draft run has an invalid prediction cutoff")
        maximum_cutoff_unix = max(int(value.timestamp()) for value in cutoff_values)
        relevant_change = connection.execute(
            """SELECT 1 FROM draft_lineage_changes
                WHERE dependency_revision>?
                  AND (affected_from_unix IS NULL OR affected_from_unix<=?)
                LIMIT 1""",
            (expected_dependency_revision, maximum_cutoff_unix),
        ).fetchone()
        if relevant_change is not None:
            raise RuntimeError("draft dependencies changed while runs were rebuilding")
        if (
            dependency_revision == expected_dependency_revision
            and dependency_fingerprint != expected_dependency_fingerprint
        ):
            raise RuntimeError("draft dependencies changed while runs were rebuilding")
        artifacts = draft_prediction_artifacts(connection)
        if any(
            artifacts.get((row.run_id, row.match_id))
            != (
                row.input_snapshot_hash,
                persisted_draft_artifact_fingerprint(row),
            )
            for row in runs
        ):
            raise RuntimeError("draft artifacts changed while runs were rebuilding")
        connection.executemany(
            """INSERT INTO draft_prediction_validations
               (run_id, match_id, input_snapshot_hash, artifact_fingerprint,
                dependency_fingerprint, dependency_revision,
                validation_version, validated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id, match_id) DO UPDATE SET
                   input_snapshot_hash=excluded.input_snapshot_hash,
                   artifact_fingerprint=excluded.artifact_fingerprint,
                   dependency_fingerprint=excluded.dependency_fingerprint,
                   dependency_revision=excluded.dependency_revision,
                   validation_version=excluded.validation_version,
                   validated_at=excluded.validated_at""",
            (
                (
                    row.run_id,
                    row.match_id,
                    row.input_snapshot_hash,
                    persisted_draft_artifact_fingerprint(row),
                    dependency_fingerprint,
                    dependency_revision,
                    DRAFT_VALIDATION_VERSION,
                    now,
                )
                for row in runs
            ),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


@dataclass(frozen=True)
class LoadedDraftMap:
    """One exact strict map, usable as history and optionally as a target."""

    match_id: int
    series_id: int | None
    event_id: str
    duration_seconds: int
    radiant_win: bool
    prediction_cutoff_source: str | None
    target: DraftTarget | None
    evidence: DraftMapEvidence


@dataclass(frozen=True)
class DraftCorpus:
    assignment_version: str
    score_version: str
    availability_mode: str
    formal_draft_maps: int
    event_order: tuple[EventOrderEntry, ...]
    cold_start_support: int
    maps: tuple[LoadedDraftMap, ...]
    profile_maps: tuple[ProfileMap, ...]

    @property
    def targets(self) -> tuple[LoadedDraftMap, ...]:
        return tuple(row for row in self.maps if row.target is not None)


@dataclass(frozen=True)
class EvaluationPoint:
    match_id: int
    series_id: int | None
    event_id: str
    probability: float
    outcome: bool


@dataclass(frozen=True)
class EventOrderEntry:
    event_id: str
    canonical_name: str
    main_event_start_at: str


@dataclass(frozen=True)
class CalibrationMetrics:
    support: int
    brier_score: float | None
    log_loss: float | None
    ece_5_bin: float | None
    ece_90_upper: float | None
    auc: float | None
    accuracy: float | None
    gate_status: str
    gate_failures: tuple[str, ...]


@dataclass(frozen=True)
class PersistedRun:
    run_id: str
    model_version: str
    model_kind: str
    horizon_minutes: int
    availability_mode: str
    training_cutoff: str
    feature_schema_hash: str
    configuration_json: str
    metrics_json: str
    status: str
    match_id: int
    prediction_cutoff: str
    cutoff_source: str
    input_snapshot_hash: str
    probability: float | None
    uncertainty: float | None
    support: int
    eventual_radiant_win: int
    prediction_status: str


@dataclass(frozen=True)
class PersistenceCounts:
    inserted_runs: int = 0
    unchanged_runs: int = 0
    inserted_predictions: int = 0
    unchanged_predictions: int = 0


@dataclass(frozen=True)
class SliceReport:
    model_kind: str
    horizon_minutes: int
    eligible_targets: int
    predicted: int
    insufficient_evidence: int
    metrics: CalibrationMetrics


@dataclass(frozen=True)
class EventSliceReport:
    event_id: str
    canonical_name: str
    model_kind: str
    horizon_minutes: int
    eligible_targets: int
    predicted: int
    insufficient_evidence: int
    metrics: CalibrationMetrics


@dataclass(frozen=True)
class BacktestReport:
    backtest_version: str
    availability_mode: str
    assignment_version: str
    score_version: str
    dry_run: bool
    formal_draft_maps: int
    cold_start_support: int
    eligible_targets: int
    runs: int
    inserted_runs: int
    unchanged_runs: int
    inserted_predictions: int
    unchanged_predictions: int
    event_order: tuple[EventOrderEntry, ...]
    slices: tuple[SliceReport, ...]
    event_slices: tuple[EventSliceReport, ...]


@dataclass(frozen=True)
class _SnapshotRow:
    game: LoadedDraftMap
    snapshot: DraftFeatureSnapshot


def _parse_utc(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _positive_integer(value: object) -> int | None:
    parsed = _integer(value)
    return parsed if parsed is not None and parsed > 0 else None


def _number(value: object) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        return float(value)
    return None


def _nonnegative_number(value: object) -> float | None:
    parsed = _number(value)
    return parsed if parsed is not None and parsed >= 0.0 else None


def _json_object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise ValueError(f"{field} must contain JSON")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must contain an object")
    return parsed


def _canonical_json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _player_id(match_id: int, account_id: int | None, player_slot: int) -> int:
    if account_id is not None and account_id > 0:
        return account_id
    return -(match_id * 256 + player_slot + 1)


def resolve_assignment_version(
    connection: sqlite3.Connection,
    requested: str | None,
) -> str:
    versions = tuple(
        str(row[0])
        for row in connection.execute(
            """SELECT DISTINCT roles.assignment_version
               FROM player_role_assignments AS roles
               JOIN formal_map_eligibility AS eligible
                 ON eligible.match_id=roles.match_id
               WHERE eligible.draft_readiness='ready'
                 AND roles.purpose='expected_position'
               ORDER BY roles.assignment_version"""
        ).fetchall()
    )
    if requested is not None:
        if requested not in versions:
            raise ValueError(
                f"expected-position assignment version {requested!r} is unavailable"
            )
        return requested
    if len(versions) != 1:
        rendered = ", ".join(versions) if versions else "none"
        raise ValueError(
            "--assignment-version is required unless exactly one expected-position "
            f"version is available; found: {rendered}"
        )
    return versions[0]


def _validate_assignment_mode(
    assignment_version: str, availability_mode: AvailabilityMode
) -> None:
    expected_suffix = {
        AvailabilityMode.RECONSTRUCTED: "-reconstructed-walk-forward",
        AvailabilityMode.PROSPECTIVE: "-prospective",
    }[availability_mode]
    if not assignment_version.endswith(expected_suffix):
        raise ValueError(
            f"assignment version {assignment_version!r} does not match "
            f"availability mode {availability_mode.value!r}"
        )


def _source_availability(row: sqlite3.Row) -> tuple[datetime | None, str | None]:
    candidates = (
        (
            _parse_utc(row["observation_usable_at"], "observation first_usable_at"),
            "raw_observation_first_usable_at",
        ),
        (
            _parse_utc(row["artifact_usable_at"], "artifact first_usable_at"),
            "raw_artifact_first_usable_at",
        ),
    )
    available = tuple(value for value in candidates if value[0] is not None)
    return min(available, key=lambda value: (value[0], value[1])) if available else (None, None)


def _rows_by_match(
    rows: Iterable[sqlite3.Row],
) -> dict[int, list[sqlite3.Row]]:
    result: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        result.setdefault(int(row["match_id"]), []).append(row)
    return result


def _role_rows(
    connection: sqlite3.Connection, assignment_version: str
) -> dict[tuple[int, int, str], sqlite3.Row]:
    rows = connection.execute(
        _CORPUS_ROLE_QUERY,
        (assignment_version,),
    ).fetchall()
    return {
        (int(row["match_id"]), int(row["player_slot"]), str(row["purpose"])): row
        for row in rows
    }


def _team_state_evidence(
    state: sqlite3.Row | None,
    *,
    won: bool,
    completed_at: datetime,
) -> DraftTeamMapEvidence:
    if state is None:
        return DraftTeamMapEvidence()
    _json_object(state["objective_conversion_json"], "objective_conversion_json")
    had_deficit = state["first_significant_deficit_at"] is not None
    had_lead = state["first_significant_lead_at"] is not None
    return DraftTeamMapEvidence(
        comeback_opportunity=had_deficit,
        came_back=won if had_deficit else None,
        throw_opportunity=had_lead,
        threw=(not won) if had_lead else None,
        closeout_opportunity=had_lead,
        closed_out=won if had_lead else None,
        # The persisted conversion facts expose opportunities, not exact event
        # counts. Keep count features missing rather than treating booleans as
        # counts.
        roshan_events=None,
        high_ground_events=None,
        long_fight_wins=None,
        long_fight_opportunities=None,
        state_provenance=DerivedFactProvenance(
            cutoff=completed_at,
            first_usable_at=_parse_utc(state["created_at"], "team state created_at"),
            input_hash=str(state["input_hash"]),
            version=str(state["label_version"]),
        ),
    )


def _profile_state(
    state: sqlite3.Row,
    *,
    match_id: int,
    team_id: int,
    opponent_id: int,
    side: Side,
    won: bool,
) -> TeamMapState:
    max_lead = _number(state["max_lead"])
    max_deficit = _number(state["max_deficit"])
    thresholds = tuple(
        ThresholdFacts(
            threshold,
            0 if max_lead is not None and max_lead >= threshold else None,
            0 if max_deficit is not None and max_deficit <= -threshold else None,
        )
        for threshold in (3_000, 5_000, 10_000)
    )
    crossing_values = json.loads(state["crossings_json"])
    if not isinstance(crossing_values, list):
        raise ValueError("crossings_json must contain an array")
    crossings = tuple(CurveCrossing(**value) for value in crossing_values)
    conversion = _json_object(
        state["objective_conversion_json"], "objective_conversion_json"
    )
    source_values = json.loads(state["source_versions_json"])
    if not isinstance(source_values, list):
        raise ValueError("source_versions_json must contain an array")
    source_versions = tuple((str(key), str(value)) for key, value in source_values)
    duration = _integer(state["duration_seconds"])
    scoreable = str(state["label"]) != TeamStateLabel.UNSCORABLE.value
    return TeamMapState(
        match_id=match_id,
        team_id=team_id,
        opponent_id=opponent_id,
        side=side,
        won=won,
        label=TeamStateLabel(str(state["label"])),
        unscorable_reason=None if scoreable else "persisted_state_unscorable",
        duration_seconds=duration,
        analysis_start_minute=10 if scoreable else None,
        analysis_end_minute=(
            None if not scoreable or duration is None else max(10, duration // 60 - 2)
        ),
        smoothed_curve=(),
        max_lead=max_lead,
        max_deficit=max_deficit,
        ahead_fraction=_number(state["ahead_fraction"]),
        behind_fraction=_number(state["behind_fraction"]),
        even_fraction=_number(state["even_fraction"]),
        signed_auc=_number(state["signed_auc"]),
        absolute_auc=_number(state["absolute_auc"]),
        crossings=crossings,
        first_significant_lead_at=_integer(state["first_significant_lead_at"]),
        first_significant_deficit_at=_integer(
            state["first_significant_deficit_at"]
        ),
        closeout_seconds=_integer(state["closeout_seconds"]),
        thresholds=thresholds,
        objective_conversion=ObjectiveConversionFacts(**conversion),
        curve_coverage=float(state["curve_coverage"]),
        source_versions=source_versions,
        input_hash=str(state["input_hash"]),
        label_version=str(state["label_version"]),
    )


def _hero_evidence(
    *,
    match_id: int,
    player: sqlite3.Row,
    facts: Mapping[str, Any],
    observed_role: sqlite3.Row | None,
    score: sqlite3.Row | None,
    completed_at: datetime,
    availability_mode: AvailabilityMode,
) -> DraftHeroMapEvidence:
    account_id = _integer(player["account_id"])
    buybacks = facts.get("buyback_log")
    observed_position = (
        None if observed_role is None else _integer(observed_role["position"])
    )
    observed_stored_cutoff = (
        None
        if observed_role is None
        else _parse_utc(observed_role["input_cutoff"], "observed role input_cutoff")
    )
    observed_provenance = (
        None
        if observed_role is None or observed_position is None
        else DerivedFactProvenance(
            cutoff=(
                completed_at
                if availability_mode is AvailabilityMode.RECONSTRUCTED
                else observed_stored_cutoff
            ),
            first_usable_at=_parse_utc(
                observed_role["created_at"], "observed role created_at"
            ),
            input_hash=str(observed_role["input_hash"]),
            version=(
                str(observed_role["assignment_version"])
                if availability_mode is AvailabilityMode.PROSPECTIVE
                else f"{observed_role['assignment_version']}+stored-cutoff="
                f"{observed_stored_cutoff.isoformat()}"
            ),
        )
    )
    execution_score = None if score is None else _number(score["execution_score"])
    score_stored_cutoff = (
        None
        if score is None
        else _parse_utc(score["benchmark_cutoff"], "score benchmark_cutoff")
    )
    score_provenance = (
        None
        if score is None or execution_score is None
        else DerivedFactProvenance(
            cutoff=(
                completed_at
                if availability_mode is AvailabilityMode.RECONSTRUCTED
                else score_stored_cutoff
            ),
            first_usable_at=_parse_utc(score["created_at"], "score created_at"),
            input_hash=str(score["input_hash"]),
            version=(
                str(score["score_version"])
                if availability_mode is AvailabilityMode.PROSPECTIVE
                else f"{score['score_version']}+stored-cutoff="
                f"{score_stored_cutoff.isoformat()}"
            ),
        )
    )
    return DraftHeroMapEvidence(
        player_id=_player_id(match_id, account_id, int(player["player_slot"])),
        hero_id=int(player["hero_id"]),
        observed_position=observed_position,
        observed_position_confidence=(
            0.0 if observed_position is None else float(observed_role["confidence"])
        ),
        observed_role_purpose=(
            None if observed_position is None else RolePurpose.OBSERVED_POSITION
        ),
        observed_role_source=(
            None
            if observed_position is None
            else RoleSource(str(observed_role["assignment_source"]))
        ),
        observed_role_provenance=observed_provenance,
        execution_score=execution_score,
        score_provenance=score_provenance,
        control_seconds=_nonnegative_number(facts.get("stuns")),
        hero_healing=_nonnegative_number(facts.get("hero_healing")),
        last_hits=_nonnegative_number(facts.get("last_hits")),
        tower_damage=_nonnegative_number(facts.get("tower_damage")),
        net_worth=_nonnegative_number(facts.get("net_worth")),
        buyback_count=len(buybacks) if isinstance(buybacks, list) else None,
    )


def _load_draft_corpus(
    connection: sqlite3.Connection,
    *,
    availability_mode: AvailabilityMode,
    assignment_version: str | None = None,
) -> DraftCorpus:
    """Load exact strict maps without using a target map's observed role."""

    resolved_version = resolve_assignment_version(connection, assignment_version)
    _validate_assignment_mode(resolved_version, availability_mode)
    score_version = score_version_for_role(resolved_version)
    event_order = tuple(
        EventOrderEntry(
            event_id=str(row["event_id"]),
            canonical_name=str(row["canonical_name"]),
            main_event_start_at=str(row["main_event_start_at"]),
        )
        for row in connection.execute(
            """SELECT event_id, canonical_name, main_event_start_at
               FROM formal_events
               ORDER BY main_event_start_at, event_id"""
        ).fetchall()
    )
    base_rows = connection.execute(
        """SELECT eligible.match_id, eligible.event_id, status.series_id,
                  status.map_number, status.normalizer_version,
                  status.latest_raw_artifact_id, status.latest_raw_content_hash,
                  match.start_time, match.duration, match.radiant_win,
                  match.radiant_team_id, match.dire_team_id, match.patch,
                  artifact.first_usable_at AS artifact_usable_at,
                  (SELECT MIN(observation.first_usable_at)
                     FROM raw_source_observations AS observation
                    WHERE observation.artifact_id=artifact.artifact_id
                      AND observation.content_hash=artifact.content_hash
                      AND observation.first_usable_at IS NOT NULL
                  ) AS observation_usable_at
           FROM formal_map_eligibility AS eligible
           JOIN match_ingest_status AS status ON status.match_id=eligible.match_id
           JOIN matches AS match ON match.match_id=eligible.match_id
           JOIN raw_source_artifacts AS artifact
             ON artifact.artifact_id=status.latest_raw_artifact_id
            AND artifact.content_hash=status.latest_raw_content_hash
            AND artifact.source='opendota'
           WHERE eligible.draft_readiness='ready'
           ORDER BY match.start_time, eligible.match_id"""
    ).fetchall()
    formal_count = int(
        connection.execute(
            """SELECT COUNT(*) FROM formal_map_eligibility
               WHERE draft_readiness='ready'"""
        ).fetchone()[0]
    )
    if len(base_rows) != formal_count:
        raise ValueError("a formal draft-ready map lacks its exact latest raw artifact")

    facts_by_match = _rows_by_match(
        connection.execute(
            _CORPUS_FACT_QUERY
        ).fetchall()
    )
    players_by_match = _rows_by_match(
        connection.execute(
            _CORPUS_PLAYER_QUERY
        ).fetchall()
    )
    picks_by_match = _rows_by_match(
        connection.execute(
            _CORPUS_PICK_QUERY
        ).fetchall()
    )
    roles = _role_rows(connection, resolved_version)
    scores = {
        (int(row["match_id"]), int(row["player_slot"])): row
        for row in connection.execute(
            _CORPUS_SCORE_QUERY,
            (score_version,),
        ).fetchall()
    }
    states = {
        (int(row["match_id"]), str(row["side"])): row
        for row in connection.execute(
            _CORPUS_STATE_QUERY,
            (LABEL_VERSION,),
        ).fetchall()
    }

    loaded = []
    profile_maps: list[ProfileMap] = []
    for base in base_rows:
        match_id = int(base["match_id"])
        start_time = _integer(base["start_time"])
        duration = _integer(base["duration"])
        radiant_team_id = _integer(base["radiant_team_id"])
        dire_team_id = _integer(base["dire_team_id"])
        if (
            start_time is None
            or start_time <= 0
            or duration is None
            or duration <= 0
            or base["radiant_win"] not in (0, 1)
            or radiant_team_id is None
            or dire_team_id is None
            or radiant_team_id == dire_team_id
        ):
            raise ValueError(f"formal draft map {match_id} has invalid result/timing/teams")
        content_hash = str(base["latest_raw_content_hash"] or "")
        if len(content_hash) != 64:
            raise ValueError(f"formal draft map {match_id} has invalid source hash")

        fact_rows = facts_by_match.get(match_id, [])
        player_rows = players_by_match.get(match_id, [])
        pick_rows = picks_by_match.get(match_id, [])
        if len(fact_rows) != 10 or len(player_rows) != 10 or len(pick_rows) != 10:
            raise ValueError(f"formal draft map {match_id} lacks exact ten-player draft")
        facts_by_slot = {int(row["player_slot"]): row for row in fact_rows}
        players_by_slot = {int(row["player_slot"]): row for row in player_rows}
        if len(facts_by_slot) != 10 or set(facts_by_slot) != set(players_by_slot):
            raise ValueError(f"formal draft map {match_id} has inconsistent player slots")

        if availability_mode is AvailabilityMode.PROSPECTIVE:
            expected_positions: dict[bool, list[int]] = {True: [], False: []}
            expected_complete = True
            for slot, player in players_by_slot.items():
                if player["is_radiant"] not in (0, 1):
                    raise ValueError(
                        f"formal draft map {match_id} has an invalid player side"
                    )
                role = roles.get((match_id, slot, "expected_position"))
                position = None if role is None else _integer(role["position"])
                if position not in range(1, 6):
                    expected_complete = False
                    continue
                expected_positions[bool(player["is_radiant"])].append(position)
            if not expected_complete or any(
                sorted(positions) != [1, 2, 3, 4, 5]
                for positions in expected_positions.values()
            ):
                continue

        side_players: dict[bool, list[sqlite3.Row]] = {True: [], False: []}
        fact_objects: dict[int, dict[str, Any]] = {}
        for slot, player in players_by_slot.items():
            fact = facts_by_slot[slot]
            if player["hero_id"] is None or fact["hero_id"] is None:
                raise ValueError(f"formal draft map {match_id} has a missing hero")
            if int(player["hero_id"]) != int(fact["hero_id"]):
                raise ValueError(f"formal draft map {match_id} player heroes disagree")
            if player["is_radiant"] not in (0, 1) or fact["is_radiant"] not in (0, 1):
                raise ValueError(f"formal draft map {match_id} has an invalid player side")
            if bool(player["is_radiant"]) != bool(fact["is_radiant"]):
                raise ValueError(f"formal draft map {match_id} player sides disagree")
            expected_role = roles.get((match_id, slot, "expected_position"))
            if expected_role is None:
                raise ValueError(
                    f"formal draft map {match_id} lacks pinned expected positions"
                )
            radiant_side = bool(player["is_radiant"])
            expected_team_id = radiant_team_id if radiant_side else dire_team_id
            for source, value in (
                ("match player", player["team_id"]),
                ("exact fact", fact["team_id"]),
                ("expected role", expected_role["team_id"]),
            ):
                team_id = _integer(value)
                if team_id is not None and team_id != expected_team_id:
                    raise ValueError(
                        f"formal draft map {match_id} {source} team disagrees"
                    )
            player_account = _positive_integer(player["account_id"])
            for source, value in (
                ("exact fact", fact["account_id"]),
                ("expected role", expected_role["account_id"]),
            ):
                account_id = _positive_integer(value)
                if (
                    player_account is not None
                    and account_id is not None
                    and account_id != player_account
                ):
                    raise ValueError(
                        f"formal draft map {match_id} {source} account disagrees"
                    )
            side_players[radiant_side].append(player)
            fact_objects[slot] = _json_object(fact["facts_json"], "facts_json")
        if any(len(rows) != 5 for rows in side_players.values()):
            raise ValueError(f"formal draft map {match_id} does not have five per side")

        picked = {
            team: [int(row["hero_id"]) for row in pick_rows if row["team"] == team]
            for team in (0, 1)
        }
        lineup = {
            0: [int(row["hero_id"]) for row in side_players[True]],
            1: [int(row["hero_id"]) for row in side_players[False]],
        }
        if any(len(set(picked[team])) != 5 for team in (0, 1)):
            raise ValueError(f"formal draft map {match_id} has duplicate/missing side picks")
        if any(set(picked[team]) != set(lineup[team]) for team in (0, 1)):
            raise ValueError(f"formal draft map {match_id} picks and players disagree")

        def history_team(side: bool, team_id: int) -> DraftTeam:
            draft_players = []
            for player in sorted(
                side_players[side], key=lambda value: int(value["player_slot"])
            ):
                slot = int(player["player_slot"])
                account_id = _integer(player["account_id"])
                draft_players.append(
                    DraftPlayer(
                        player_id=_player_id(match_id, account_id, slot),
                        hero_id=int(player["hero_id"]),
                    )
                )
            return DraftTeam(team_id=team_id, players=tuple(draft_players))

        def target_team(side: bool, team_id: int) -> DraftTeam:
            draft_players = []
            for player in sorted(
                side_players[side], key=lambda value: int(value["player_slot"])
            ):
                slot = int(player["player_slot"])
                role = roles[(match_id, slot, "expected_position")]
                position = _integer(role["position"])
                source = RoleSource(str(role["assignment_source"]))
                draft_players.append(
                    DraftPlayer(
                        player_id=_player_id(
                            match_id, _integer(player["account_id"]), slot
                        ),
                        hero_id=int(player["hero_id"]),
                        expected_role=ExpectedRoleAssignment(
                            purpose=RolePurpose.EXPECTED_POSITION,
                            source=source,
                            position=position,
                            confidence=(
                                float(role["confidence"])
                                if position is not None
                                else 0.0
                            ),
                            provenance=DerivedFactProvenance(
                                cutoff=_parse_utc(
                                    role["input_cutoff"],
                                    "expected role input_cutoff",
                                ),
                                first_usable_at=_parse_utc(
                                    role["created_at"], "expected role created_at"
                                ),
                                input_hash=str(role["input_hash"]),
                                version=str(role["assignment_version"]),
                            ),
                        ),
                    )
                )
            return DraftTeam(team_id=team_id, players=tuple(draft_players))

        radiant_history = history_team(True, radiant_team_id)
        dire_history = history_team(False, dire_team_id)
        started_at = datetime.fromtimestamp(start_time, UTC)
        completed_at = started_at + timedelta(seconds=duration)
        for player in player_rows:
            slot = int(player["player_slot"])
            role = roles[(match_id, slot, "expected_position")]
            role_cutoff = _parse_utc(role["input_cutoff"], "expected role input_cutoff")
            if role_cutoff is None or role_cutoff > started_at:
                raise ValueError(
                    f"formal draft map {match_id} has a non-causal expected position"
                )
        source_usable_at, source_cutoff = _source_availability(base)
        if source_usable_at is not None and source_usable_at < completed_at:
            raise ValueError(
                f"formal draft map {match_id} raw facts precede map completion"
            )
        fact_usable_values = tuple(
            _parse_utc(row["first_usable_at"], "player fact first_usable_at")
            for row in fact_rows
        )
        if source_usable_at is None or any(
            value is None for value in fact_usable_values
        ):
            map_usable_at = None
        else:
            map_usable_at = max(
                source_usable_at,
                *(value for value in fact_usable_values if value is not None),
            )
        evidence_usable_at = (
            completed_at
            if availability_mode is AvailabilityMode.RECONSTRUCTED
            else map_usable_at
        )

        radiant_hero_evidence = tuple(
            _hero_evidence(
                match_id=match_id,
                player=player,
                facts=fact_objects[int(player["player_slot"])],
                observed_role=roles.get(
                    (match_id, int(player["player_slot"]), "observed_position")
                ),
                score=scores.get((match_id, int(player["player_slot"]))),
                completed_at=completed_at,
                availability_mode=availability_mode,
            )
            for player in sorted(
                side_players[True], key=lambda value: int(value["player_slot"])
            )
        )
        dire_hero_evidence = tuple(
            _hero_evidence(
                match_id=match_id,
                player=player,
                facts=fact_objects[int(player["player_slot"])],
                observed_role=roles.get(
                    (match_id, int(player["player_slot"]), "observed_position")
                ),
                score=scores.get((match_id, int(player["player_slot"]))),
                completed_at=completed_at,
                availability_mode=availability_mode,
            )
            for player in sorted(
                side_players[False], key=lambda value: int(value["player_slot"])
            )
        )
        radiant_win = bool(base["radiant_win"])
        source_input_hash = _hash(
            {
                "raw_content_hash": content_hash,
                "assignment_version": resolved_version,
                "score_version": score_version,
                "score_hashes": sorted(
                    row["input_hash"]
                    for key, row in scores.items()
                    if key[0] == match_id
                ),
                "state_hashes": sorted(
                    row["input_hash"]
                    for key, row in states.items()
                    if key[0] == match_id
                ),
            }
        )
        map_number = _positive_integer(base["map_number"])
        series_id = _positive_integer(base["series_id"])
        evidence = DraftMapEvidence(
            evidence_id=f"strict-map:{match_id}:{source_input_hash}",
            source_input_hash=source_input_hash,
            match_id=match_id,
            completed_at=completed_at,
            first_usable_at=evidence_usable_at,
            event_id=str(base["event_id"]),
            patch=_integer(base["patch"]),
            duration_seconds=duration,
            series_id=series_id,
            map_number=map_number,
            radiant=radiant_history,
            dire=dire_history,
            radiant_win=radiant_win,
            radiant_hero_evidence=radiant_hero_evidence,
            dire_hero_evidence=dire_hero_evidence,
            radiant_team_evidence=_team_state_evidence(
                states.get((match_id, "radiant")),
                won=radiant_win,
                completed_at=completed_at,
            ),
            dire_team_evidence=_team_state_evidence(
                states.get((match_id, "dire")),
                won=not radiant_win,
                completed_at=completed_at,
            ),
        )
        for side, team_value, opponent_id, won in (
            (Side.RADIANT, radiant_history, dire_team_id, radiant_win),
            (Side.DIRE, dire_history, radiant_team_id, not radiant_win),
        ):
            state_row = states.get((match_id, side.value))
            if state_row is None:
                continue
            state_created_at = _parse_utc(
                state_row["created_at"], "team state created_at"
            )
            profile_first_usable = (
                completed_at
                if availability_mode is AvailabilityMode.RECONSTRUCTED
                else (
                    None
                    if evidence_usable_at is None or state_created_at is None
                    else max(evidence_usable_at, state_created_at)
                )
            )
            profile_maps.append(
                ProfileMap(
                    state=_profile_state(
                        state_row,
                        match_id=match_id,
                        team_id=team_value.team_id,
                        opponent_id=opponent_id,
                        side=side,
                        won=won,
                    ),
                    completed_at=completed_at,
                    first_usable_at=profile_first_usable,
                    event_id=str(base["event_id"]),
                    patch=_integer(base["patch"]),
                    roster=tuple(
                        sorted(
                            player.player_id
                            for player in team_value.players
                            if player.player_id > 0
                        )
                    ),
                )
            )

        target = None
        cutoff_source = None
        if availability_mode is AvailabilityMode.RECONSTRUCTED:
            cutoff = started_at
            cutoff_source = "reconstructed_map_start"
        elif source_usable_at is not None and source_usable_at <= started_at:
            # This branch is retained for a future independently archived draft
            # timestamp. Exact completed-map artifacts are validated above and
            # therefore cannot currently satisfy it.
            cutoff = source_usable_at
            cutoff_source = source_cutoff
        else:
            cutoff = None
        if cutoff is not None and any(
            _parse_utc(
                roles[(match_id, int(player["player_slot"]), "expected_position")][
                    "input_cutoff"
                ],
                "expected role input_cutoff",
            )
            > cutoff
            for player in player_rows
        ):
            cutoff = None
            cutoff_source = None
        if cutoff is not None:
            radiant_target = target_team(True, radiant_team_id)
            dire_target = target_team(False, dire_team_id)
            target = DraftTarget(
                match_id=match_id,
                prediction_cutoff=cutoff,
                event_id=str(base["event_id"]),
                patch=_integer(base["patch"]),
                series_id=series_id,
                map_number=map_number,
                radiant=radiant_target,
                dire=dire_target,
                availability_mode=availability_mode,
            )
        loaded.append(
            LoadedDraftMap(
                match_id=match_id,
                series_id=series_id,
                event_id=str(base["event_id"]),
                duration_seconds=duration,
                radiant_win=radiant_win,
                prediction_cutoff_source=cutoff_source,
                target=target,
                evidence=evidence,
            )
        )

    loaded.sort(
        key=lambda row: (
            row.target is None,
            row.target.prediction_cutoff if row.target is not None else row.evidence.completed_at,
            row.match_id,
        )
    )
    return DraftCorpus(
        assignment_version=resolved_version,
        score_version=score_version,
        availability_mode=availability_mode.value,
        formal_draft_maps=formal_count,
        event_order=event_order,
        # This strict implementation deliberately excludes legacy professional
        # maps. A future version may add separately versioned cold-start priors.
        cold_start_support=0,
        maps=tuple(loaded),
        profile_maps=tuple(profile_maps),
    )


def load_draft_corpus(
    connection: sqlite3.Connection,
    *,
    availability_mode: AvailabilityMode,
    assignment_version: str | None = None,
) -> DraftCorpus:
    return _load_draft_corpus(
        connection,
        availability_mode=availability_mode,
        assignment_version=assignment_version,
    )


def load_bounded_draft_snapshot(
    connection: sqlite3.Connection,
    *,
    availability_mode: AvailabilityMode,
    assignment_version: str | None = None,
    max_rows: int,
    max_bytes: int,
    max_value_bytes: int,
) -> tuple[str, DraftCorpus]:
    """Return a fingerprint and corpus only after SQL-level size gates pass."""

    if max_value_bytes < 1:
        raise ValueError("draft dependency value limit must be positive")
    previous_limit = connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    connection.setlimit(
        sqlite3.SQLITE_LIMIT_LENGTH,
        min(previous_limit, max_value_bytes),
    )
    try:
        resolved_version = resolve_assignment_version(
            connection,
            assignment_version,
        )
        _validate_assignment_mode(resolved_version, availability_mode)
        _bounded_draft_dependency_usage(
            connection,
            max_rows=max_rows,
            max_bytes=max_bytes,
            additional_queries=(
                ("corpus_player_map_facts", _CORPUS_FACT_QUERY, ()),
                (
                    "corpus_player_role_assignments",
                    _CORPUS_ROLE_QUERY,
                    (resolved_version,),
                ),
                (
                    "corpus_player_map_scores",
                    _CORPUS_SCORE_QUERY,
                    (score_version_for_role(resolved_version),),
                ),
                ("corpus_match_players", _CORPUS_PLAYER_QUERY, ()),
                ("corpus_picks_bans", _CORPUS_PICK_QUERY, ()),
                (
                    "corpus_team_map_states",
                    _CORPUS_STATE_QUERY,
                    (LABEL_VERSION,),
                ),
            ),
        )
        fingerprint = _dependency_fingerprint(
            connection,
            _DRAFT_DEPENDENCY_QUERIES,
        )
        corpus = _load_draft_corpus(
            connection,
            availability_mode=availability_mode,
            assignment_version=resolved_version,
        )
        return fingerprint, corpus
    finally:
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous_limit)


def _model_features(
    snapshot: DraftFeatureSnapshot, model_kind: str
) -> dict[str, float | None]:
    if model_kind == "pure_draft":
        return snapshot.pure_values()
    if model_kind == "context_adjusted":
        return snapshot.context_values(include_pure=True)
    raise ValueError(f"unsupported model kind: {model_kind}")


def _style_snapshot(
    corpus: DraftCorpus,
    target: DraftTarget,
    team: DraftTeam,
) -> DraftStyleSnapshot:
    mode = ProfileAvailabilityMode(corpus.availability_mode)
    roster = tuple(
        sorted(player.player_id for player in team.players if player.player_id > 0)
    )
    priors = derive_causal_event_patch_priors(
        team_id=team.team_id,
        cutoff=target.prediction_cutoff,
        maps=corpus.profile_maps,
        target_event_id=target.event_id,
        target_patch=target.patch,
    )
    profile = build_team_style_profile(
        team_id=team.team_id,
        cutoff=target.prediction_cutoff,
        maps=corpus.profile_maps,
        priors=priors,
        target_roster=roster,
        target_patch=target.patch,
        availability_mode=mode,
    )
    comeback = profile.rate(comeback_metric(5_000))
    throw = profile.rate(throw_metric(5_000))
    closeout = profile.rate(CLOSEOUT_5K_RATE)

    def rate(value: float, support: int) -> DraftStyleRateSnapshot:
        return DraftStyleRateSnapshot(
            value=value,
            support=support,
            coverage=min(1.0, support / 5.0),
        )

    return DraftStyleSnapshot(
        team_id=team.team_id,
        availability_mode=target.availability_mode,
        provenance=DerivedFactProvenance(
            cutoff=target.prediction_cutoff,
            first_usable_at=target.prediction_cutoff,
            input_hash=profile.input_hash,
            version=PROFILE_VERSION,
        ),
        comeback_rate=rate(comeback.mean, comeback.opportunities),
        throw_resilience_rate=rate(1.0 - throw.mean, throw.opportunities),
        closeout_rate=rate(closeout.mean, closeout.opportunities),
    )


def _draft_snapshot_rows(corpus: DraftCorpus) -> tuple[_SnapshotRow, ...]:
    history = tuple(row.evidence for row in corpus.maps)
    rows: list[_SnapshotRow] = []
    for row in sorted(
        corpus.targets,
        key=lambda value: (value.target.prediction_cutoff, value.match_id),
    ):
        target = row.target
        if target is None:
            continue
        styled_target = replace(
            target,
            radiant_style=_style_snapshot(corpus, target, target.radiant),
            dire_style=_style_snapshot(corpus, target, target.dire),
        )
        rows.append(
            _SnapshotRow(
                replace(row, target=styled_target),
                build_draft_feature_snapshot(styled_target, history),
            )
        )
    return tuple(rows)


def draft_snapshot_hashes(corpus: DraftCorpus) -> dict[int, str]:
    """Rebuild current target input identities for persisted-run validation."""
    return {
        row.game.match_id: row.snapshot.input_hash
        for row in _draft_snapshot_rows(corpus)
    }


def _prepare_runs(
    corpus: DraftCorpus,
    *,
    min_samples: int,
    l2_regularization: float,
) -> tuple[
    tuple[PersistedRun, ...],
    tuple[SliceReport, ...],
    tuple[EventSliceReport, ...],
]:
    snapshots = _draft_snapshot_rows(corpus)
    runs: list[PersistedRun] = []
    points: dict[tuple[str, int], list[EvaluationPoint]] = {
        (kind, horizon): [] for kind in MODEL_KINDS for horizon in HORIZONS
    }
    eligible: dict[tuple[str, int], int] = {
        (kind, horizon): 0 for kind in MODEL_KINDS for horizon in HORIZONS
    }
    insufficient: dict[tuple[str, int], int] = {
        (kind, horizon): 0 for kind in MODEL_KINDS for horizon in HORIZONS
    }
    event_keys = tuple(
        (event.event_id, kind, horizon)
        for event in corpus.event_order
        for kind in MODEL_KINDS
        for horizon in HORIZONS
    )
    event_points: dict[tuple[str, str, int], list[EvaluationPoint]] = {
        key: [] for key in event_keys
    }
    event_eligible = {key: 0 for key in event_keys}
    event_insufficient = {key: 0 for key in event_keys}

    for current in snapshots:
        target = current.game.target
        if target is None or current.game.prediction_cutoff_source is None:
            raise AssertionError("snapshot target lacks a persisted cutoff source")
        earlier = tuple(
            row
            for row in snapshots
            if row.game.target is not None
            and row.game.target.prediction_cutoff < target.prediction_cutoff
            and row.game.evidence.completed_at < target.prediction_cutoff
        )
        for horizon in HORIZONS:
            if current.game.duration_seconds <= horizon * 60:
                continue
            for model_kind in MODEL_KINDS:
                key = (model_kind, horizon)
                event_key = (current.game.event_id, model_kind, horizon)
                eligible[key] += 1
                event_eligible[event_key] += 1
                target_features = _model_features(current.snapshot, model_kind)
                schema = FeatureSchema.from_names(target_features)
                training_rows = tuple(
                    DraftTrainingRow(
                        match_id=row.game.match_id,
                        input_snapshot_hash=row.snapshot.input_hash,
                        cutoff=row.game.target.prediction_cutoff,
                        completed_at=row.game.evidence.completed_at,
                        result_usable_at=row.game.evidence.first_usable_at,
                        outcome=row.game.radiant_win,
                        duration_minutes=row.game.duration_seconds / 60.0,
                        series_id=(
                            row.game.series_id
                            if row.game.series_id is not None
                            else f"match:{row.game.match_id}"
                        ),
                        features=_model_features(row.snapshot, model_kind),
                    )
                    for row in earlier
                    if row.game.target is not None
                    and row.game.duration_seconds > horizon * 60
                )
                model = fit_draft_model(
                    training_rows,
                    schema,
                    target.prediction_cutoff,
                    horizon,
                    min_samples=min_samples,
                    model_kind=model_kind,
                    l2_regularization=l2_regularization,
                )
                prediction = predict_draft(model, target_features)
                probability = prediction.probability
                if probability is None:
                    insufficient[key] += 1
                    event_insufficient[event_key] += 1
                else:
                    point = EvaluationPoint(
                        current.game.match_id,
                        current.game.series_id,
                        current.game.event_id,
                        probability,
                        current.game.radiant_win,
                    )
                    points[key].append(point)
                    event_points[event_key].append(point)
                configuration = {
                    "backtest_version": BACKTEST_VERSION,
                    "assignment_version": corpus.assignment_version,
                    "score_version": corpus.score_version,
                    "target_match_id": current.game.match_id,
                    "target_event_id": current.game.event_id,
                    "cutoff_source": current.game.prediction_cutoff_source,
                    "feature_version": current.snapshot.feature_version,
                    "min_samples": min_samples,
                    "l2_regularization": l2_regularization,
                    "training_input_hash": model.training_input_hash,
                    "model_hash": model.model_hash,
                }
                per_run_metrics = {
                    "model_reason": model.reason,
                    "training_support": model.support,
                    "training_series_support": model.series_support,
                    "snapshot_support": current.snapshot.support,
                    "pure_coverage": current.snapshot.pure_coverage,
                    "context_coverage": current.snapshot.context_coverage,
                    "prediction": prediction.to_payload(),
                }
                stable_identity = {
                    "configuration": configuration,
                    "availability_mode": corpus.availability_mode,
                    "model_kind": model_kind,
                    "horizon_minutes": horizon,
                    "training_cutoff": target.prediction_cutoff.isoformat(),
                    "feature_schema_hash": model.feature_schema_hash,
                    "input_snapshot_hash": current.snapshot.input_hash,
                }
                run_id = f"draft-{_hash(stable_identity)}"
                runs.append(
                    PersistedRun(
                        run_id=run_id,
                        model_version=model.model_version,
                        model_kind=model_kind,
                        horizon_minutes=horizon,
                        availability_mode=corpus.availability_mode,
                        training_cutoff=target.prediction_cutoff.isoformat(),
                        feature_schema_hash=model.feature_schema_hash,
                        configuration_json=_canonical_json(configuration),
                        metrics_json=_canonical_json(per_run_metrics),
                        status=model.status.value,
                        match_id=current.game.match_id,
                        prediction_cutoff=target.prediction_cutoff.isoformat(),
                        cutoff_source=current.game.prediction_cutoff_source,
                        input_snapshot_hash=current.snapshot.input_hash,
                        probability=probability,
                        uncertainty=prediction.uncertainty,
                        support=prediction.support,
                        eventual_radiant_win=int(current.game.radiant_win),
                        prediction_status=(
                            "settled" if probability is not None else "insufficient_evidence"
                        ),
                    )
                )

    slice_reports = []
    for model_kind in MODEL_KINDS:
        for horizon in HORIZONS:
            key = (model_kind, horizon)
            metrics = evaluate_points(
                points[key],
                seed_material=(
                    f"{BACKTEST_VERSION}:{corpus.availability_mode}:"
                    f"{model_kind}:{horizon}"
                ),
            )
            slice_reports.append(
                SliceReport(
                    model_kind=model_kind,
                    horizon_minutes=horizon,
                    eligible_targets=eligible[key],
                    predicted=len(points[key]),
                    insufficient_evidence=insufficient[key],
                    metrics=metrics,
                )
            )
    event_reports = []
    for event in corpus.event_order:
        for model_kind in MODEL_KINDS:
            for horizon in HORIZONS:
                key = (event.event_id, model_kind, horizon)
                metrics = evaluate_points(
                    event_points[key],
                    seed_material=(
                        f"{BACKTEST_VERSION}:{corpus.availability_mode}:"
                        f"{event.event_id}:{model_kind}:{horizon}"
                    ),
                )
                event_reports.append(
                    EventSliceReport(
                        event_id=event.event_id,
                        canonical_name=event.canonical_name,
                        model_kind=model_kind,
                        horizon_minutes=horizon,
                        eligible_targets=event_eligible[key],
                        predicted=len(event_points[key]),
                        insufficient_evidence=event_insufficient[key],
                        metrics=metrics,
                    )
                )
    return tuple(runs), tuple(slice_reports), tuple(event_reports)


def _bootstrap_ece_upper(
    points: Sequence[EvaluationPoint], *, seed_material: str
) -> float | None:
    if not points:
        return None
    clusters: dict[str, list[EvaluationPoint]] = {}
    for row in points:
        cluster = (
            f"series:{row.series_id}"
            if row.series_id is not None
            else f"match:{row.match_id}"
        )
        clusters.setdefault(cluster, []).append(row)
    keys = sorted(clusters)
    seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
    generator = random.Random(seed)
    estimates = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sample = [
            point
            for _ in keys
            for point in clusters[keys[generator.randrange(len(keys))]]
        ]
        bins = _equal_count_calibration_bins(
            tuple(int(row.outcome) for row in sample),
            tuple(row.probability for row in sample),
            CALIBRATION_BINS,
        )
        estimate = math.fsum(row.count * row.absolute_gap for row in bins) / len(
            sample
        )
        if estimate is not None:
            estimates.append(estimate)
    if not estimates:
        return None
    estimates.sort()
    return estimates[math.ceil(0.90 * len(estimates)) - 1]


def evaluate_points(
    points: Sequence[EvaluationPoint], *, seed_material: str
) -> CalibrationMetrics:
    ordered = tuple(
        sorted(
            points,
            key=lambda row: (
                row.probability,
                int(row.outcome),
                row.event_id,
                -1 if row.series_id is None else row.series_id,
                row.match_id,
            ),
        )
    )
    support = len(ordered)
    if not ordered:
        return CalibrationMetrics(
            0, None, None, None, None, None, None, "unsupported", ("support<100",)
        )
    base = evaluate_binary_predictions(
        (row.outcome for row in ordered),
        (row.probability for row in ordered),
        ece_bins=CALIBRATION_BINS,
    )
    upper = _bootstrap_ece_upper(ordered, seed_material=seed_material)
    gate = passes_calibration_gate(base, ece_upper_bound=upper)
    status = "unsupported" if support < 100 else "passed" if gate.passed else "failed"
    return CalibrationMetrics(
        support=support,
        brier_score=base.brier_score,
        log_loss=base.log_loss,
        ece_5_bin=base.expected_calibration_error,
        ece_90_upper=upper,
        auc=base.auc,
        accuracy=base.accuracy,
        gate_status=status,
        gate_failures=gate.reasons,
    )


def _stable_run_columns(row: PersistedRun) -> tuple[object, ...]:
    return (
        row.model_version,
        row.model_kind,
        row.horizon_minutes,
        row.availability_mode,
        row.training_cutoff,
        row.feature_schema_hash,
        row.configuration_json,
        row.metrics_json,
        row.status,
    )


def _stable_prediction_columns(row: PersistedRun) -> tuple[object, ...]:
    return (
        row.match_id,
        row.prediction_cutoff,
        row.cutoff_source,
        row.input_snapshot_hash,
        row.probability,
        row.uncertainty,
        row.support,
        row.eventual_radiant_win,
        row.prediction_status,
    )


def persist_runs(
    connection: sqlite3.Connection,
    runs: Sequence[PersistedRun],
    *,
    dry_run: bool,
) -> PersistenceCounts:
    """Insert immutable runs and predictions atomically, or compare in dry-run."""

    inserted_runs = unchanged_runs = inserted_predictions = unchanged_predictions = 0
    created_at = datetime.now(UTC).isoformat()
    if not dry_run:
        connection.execute("BEGIN IMMEDIATE")
    try:
        for row in runs:
            existing_run = connection.execute(
                """SELECT model_version, model_kind, horizon_minutes,
                          availability_mode, training_cutoff, feature_schema_hash,
                          configuration_json, metrics_json, status
                   FROM draft_model_runs WHERE run_id=?""",
                (row.run_id,),
            ).fetchone()
            if existing_run is None:
                inserted_runs += 1
                if not dry_run:
                    connection.execute(
                        """INSERT INTO draft_model_runs
                           (run_id, model_version, model_kind, horizon_minutes,
                            availability_mode, training_cutoff, feature_schema_hash,
                            configuration_json, metrics_json, status, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (row.run_id, *_stable_run_columns(row), created_at),
                    )
            elif tuple(existing_run) == _stable_run_columns(row):
                unchanged_runs += 1
            else:
                raise ValueError(f"immutable draft run conflict: {row.run_id}")

            existing_prediction = connection.execute(
                """SELECT match_id, prediction_cutoff, cutoff_source,
                          input_snapshot_hash, probability, uncertainty, support,
                          eventual_radiant_win, status
                   FROM draft_predictions WHERE run_id=? AND match_id=?""",
                (row.run_id, row.match_id),
            ).fetchone()
            if existing_prediction is None:
                inserted_predictions += 1
                if not dry_run:
                    connection.execute(
                        """INSERT INTO draft_predictions
                           (run_id, match_id, prediction_cutoff, cutoff_source,
                            input_snapshot_hash, probability, uncertainty, support,
                            eventual_radiant_win, status, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row.run_id,
                            *_stable_prediction_columns(row),
                            created_at,
                        ),
                    )
            elif tuple(existing_prediction) == _stable_prediction_columns(row):
                unchanged_predictions += 1
            else:
                raise ValueError(
                    f"immutable draft prediction conflict: {row.run_id}/{row.match_id}"
                )
        if not dry_run:
            connection.commit()
    except BaseException:
        if not dry_run:
            connection.rollback()
        raise
    return PersistenceCounts(
        inserted_runs,
        unchanged_runs,
        inserted_predictions,
        unchanged_predictions,
    )


def run_strict_draft_backtest(
    database: Path,
    *,
    availability_mode: AvailabilityMode = AvailabilityMode.RECONSTRUCTED,
    assignment_version: str | None = None,
    dry_run: bool = False,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    l2_regularization: float = DEFAULT_L2_REGULARIZATION,
) -> BacktestReport:
    """Build, evaluate, and atomically persist chronological OOS predictions."""

    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 2:
        raise ValueError("min_samples must be an integer of at least 2")
    if (
        isinstance(l2_regularization, bool)
        or not isinstance(l2_regularization, (int, float))
        or not math.isfinite(l2_regularization)
        or l2_regularization <= 0
    ):
        raise ValueError("l2_regularization must be positive")
    database = database.resolve()
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro" if dry_run else str(database),
        uri=dry_run,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        expected_fingerprint = None
        expected_revision = None
        if not dry_run:
            ensure_draft_lineage_tracking(connection)
        connection.execute("BEGIN")
        try:
            if not dry_run:
                expected_fingerprint = draft_dependency_fingerprint(connection)
                revision_row = connection.execute(
                    """SELECT dependency_revision FROM draft_lineage_revisions
                         WHERE singleton=1"""
                ).fetchone()
                if revision_row is None:
                    raise RuntimeError("draft dependency revision is unavailable")
                expected_revision = int(revision_row[0])
            corpus = load_draft_corpus(
                connection,
                availability_mode=availability_mode,
                assignment_version=assignment_version,
            )
            runs, slices, event_slices = _prepare_runs(
                corpus,
                min_samples=min_samples,
                l2_regularization=float(l2_regularization),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        counts = persist_runs(connection, runs, dry_run=dry_run)
        if not dry_run:
            persist_draft_prediction_validations(
                connection,
                runs,
                expected_dependency_fingerprint=expected_fingerprint,
                expected_dependency_revision=expected_revision,
            )
        return BacktestReport(
            backtest_version=BACKTEST_VERSION,
            availability_mode=availability_mode.value,
            assignment_version=corpus.assignment_version,
            score_version=corpus.score_version,
            dry_run=dry_run,
            formal_draft_maps=corpus.formal_draft_maps,
            cold_start_support=corpus.cold_start_support,
            eligible_targets=len(corpus.targets),
            runs=len(runs),
            inserted_runs=counts.inserted_runs,
            unchanged_runs=counts.unchanged_runs,
            inserted_predictions=counts.inserted_predictions,
            unchanged_predictions=counts.unchanged_predictions,
            event_order=corpus.event_order,
            slices=slices,
            event_slices=event_slices,
        )
    finally:
        connection.close()


def report_as_dict(report: BacktestReport) -> dict[str, Any]:
    """Return a JSON-serializable report without enum or tuple surprises."""

    return asdict(report)
