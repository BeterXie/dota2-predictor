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
        """SELECT roles.*
           FROM player_role_assignments AS roles
           JOIN formal_map_eligibility AS eligible
             ON eligible.match_id=roles.match_id
           WHERE eligible.draft_readiness='ready'
             AND roles.assignment_version=?
             AND roles.purpose IN ('expected_position', 'observed_position')""",
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


def load_draft_corpus(
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
            """SELECT facts.*
               FROM formal_map_eligibility AS eligible
               JOIN match_ingest_status AS status ON status.match_id=eligible.match_id
               JOIN player_map_facts AS facts
                 ON facts.match_id=eligible.match_id
                AND facts.source_content_hash=status.latest_raw_content_hash
                AND facts.fact_version=status.normalizer_version || ':' ||
                                       status.latest_raw_content_hash
               WHERE eligible.draft_readiness='ready'
               ORDER BY facts.match_id, facts.player_slot"""
        ).fetchall()
    )
    players_by_match = _rows_by_match(
        connection.execute(
            """SELECT player.*
               FROM formal_map_eligibility AS eligible
               JOIN match_players AS player ON player.match_id=eligible.match_id
               WHERE eligible.draft_readiness='ready'
               ORDER BY player.match_id, player.player_slot"""
        ).fetchall()
    )
    picks_by_match = _rows_by_match(
        connection.execute(
            """SELECT pick.*
               FROM formal_map_eligibility AS eligible
               JOIN picks_bans AS pick ON pick.match_id=eligible.match_id
               WHERE eligible.draft_readiness='ready' AND pick.is_pick=1
               ORDER BY pick.match_id, pick.ord, pick.id"""
        ).fetchall()
    )
    roles = _role_rows(connection, resolved_version)
    scores = {
        (int(row["match_id"]), int(row["player_slot"])): row
        for row in connection.execute(
            """SELECT score.*
               FROM player_map_scores AS score
               JOIN formal_map_eligibility AS eligible
                 ON eligible.match_id=score.match_id
               WHERE eligible.draft_readiness='ready' AND score.score_version=?""",
            (score_version,),
        ).fetchall()
    }
    states = {
        (int(row["match_id"]), str(row["side"])): row
        for row in connection.execute(
            """SELECT state.*
               FROM team_map_states AS state
               JOIN formal_map_eligibility AS eligible
                 ON eligible.match_id=state.match_id
               WHERE eligible.draft_readiness='ready' AND state.label_version=?""",
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
    history = tuple(row.evidence for row in corpus.maps)
    snapshot_rows = []
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
        styled_game = replace(row, target=styled_target)
        snapshot_rows.append(
            _SnapshotRow(
                styled_game,
                build_draft_feature_snapshot(styled_target, history),
            )
        )
    snapshots = tuple(snapshot_rows)
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
        counts = persist_runs(connection, runs, dry_run=dry_run)
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
