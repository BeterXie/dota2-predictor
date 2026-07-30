"""Durable, idempotent derived-data completion for strict formal maps."""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Collection, Iterator, Mapping

from sqlalchemy.exc import SQLAlchemyError

from database.session import PostgresSession

from .benchmarks import BENCHMARK_VERSION
from .player_scoring import score_version_for_role
from .roles import PROSPECTIVE_ASSIGNMENT_VERSION, RECONSTRUCTED_ASSIGNMENT_VERSION
from .storage import IntelligenceStorage
from .team_profiles import PROFILE_VERSION
from .team_states import LABEL_VERSION


UTC = timezone.utc
ROLE_VERSION = RECONSTRUCTED_ASSIGNMENT_VERSION
SCORE_VERSION = score_version_for_role(ROLE_VERSION)


@dataclass(frozen=True)
class DerivedRunReport:
    requested_maps: int
    pending_maps: int
    derived_maps: int
    assignment_rows: int
    prospective_pending_maps: int
    prospective_assignment_rows: int
    score_rows: int
    state_rows: int
    profile_rows: int
    profile_cutoff: str | None


@dataclass(frozen=True)
class _SourceSnapshot:
    content_hash: str
    normalizer_version: str
    profile_context_hash: str


@dataclass(frozen=True)
class StrictCertificationAuthority:
    match_id: int
    source_content_hash: str
    role_assignment_version: str
    score_version: str
    team_state_version: str
    profile_version: str
    profile_cutoff: str
    derived_at: str
    derived_normalizer_version: str
    benchmark_version: str
    profile_context_hash: str
    status_event_id: str
    latest_raw_content_hash: str
    status_normalizer_version: str
    eligible_event_id: str
    player_readiness: str
    state_readiness: str
    draft_readiness: str
    current_profile_context_hash: str


@dataclass(frozen=True)
class _PendingComponents:
    base: frozenset[int] = frozenset()
    player: frozenset[int] = frozenset()
    state: frozenset[int] = frozenset()

    @property
    def all(self) -> set[int]:
        return set(self.base | self.player | self.state)


@dataclass(frozen=True)
class CurrentDerivedScopes:
    """Fail-closed formal and current-lineage scopes for delivery readers."""

    available: bool
    formal: frozenset[int] = frozenset()
    current: frozenset[int] = frozenset()
    player: frozenset[int] = frozenset()
    state: frozenset[int] = frozenset()
    draft: frozenset[int] = frozenset()
    draft_predictions: frozenset[tuple[str, int]] = frozenset()
    valid_profile_cutoffs: frozenset[str] = frozenset()


_PROFILE_CONTEXT_VERSION = "event-profile-context-v1"
_PROFILE_CONTEXT_FIELDS = (
    "event_id",
    "tier",
    "prize_pool_usd",
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
)
_PROFILE_CONTEXT_JSON_FIELDS = {
    "included_stages_json",
    "excluded_categories_json",
}


def _relation_columns(connection: PostgresSession, name: str) -> set[str]:
    rows = connection.execute(
        """SELECT column_name
             FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name=?""",
        (name,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def current_derived_scopes(connection: PostgresSession) -> CurrentDerivedScopes:
    """Return only lineage rows proven current against the formal registry."""
    if connection.in_transaction:
        return _current_derived_scopes(connection)
    with connection.transaction():
        return _current_derived_scopes(connection)


def _current_derived_scopes(connection: PostgresSession) -> CurrentDerivedScopes:
    formal_columns = _relation_columns(connection, "formal_map_eligibility")
    if "match_id" not in formal_columns:
        return CurrentDerivedScopes(available=False)
    try:
        formal = frozenset(
            int(row[0])
            for row in connection.execute(
                "SELECT match_id FROM formal_map_eligibility"
            ).fetchall()
        )
    except SQLAlchemyError:
        return CurrentDerivedScopes(available=False)

    required = {
        "formal_map_eligibility": {
            "match_id",
            "event_id",
            "player_readiness",
            "state_readiness",
            "draft_readiness",
        },
        "match_ingest_status": {
            "match_id",
            "event_id",
            "latest_raw_content_hash",
            "normalizer_version",
        },
        "strict_derived_status": {
            "match_id",
            "source_content_hash",
            "role_assignment_version",
            "score_version",
            "team_state_version",
            "profile_version",
            "profile_cutoff",
            "normalizer_version",
            "benchmark_version",
            "profile_context_hash",
        },
    }
    if any(
        not columns.issubset(_relation_columns(connection, relation))
        for relation, columns in required.items()
    ):
        return CurrentDerivedScopes(available=True, formal=formal)

    try:
        rows = connection.execute(
            """SELECT derived.match_id, derived.source_content_hash,
                      derived.role_assignment_version, derived.score_version,
                      derived.team_state_version, derived.profile_version,
                      derived.profile_cutoff,
                      derived.normalizer_version AS derived_normalizer_version,
                      derived.benchmark_version, derived.profile_context_hash,
                      status.event_id, status.latest_raw_content_hash,
                      status.normalizer_version,
                      eligible.event_id AS eligible_event_id,
                      eligible.player_readiness, eligible.state_readiness,
                      eligible.draft_readiness
                 FROM strict_derived_status AS derived
                 JOIN match_ingest_status AS status USING(match_id)
                 LEFT JOIN formal_map_eligibility AS eligible USING(match_id)"""
        ).fetchall()
        contexts = StrictDerivedPipeline._profile_context_hashes(
            connection, {str(row["event_id"]) for row in rows}
        )
    except (KeyError, TypeError, ValueError, SQLAlchemyError):
        return CurrentDerivedScopes(available=True, formal=formal)

    player_complete: set[int] = set()
    if {
        "match_id",
        "player_slot",
        "score_version",
    }.issubset(_relation_columns(connection, "player_map_scores")) and {
        "match_id",
        "player_slot",
        "purpose",
        "assignment_version",
        "position",
    }.issubset(_relation_columns(connection, "player_role_assignments")):
        try:
            scored = {
                int(row[0])
                for row in connection.execute(
                    """SELECT match_id FROM player_map_scores
                         WHERE score_version=?
                         GROUP BY match_id
                        HAVING COUNT(DISTINCT player_slot)=10""",
                    (SCORE_VERSION,),
                )
            }
            assigned = {
                int(row[0])
                for row in connection.execute(
                    """SELECT match_id FROM player_role_assignments
                         WHERE purpose='observed_position'
                           AND assignment_version=? AND position BETWEEN 1 AND 5
                         GROUP BY match_id
                        HAVING COUNT(DISTINCT player_slot)=10""",
                    (ROLE_VERSION,),
                )
            }
            player_complete = scored & assigned
        except SQLAlchemyError:
            player_complete = set()

    state_complete: set[int] = set()
    if {
        "match_id",
        "side",
        "label_version",
    }.issubset(_relation_columns(connection, "team_map_states")):
        try:
            state_complete = {
                int(row[0])
                for row in connection.execute(
                    """SELECT match_id FROM team_map_states
                         WHERE label_version=?
                         GROUP BY match_id
                        HAVING COUNT(DISTINCT side)=2""",
                    (LABEL_VERSION,),
                )
            }
        except SQLAlchemyError:
            state_complete = set()

    current: set[int] = set()
    player: set[int] = set()
    state: set[int] = set()
    draft_candidates: set[int] = set()
    current_cutoffs: set[str] = set()
    invalid_cutoffs: set[str] = set()
    for row in rows:
        event_id = str(row["event_id"])
        lineage_current = (
            row["eligible_event_id"] is not None
            and str(row["eligible_event_id"]) == event_id
            and row["latest_raw_content_hash"] is not None
            and row["source_content_hash"] == row["latest_raw_content_hash"]
            and row["role_assignment_version"] == ROLE_VERSION
            and row["score_version"] == SCORE_VERSION
            and row["team_state_version"] == LABEL_VERSION
            and row["profile_version"] == PROFILE_VERSION
            and row["derived_normalizer_version"] == row["normalizer_version"]
            and row["benchmark_version"] == BENCHMARK_VERSION
            and row["profile_context_hash"] == contexts.get(event_id)
        )
        match_id = int(row["match_id"])
        cutoff = str(row["profile_cutoff"])
        state_eligible = row["state_readiness"] in {"ready", "unscorable"}
        profile_current = (
            lineage_current and state_eligible and match_id in state_complete
        )
        if state_eligible:
            if profile_current:
                current_cutoffs.add(cutoff)
            else:
                invalid_cutoffs.add(cutoff)
        if not lineage_current:
            continue
        current.add(match_id)
        if row["player_readiness"] == "ready" and match_id in player_complete:
            player.add(match_id)
        if state_eligible and match_id in state_complete:
            state.add(match_id)
        if row["draft_readiness"] == "ready":
            draft_candidates.add(match_id)
    draft_predictions = _current_draft_prediction_keys(
        connection, draft_candidates
    )
    draft = {match_id for _, match_id in draft_predictions}
    return CurrentDerivedScopes(
        available=True,
        formal=formal,
        current=frozenset(current),
        player=frozenset(player),
        state=frozenset(state),
        draft=frozenset(draft),
        draft_predictions=draft_predictions,
        valid_profile_cutoffs=frozenset(current_cutoffs - invalid_cutoffs),
    )


def _sha256_identity(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        return None
    return normalized


def _draft_prediction_artifacts_for_read(
    connection: PostgresSession,
    loader: Callable[
        [PostgresSession], dict[tuple[str, int], tuple[str, str]]
    ],
) -> dict[tuple[str, int], tuple[str, str]]:
    """Build artifact fingerprints from the current PostgreSQL snapshot."""
    return loader(connection)


def _current_draft_prediction_keys(
    connection: PostgresSession,
    match_ids: Collection[int],
) -> frozenset[tuple[str, int]]:
    """Return proofs unaffected by dependency changes at or before their cutoff."""
    if not match_ids:
        return frozenset()
    required = {
        "draft_prediction_validations": {
            "run_id",
            "match_id",
            "input_snapshot_hash",
            "artifact_fingerprint",
            "dependency_fingerprint",
            "dependency_revision",
            "validation_version",
        },
        "draft_predictions": {
            "run_id",
            "match_id",
            "prediction_cutoff",
            "input_snapshot_hash",
        },
    }
    if any(
        not columns.issubset(_relation_columns(connection, relation))
        for relation, columns in required.items()
    ):
        return frozenset()

    from .backtest import (
        DRAFT_VALIDATION_VERSION,
        draft_prediction_artifacts,
        draft_lineage_tracking_is_current,
    )

    def load() -> frozenset[tuple[str, int]]:
        if not draft_lineage_tracking_is_current(connection):
            return frozenset()
        artifacts = _draft_prediction_artifacts_for_read(
            connection, draft_prediction_artifacts
        )
        rows = connection.execute(
            """SELECT validation.run_id, validation.match_id,
                      validation.input_snapshot_hash,
                      validation.artifact_fingerprint
                 FROM draft_prediction_validations AS validation
                 JOIN draft_predictions AS prediction
                   ON prediction.run_id=validation.run_id
                  AND prediction.match_id=validation.match_id
                  AND prediction.input_snapshot_hash=
                      validation.input_snapshot_hash
                 JOIN draft_lineage_revisions AS lineage
                   ON lineage.singleton=1
                WHERE validation.validation_version=?
                  AND validation.dependency_revision<=
                      lineage.dependency_revision
                  AND prediction.prediction_cutoff IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM draft_lineage_changes AS change
                       WHERE change.dependency_revision>
                             validation.dependency_revision
                         AND (change.affected_from_unix IS NULL OR
                              change.affected_from_unix<=CAST(EXTRACT(EPOCH FROM
                                  prediction.prediction_cutoff::timestamptz)
                                  AS BIGINT))
                  )""",
            (DRAFT_VALIDATION_VERSION,),
        ).fetchall()
        eligible = set(match_ids)
        return frozenset(
            (str(row[0]), int(row[1]))
            for row in rows
            if int(row[1]) in eligible
            and artifacts.get((str(row[0]), int(row[1])))
            == (str(row[2]), str(row[3]))
        )

    try:
        if connection.in_transaction:
            return load()
        with connection.transaction():
            return load()
    except (TypeError, ValueError, SQLAlchemyError):
        return frozenset()


def _rebuild_current_draft_prediction_keys(
    connection: PostgresSession,
    match_ids: Collection[int],
) -> frozenset[tuple[str, int]]:
    """Expensively rebuild prediction inputs for offline proof refresh."""
    if not match_ids:
        return frozenset()
    required = {
        "draft_model_runs": {
            "run_id",
            "model_version",
            "model_kind",
            "horizon_minutes",
            "availability_mode",
            "training_cutoff",
            "configuration_json",
        },
        "draft_predictions": {
            "run_id",
            "match_id",
            "prediction_cutoff",
            "cutoff_source",
            "input_snapshot_hash",
        },
        "match_ingest_status": {"match_id", "event_id"},
    }
    if any(
        not columns.issubset(_relation_columns(connection, relation))
        for relation, columns in required.items()
    ):
        return frozenset()

    from .backtest import (
        BACKTEST_VERSION,
        HORIZONS,
        MODEL_KINDS,
        _prepare_runs,
        draft_prediction_artifacts,
        load_draft_corpus,
        persisted_draft_artifact_fingerprint,
    )
    from .draft_features import AvailabilityMode, FEATURE_VERSION
    from .draft_model import MODEL_VERSION

    try:
        rows = connection.execute(
            """SELECT run.run_id, prediction.match_id,
                      prediction.input_snapshot_hash,
                      prediction.prediction_cutoff, prediction.cutoff_source,
                      run.model_version, run.model_kind, run.horizon_minutes,
                      run.availability_mode, run.training_cutoff,
                      run.configuration_json, status.event_id
                 FROM draft_predictions AS prediction
                 JOIN draft_model_runs AS run ON run.run_id=prediction.run_id
                 JOIN match_ingest_status AS status
                   ON status.match_id=prediction.match_id"""
        ).fetchall()
    except SQLAlchemyError:
        return frozenset()

    candidates: dict[tuple[str, str, int, float], set[tuple[str, int]]] = {}
    for row in rows:
        try:
            run_id = str(row[0])
            match_id = int(row[1])
            input_hash = _sha256_identity(row[2])
            prediction_cutoff = str(row[3])
            cutoff_source = str(row[4])
            model_version = str(row[5])
            model_kind = str(row[6])
            horizon = int(row[7])
            mode = AvailabilityMode(str(row[8]))
            training_cutoff = str(row[9])
            configuration = json.loads(str(row[10]))
            event_id = str(row[11])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            match_id not in match_ids
            or input_hash is None
            or not run_id
            or not isinstance(configuration, dict)
        ):
            continue
        assignment_version = configuration.get("assignment_version")
        score_version = configuration.get("score_version")
        target_match_id = configuration.get("target_match_id")
        min_samples = configuration.get("min_samples")
        l2_regularization = configuration.get("l2_regularization")
        if (
            not isinstance(assignment_version, str)
            or score_version != score_version_for_role(assignment_version)
            or configuration.get("backtest_version") != BACKTEST_VERSION
            or configuration.get("feature_version") != FEATURE_VERSION
            or model_version != MODEL_VERSION
            or model_kind not in MODEL_KINDS
            or horizon not in HORIZONS
            or isinstance(target_match_id, bool)
            or target_match_id != match_id
            or configuration.get("target_event_id") != event_id
            or configuration.get("cutoff_source") != cutoff_source
            or training_cutoff != prediction_cutoff
            or isinstance(min_samples, bool)
            or not isinstance(min_samples, int)
            or min_samples < 2
            or isinstance(l2_regularization, bool)
            or not isinstance(l2_regularization, (int, float))
            or not math.isfinite(float(l2_regularization))
            or float(l2_regularization) <= 0
        ):
            continue
        candidates.setdefault(
            (
                mode.value,
                assignment_version,
                min_samples,
                float(l2_regularization),
            ),
            set(),
        ).add(
            (run_id, match_id)
        )

    current: set[tuple[str, int]] = set()
    try:
        actual_artifacts = draft_prediction_artifacts(connection)
    except (KeyError, TypeError, ValueError, SQLAlchemyError):
        return frozenset()
    for (
        mode_value,
        assignment_version,
        min_samples,
        l2_regularization,
    ), grouped in candidates.items():
        try:
            corpus = load_draft_corpus(
                connection,
                availability_mode=AvailabilityMode(mode_value),
                assignment_version=assignment_version,
            )
            rebuilt, _, _ = _prepare_runs(
                corpus,
                min_samples=min_samples,
                l2_regularization=l2_regularization,
            )
            expected_artifacts = {
                (row.run_id, row.match_id): (
                    row.input_snapshot_hash,
                    persisted_draft_artifact_fingerprint(row),
                )
                for row in rebuilt
            }
        except (KeyError, TypeError, ValueError, SQLAlchemyError):
            continue
        current.update(
            key
            for key in grouped
            if expected_artifacts.get(key) == actual_artifacts.get(key)
        )
    return frozenset(current)


def _maximum_prediction_cutoff_unix(
    connection: PostgresSession,
    match_ids: Collection[int],
) -> int | None:
    eligible = set(match_ids)
    if not eligible:
        return None
    rows = connection.execute(
        """SELECT match_id,
                  CAST(EXTRACT(EPOCH FROM prediction_cutoff::timestamptz) AS BIGINT)
             FROM draft_predictions
            WHERE prediction_cutoff IS NOT NULL"""
    ).fetchall()
    values = [int(row[1]) for row in rows if int(row[0]) in eligible]
    return max(values) if values else None


def refresh_draft_prediction_validations(
    connection: PostgresSession,
    match_ids: Collection[int] | None = None,
) -> frozenset[tuple[str, int]]:
    """Rebuild and atomically publish proofs if dependencies stayed unchanged."""
    if connection.in_transaction:
        raise RuntimeError("draft validation refresh requires no active transaction")
    from .backtest import (
        DRAFT_VALIDATION_VERSION,
        draft_dependency_fingerprint,
        draft_lineage_tracking_is_current,
        draft_prediction_artifacts,
        ensure_draft_lineage_tracking,
    )

    ensure_draft_lineage_tracking(connection)
    with connection.transaction():
        dependency_fingerprint = draft_dependency_fingerprint(connection)
        revision_row = connection.execute(
            """SELECT dependency_revision, artifact_revision
                 FROM draft_lineage_revisions
                 WHERE singleton=1"""
        ).fetchone()
        if revision_row is None:
            raise RuntimeError("draft lineage revisions are unavailable")
        dependency_revision = int(revision_row[0])
        artifact_revision = int(revision_row[1])
        candidates = (
            set(match_ids)
            if match_ids is not None
            else {
                int(row[0])
                for row in connection.execute(
                    """SELECT match_id FROM formal_map_eligibility
                         WHERE draft_readiness='ready'"""
                ).fetchall()
            }
        )
        current = _rebuild_current_draft_prediction_keys(connection, candidates)
        validation_cutoff_unix = _maximum_prediction_cutoff_unix(
            connection, candidates
        )

    with connection.transaction():
        if not draft_lineage_tracking_is_current(connection):
            raise RuntimeError(
                "draft lineage tracking changed while validation was rebuilding"
            )
        current_revision = connection.execute(
            """SELECT dependency_revision, artifact_revision
                 FROM draft_lineage_revisions
                 WHERE singleton=1
                 FOR UPDATE"""
        ).fetchone()
        if current_revision is None:
            raise RuntimeError("draft lineage revisions are unavailable")
        publish_dependency_revision = int(current_revision[0])
        if publish_dependency_revision < dependency_revision:
            raise RuntimeError("draft dependency revision moved backwards")
        relevant_change = connection.execute(
            """SELECT 1 FROM draft_lineage_changes
                WHERE dependency_revision>?
                  AND (affected_from_unix IS NULL OR
                       (? IS NOT NULL AND affected_from_unix<=?))
                LIMIT 1""",
            (
                dependency_revision,
                validation_cutoff_unix,
                validation_cutoff_unix,
            ),
        ).fetchone()
        if relevant_change is not None:
            raise RuntimeError("draft dependencies changed while validation was rebuilding")
        if int(current_revision[1]) != artifact_revision:
            raise RuntimeError("draft artifacts changed while validation was rebuilding")
        publish_dependency_fingerprint = draft_dependency_fingerprint(connection)
        if (
            publish_dependency_revision == dependency_revision
            and publish_dependency_fingerprint != dependency_fingerprint
        ):
            raise RuntimeError("draft dependencies changed while validation was rebuilding")
        artifacts = draft_prediction_artifacts(connection)
        now = datetime.now(UTC).isoformat()
        if match_ids is None:
            connection.execute("DELETE FROM draft_prediction_validations")
        else:
            connection.executemany(
                "DELETE FROM draft_prediction_validations WHERE match_id=?",
                [(match_id,) for match_id in sorted(candidates)],
            )
        connection.executemany(
            """INSERT INTO draft_prediction_validations
               (run_id, match_id, input_snapshot_hash, artifact_fingerprint,
                dependency_fingerprint, dependency_revision,
                validation_version, validated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_id,
                    match_id,
                    artifacts[(run_id, match_id)][0],
                    artifacts[(run_id, match_id)][1],
                    publish_dependency_fingerprint,
                    publish_dependency_revision,
                    DRAFT_VALIDATION_VERSION,
                    now,
                )
                for run_id, match_id in sorted(current)
            ],
        )
    return current


def current_state_input_hashes(
    connection: PostgresSession,
    scopes: CurrentDerivedScopes | None = None,
) -> dict[int, frozenset[str]]:
    scopes = scopes or current_derived_scopes(connection)
    if not scopes.state or not {
        "match_id",
        "input_hash",
        "label_version",
    }.issubset(_relation_columns(connection, "team_map_states")):
        return {}
    result: dict[int, set[str]] = {}
    try:
        rows = connection.execute(
            """SELECT match_id, input_hash FROM team_map_states
                WHERE label_version=?""",
            (LABEL_VERSION,),
        ).fetchall()
    except SQLAlchemyError:
        return {}
    for row in rows:
        match_id = int(row[0])
        if match_id in scopes.state and row[1] is not None:
            result.setdefault(match_id, set()).add(str(row[1]))
    return {key: frozenset(value) for key, value in result.items()}


def profile_weighting_is_current(
    weighting_json: object,
    state_hashes: Mapping[int, frozenset[str]],
) -> bool:
    try:
        payload = (
            json.loads(weighting_json)
            if isinstance(weighting_json, str)
            else weighting_json
        )
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("maps"), list):
        return False
    for item in payload["maps"]:
        if not isinstance(item, dict):
            return False
        match_id = item.get("match_id")
        state_hash = item.get("state_input_hash")
        if (
            not isinstance(match_id, int)
            or isinstance(match_id, bool)
            or not isinstance(state_hash, str)
            or state_hash not in state_hashes.get(match_id, frozenset())
        ):
            return False
        evidence = item.get("opponent_strength_evidence", [])
        if not isinstance(evidence, list):
            return False
        for row in evidence:
            if (
                not isinstance(row, (list, tuple))
                or len(row) < 2
                or not isinstance(row[0], int)
                or isinstance(row[0], bool)
                or not isinstance(row[1], str)
                or row[1] not in state_hashes.get(row[0], frozenset())
            ):
                return False
    return True


class StrictDerivedPipeline:
    """Complete only maps whose current source version lacks derived lineage."""

    def __init__(self, storage: IntelligenceStorage) -> None:
        self.storage = storage

    def run(
        self,
        match_ids: Collection[int] = (),
        *,
        force: bool = False,
    ) -> DerivedRunReport:
        requested = {int(value) for value in match_ids}
        with self._connection() as connection:
            components = self._pending_components(connection)
            base_pending = set(components.base)
            player_pending = set(components.player)
            state_pending = set(components.state)
            retired = self._retired_ids(connection)
            eligible_requested = self._eligible_ids(connection, requested)
            if force:
                missing = requested - eligible_requested
                if missing:
                    raise ValueError(
                        "formal ready match not found: "
                        + ",".join(str(value) for value in sorted(missing))
                    )
            base_pending.update(eligible_requested)
            player_scope = self._all_player_ready_ids(connection)
            profile_scope = self._all_profile_ready_ids(connection)
            eligible_scope = self._all_eligible_ids(connection)
            player_pending.update(eligible_requested & player_scope)
            state_pending.update(eligible_requested & profile_scope)
            dependency_sources = self._source_snapshots(connection, eligible_scope)
            if retired:
                # Removing a map changes the complete earlier-only corpus. A full
                # rebuild is rare but is the only safe way to refresh final team
                # profiles when the removed map has no later causal successor.
                base_pending = set(eligible_scope)
                player_pending = set(player_scope)
                state_pending = set(profile_scope)
            else:
                player_pending = self._causal_successor_ids(
                    connection, player_pending, component="player"
                )
                state_pending = self._causal_successor_ids(
                    connection, state_pending, component="state"
                )
            pending = base_pending | player_pending | state_pending
            self._require_formal_ids(connection, pending)
            sources = {match_id: dependency_sources[match_id] for match_id in pending}

        if not pending:
            if retired:
                with self._connection() as connection:
                    with connection.transaction():
                        self._verify_retired_ids(connection, retired)
                        self._delete_retired_statuses(connection, retired)
            prospective_pending, prospective_rows = (
                self._complete_pending_prospective_assignments()
            )
            return DerivedRunReport(
                len(requested),
                0,
                0,
                0,
                prospective_pending,
                prospective_rows,
                0,
                0,
                0,
                None,
            )

        ordered = tuple(sorted(pending))
        player_ordered = tuple(sorted(player_pending))
        state_ordered = tuple(sorted(state_pending))
        assignment_rows = 0
        score_rows = 0
        state_rows = 0
        profile_rows = 0
        if player_ordered:
            # These functions read the complete earlier-only history, but persist
            # only the selected maps.
            from scripts.assign_strict_event_roles import (
                AvailabilityMode,
                run_assignment,
            )
            from scripts.score_strict_event_players import run_scoring

            assignments = run_assignment(
                self.storage,
                match_ids=player_ordered,
                availability_mode=AvailabilityMode.RECONSTRUCTED_WALK_FORWARD,
            )
            scores = run_scoring(
                self.storage,
                match_ids=player_ordered,
                assignment_version=ROLE_VERSION,
            )
            assignment_rows = assignments.assignments
            score_rows = scores.scored_players
        cutoff = self._profile_cutoff(ordered)
        if state_ordered:
            from scripts.build_strict_team_profiles import build_strict_profiles

            profiles = build_strict_profiles(
                self.storage,
                cutoff,
                match_ids=state_ordered,
            )
            state_rows = profiles.state_rows
            profile_rows = profiles.profile_rows
        with self._connection() as connection:
            with connection.transaction():
                if self._all_eligible_ids(connection) != eligible_scope:
                    raise RuntimeError("formal eligible scope changed while deriving")
                if self._all_player_ready_ids(connection) != player_scope:
                    raise RuntimeError("formal player scope changed while deriving")
                if self._all_profile_ready_ids(connection) != profile_scope:
                    raise RuntimeError("formal profile scope changed while deriving")
                self._verify_source_snapshots(connection, dependency_sources)
                self._verify_retired_ids(connection, retired)
                self._verify_derived(
                    connection,
                    sources,
                    player_match_ids=player_pending,
                    state_match_ids=state_pending,
                )
                now = datetime.now(UTC).isoformat()
                for match_id in ordered:
                    source = sources[match_id]
                    previous = connection.execute(
                        "SELECT profile_cutoff FROM strict_derived_status WHERE match_id=?",
                        (match_id,),
                    ).fetchone()
                    status_cutoff = (
                        cutoff.isoformat()
                        if match_id in state_pending or previous is None
                        else str(previous[0])
                    )
                    cursor = connection.execute(
                        """INSERT INTO strict_derived_status
                           (match_id, source_content_hash, role_assignment_version,
                            score_version, team_state_version, profile_version,
                            profile_cutoff, derived_at, normalizer_version,
                            benchmark_version, profile_context_hash)
                           SELECT status.match_id, status.latest_raw_content_hash,
                                  ?, ?, ?, ?, ?, ?, status.normalizer_version,
                                  ?, ?
                             FROM match_ingest_status AS status
                            WHERE status.match_id=?
                              AND status.latest_raw_content_hash=?
                              AND status.normalizer_version=?
                           ON CONFLICT(match_id) DO UPDATE SET
                             source_content_hash=excluded.source_content_hash,
                             role_assignment_version=excluded.role_assignment_version,
                             score_version=excluded.score_version,
                             team_state_version=excluded.team_state_version,
                             profile_version=excluded.profile_version,
                             profile_cutoff=excluded.profile_cutoff,
                             normalizer_version=excluded.normalizer_version,
                             benchmark_version=excluded.benchmark_version,
                             profile_context_hash=excluded.profile_context_hash,
                             derived_at=excluded.derived_at""",
                        (
                            ROLE_VERSION,
                            SCORE_VERSION,
                            LABEL_VERSION,
                            PROFILE_VERSION,
                            status_cutoff,
                            now,
                            BENCHMARK_VERSION,
                            source.profile_context_hash,
                            match_id,
                            source.content_hash,
                            source.normalizer_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            f"source version changed while deriving match {match_id}"
                        )
                self._delete_retired_statuses(connection, retired)
        prospective_pending, prospective_rows = (
            self._complete_pending_prospective_assignments(ordered)
        )
        return DerivedRunReport(
            len(requested),
            len(ordered),
            len(ordered),
            assignment_rows,
            prospective_pending,
            prospective_rows,
            score_rows,
            state_rows,
            profile_rows,
            cutoff.isoformat() if state_ordered else None,
        )

    def _complete_pending_prospective_assignments(
        self,
        match_ids: Collection[int] | None = None,
    ) -> tuple[int, int]:
        with self._connection() as connection:
            with connection.transaction():
                authorities = self._pending_prospective_authorities(
                    connection,
                    match_ids,
                )
        if not authorities:
            return 0, 0

        from scripts.assign_strict_event_roles import AvailabilityMode, run_assignment

        report = run_assignment(
            self.storage,
            match_ids=tuple(sorted(authorities)),
            availability_mode=AvailabilityMode.PROSPECTIVE,
            preserve_existing=True,
            required_certification_authorities=authorities,
        )
        return len(authorities), report.inserted

    @classmethod
    def _pending_prospective_authorities(
        cls,
        connection: PostgresSession,
        match_ids: Collection[int] | None = None,
    ) -> dict[int, StrictCertificationAuthority]:
        selected = None if match_ids is None else {int(value) for value in match_ids}
        if selected == set():
            return {}
        restriction = ""
        parameters: list[object] = [PROSPECTIVE_ASSIGNMENT_VERSION]
        if selected is not None:
            placeholders = ",".join("?" for _ in selected)
            restriction = f" AND derived.match_id IN ({placeholders})"
            parameters.extend(sorted(selected))
        rows = connection.execute(
            """SELECT derived.match_id
                 FROM strict_derived_status AS derived
                 JOIN match_ingest_status AS status USING(match_id)
                 JOIN formal_map_eligibility AS eligible USING(match_id)
                WHERE eligible.player_readiness='ready'
                  AND eligible.event_id=status.event_id
                  AND derived.source_content_hash=status.latest_raw_content_hash
                  AND NOT EXISTS (
                      SELECT 1
                        FROM player_role_assignments AS roles
                       WHERE roles.match_id=derived.match_id
                         AND roles.assignment_version=?
                       GROUP BY roles.match_id
                      HAVING COUNT(*)=20
                         AND COUNT(DISTINCT CASE
                               WHEN roles.purpose='expected_position'
                               THEN roles.player_slot END)=10
                         AND COUNT(DISTINCT CASE
                               WHEN roles.purpose='observed_position'
                               THEN roles.player_slot END)=10
                  )"""
            + restriction,
            tuple(parameters),
        ).fetchall()
        authorities = strict_certification_authorities(
            connection,
            {int(row["match_id"]) for row in rows},
        )
        return {
            match_id: authority
            for match_id, authority in authorities.items()
            if authority.source_content_hash == authority.latest_raw_content_hash
            and authority.role_assignment_version == ROLE_VERSION
            and authority.score_version == SCORE_VERSION
            and authority.team_state_version == LABEL_VERSION
            and authority.profile_version == PROFILE_VERSION
            and authority.derived_normalizer_version
            == authority.status_normalizer_version
            and authority.benchmark_version == BENCHMARK_VERSION
            and authority.status_event_id == authority.eligible_event_id
            and authority.player_readiness == "ready"
            and authority.profile_context_hash
            == authority.current_profile_context_hash
        }

    @contextmanager
    def _connection(self) -> Iterator[PostgresSession]:
        yield self.storage.connection

    @staticmethod
    def _profile_context_hashes(
        connection: PostgresSession,
        event_ids: Collection[str],
    ) -> dict[str, str]:
        if not event_ids:
            return {}
        placeholders = ",".join("?" for _ in event_ids)
        rows = connection.execute(
            f"""SELECT {', '.join(_PROFILE_CONTEXT_FIELDS)}
                  FROM event_registry
                 WHERE event_id IN ({placeholders})""",
            tuple(sorted(event_ids)),
        ).fetchall()
        hashes: dict[str, str] = {}
        for row in rows:
            context: dict[str, object] = {
                "context_version": _PROFILE_CONTEXT_VERSION,
            }
            for field in _PROFILE_CONTEXT_FIELDS:
                value = row[field]
                if field in _PROFILE_CONTEXT_JSON_FIELDS:
                    try:
                        value = json.loads(str(value))
                    except (TypeError, ValueError):
                        value = str(value)
                context[field.removesuffix("_json")] = value
            encoded = json.dumps(
                context,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            hashes[str(row["event_id"])] = hashlib.sha256(encoded).hexdigest()
        return hashes

    @staticmethod
    def _require_formal_ids(
        connection: PostgresSession, match_ids: Collection[int]
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

    def _pending_ids(self, connection: PostgresSession) -> set[int]:
        return self._pending_components(connection).all

    def _pending_components(
        self, connection: PostgresSession
    ) -> _PendingComponents:
        rows = connection.execute(
            """SELECT eligible.match_id, eligible.event_id,
                      eligible.player_readiness, eligible.state_readiness,
                      eligible.draft_readiness,
                      status.latest_raw_content_hash, status.normalizer_version,
                      derived.match_id AS derived_match_id,
                      derived.source_content_hash,
                      derived.role_assignment_version,
                      derived.score_version,
                      derived.team_state_version,
                      derived.profile_version,
                      derived.normalizer_version AS derived_normalizer_version,
                      derived.benchmark_version,
                      derived.profile_context_hash
                 FROM formal_map_eligibility AS eligible
                 JOIN match_ingest_status AS status
                   ON status.match_id=eligible.match_id
                 LEFT JOIN strict_derived_status AS derived
                   ON derived.match_id=eligible.match_id
                WHERE (eligible.player_readiness='ready'
                       OR eligible.state_readiness IN ('ready', 'unscorable')
                       OR eligible.draft_readiness='ready')
                  AND status.latest_raw_content_hash IS NOT NULL"""
        ).fetchall()
        contexts = self._profile_context_hashes(
            connection, {str(row["event_id"]) for row in rows}
        )
        scored = {
            int(row[0])
            for row in connection.execute(
                """SELECT match_id FROM player_map_scores
                     WHERE score_version=?
                     GROUP BY match_id
                    HAVING COUNT(DISTINCT player_slot)=10""",
                (SCORE_VERSION,),
            )
        }
        assigned = {
            int(row[0])
            for row in connection.execute(
                """SELECT match_id FROM player_role_assignments
                     WHERE purpose='observed_position'
                       AND assignment_version=? AND position BETWEEN 1 AND 5
                     GROUP BY match_id
                    HAVING COUNT(DISTINCT player_slot)=10""",
                (ROLE_VERSION,),
            )
        }
        state_complete = {
            int(row[0])
            for row in connection.execute(
                """SELECT match_id FROM team_map_states
                     WHERE label_version=?
                     GROUP BY match_id
                    HAVING COUNT(DISTINCT side)=2""",
                (LABEL_VERSION,),
            )
        }
        player_complete = scored & assigned
        base_pending: set[int] = set()
        player_pending: set[int] = set()
        state_pending: set[int] = set()
        for row in rows:
            event_context = contexts.get(str(row["event_id"]))
            if event_context is None:
                raise ValueError(f"missing registry context for {row['event_id']}")
            match_id = int(row["match_id"])
            lineage_stale = (
                row["derived_match_id"] is None
                or row["source_content_hash"] != row["latest_raw_content_hash"]
                or row["role_assignment_version"] != ROLE_VERSION
                or row["score_version"] != SCORE_VERSION
                or row["team_state_version"] != LABEL_VERSION
                or row["profile_version"] != PROFILE_VERSION
                or row["derived_normalizer_version"] != row["normalizer_version"]
                or row["benchmark_version"] != BENCHMARK_VERSION
                or row["profile_context_hash"] != event_context
            )
            if lineage_stale:
                base_pending.add(match_id)
            if row["player_readiness"] == "ready" and (
                lineage_stale or match_id not in player_complete
            ):
                player_pending.add(match_id)
            if row["state_readiness"] in {"ready", "unscorable"} and (
                lineage_stale or match_id not in state_complete
            ):
                state_pending.add(match_id)
        return _PendingComponents(
            base=frozenset(base_pending),
            player=frozenset(player_pending),
            state=frozenset(state_pending),
        )

    @staticmethod
    def _eligible_ids(
        connection: PostgresSession, match_ids: Collection[int]
    ) -> set[int]:
        if not match_ids:
            return set()
        placeholders = ",".join("?" for _ in match_ids)
        return {
            int(row[0])
            for row in connection.execute(
                f"""SELECT match_id FROM formal_map_eligibility
                     WHERE match_id IN ({placeholders})
                       AND (player_readiness='ready'
                            OR state_readiness IN ('ready', 'unscorable')
                            OR draft_readiness='ready')""",
                tuple(match_ids),
            )
        }

    @staticmethod
    def _all_eligible_ids(connection: PostgresSession) -> set[int]:
        return {
            int(row[0])
            for row in connection.execute(
                """SELECT match_id FROM formal_map_eligibility
                    WHERE player_readiness='ready'
                       OR state_readiness IN ('ready', 'unscorable')
                       OR draft_readiness='ready'"""
            )
        }

    @staticmethod
    def _all_player_ready_ids(connection: PostgresSession) -> set[int]:
        return {
            int(row[0])
            for row in connection.execute(
                """SELECT match_id FROM formal_map_eligibility
                    WHERE player_readiness='ready'"""
            )
        }

    @staticmethod
    def _all_profile_ready_ids(connection: PostgresSession) -> set[int]:
        return {
            int(row[0])
            for row in connection.execute(
                """SELECT match_id FROM formal_map_eligibility
                    WHERE state_readiness IN ('ready', 'unscorable')"""
            )
        }

    @staticmethod
    def _retired_ids(connection: PostgresSession) -> set[int]:
        """Previously certified maps no longer admitted by the formal view."""
        return {
            int(row[0])
            for row in connection.execute(
                """SELECT derived.match_id
                     FROM strict_derived_status AS derived
                     LEFT JOIN formal_map_eligibility AS eligible
                       ON eligible.match_id=derived.match_id
                    WHERE eligible.match_id IS NULL"""
            )
        }

    @staticmethod
    def _verify_retired_ids(
        connection: PostgresSession, match_ids: Collection[int]
    ) -> None:
        if not match_ids:
            return
        placeholders = ",".join("?" for _ in match_ids)
        returned = {
            int(row[0])
            for row in connection.execute(
                f"SELECT match_id FROM formal_map_eligibility "
                f"WHERE match_id IN ({placeholders})",
                tuple(match_ids),
            )
        }
        if returned:
            raise RuntimeError(
                "formal scope changed while deriving retired maps: "
                + ",".join(str(value) for value in sorted(returned))
            )

    @staticmethod
    def _delete_retired_statuses(
        connection: PostgresSession, match_ids: Collection[int]
    ) -> None:
        if not match_ids:
            return
        placeholders = ",".join("?" for _ in match_ids)
        connection.execute(
            f"DELETE FROM strict_derived_status WHERE match_id IN ({placeholders})",
            tuple(match_ids),
        )

    @staticmethod
    def _causal_successor_ids(
        connection: PostgresSession,
        match_ids: Collection[int],
        *,
        component: str,
    ) -> set[int]:
        if not match_ids:
            return set()
        if component == "player":
            readiness = "eligible.player_readiness='ready'"
        elif component == "state":
            readiness = "eligible.state_readiness IN ('ready', 'unscorable')"
        else:
            raise ValueError("component must be player or state")
        placeholders = ",".join("?" for _ in match_ids)
        row = connection.execute(
            f"""SELECT status.start_time, status.match_id
                  FROM match_ingest_status AS status
                 WHERE status.match_id IN ({placeholders})
                 ORDER BY status.start_time, status.match_id
                 LIMIT 1""",
            tuple(match_ids),
        ).fetchone()
        if row is None or row["start_time"] is None:
            raise ValueError("affected maps must have a start time")
        return {
            int(candidate[0])
            for candidate in connection.execute(
                f"""SELECT eligible.match_id
                     FROM formal_map_eligibility AS eligible
                     JOIN match_ingest_status AS status USING(match_id)
                    WHERE {readiness}
                      AND (status.start_time>?
                           OR (status.start_time=? AND status.match_id>=?))""",
                (int(row["start_time"]), int(row["start_time"]), int(row["match_id"])),
            )
        }

    @staticmethod
    def _source_snapshots(
        connection: PostgresSession, match_ids: Collection[int]
    ) -> dict[int, _SourceSnapshot]:
        if not match_ids:
            return {}
        placeholders = ",".join("?" for _ in match_ids)
        rows = connection.execute(
            f"""SELECT match_id, event_id, latest_raw_content_hash,
                      normalizer_version
                  FROM match_ingest_status
                 WHERE match_id IN ({placeholders})""",
            tuple(match_ids),
        ).fetchall()
        contexts = StrictDerivedPipeline._profile_context_hashes(
            connection, {str(row["event_id"]) for row in rows}
        )
        snapshots: dict[int, _SourceSnapshot] = {}
        for row in rows:
            content_hash = row["latest_raw_content_hash"]
            normalizer_version = row["normalizer_version"]
            profile_context_hash = contexts.get(str(row["event_id"]))
            if (
                content_hash is None
                or not str(normalizer_version or "").strip()
                or profile_context_hash is None
            ):
                raise ValueError(
                    f"affected map {row['match_id']} has no complete source version"
                )
            snapshots[int(row["match_id"])] = _SourceSnapshot(
                str(content_hash), str(normalizer_version), profile_context_hash
            )
        missing = set(match_ids) - set(snapshots)
        if missing:
            raise ValueError(
                "formal ready match not found: "
                + ",".join(str(value) for value in sorted(missing))
            )
        return snapshots

    @staticmethod
    def _verify_source_snapshots(
        connection: PostgresSession,
        sources: Mapping[int, _SourceSnapshot],
    ) -> None:
        current = StrictDerivedPipeline._source_snapshots(connection, sources)
        changed = [
            match_id
            for match_id, source in sources.items()
            if current.get(match_id) != source
        ]
        if changed:
            raise RuntimeError(
                "source version changed while deriving matches: "
                + ",".join(str(value) for value in sorted(changed))
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
        connection: PostgresSession,
        sources: Mapping[int, _SourceSnapshot],
        *,
        player_match_ids: Collection[int] | None = None,
        state_match_ids: Collection[int] | None = None,
    ) -> None:
        player_ids = (
            set(sources) if player_match_ids is None else set(player_match_ids)
        )
        state_ids = set(sources) if state_match_ids is None else set(state_match_ids)
        for match_id, source in sources.items():
            if match_id in player_ids:
                role_count = connection.execute(
                    """SELECT COUNT(DISTINCT player_slot)
                         FROM player_role_assignments
                        WHERE match_id=? AND purpose='observed_position'
                          AND assignment_version=? AND position BETWEEN 1 AND 5""",
                    (match_id, ROLE_VERSION),
                ).fetchone()[0]
                score_rows = connection.execute(
                    "SELECT explanation_json FROM player_map_scores "
                    "WHERE match_id=? AND score_version=?",
                    (match_id, SCORE_VERSION),
                ).fetchall()
                if (role_count, len(score_rows)) != (10, 10):
                    raise RuntimeError(
                        f"derived player cardinality mismatch for {match_id}: "
                        f"roles={role_count}, scores={len(score_rows)}"
                    )
                benchmark_versions: list[object] = []
                for score in score_rows:
                    try:
                        benchmark_versions.append(
                            dict(json.loads(score["explanation_json"])).get(
                                "benchmark_version"
                            )
                        )
                    except (TypeError, ValueError):
                        benchmark_versions.append(None)
                if any(value != BENCHMARK_VERSION for value in benchmark_versions):
                    raise RuntimeError(
                        f"derived benchmark version mismatch for match {match_id}"
                    )
            if match_id in state_ids:
                states = connection.execute(
                    "SELECT source_versions_json FROM team_map_states "
                    "WHERE match_id=? AND label_version=?",
                    (match_id, LABEL_VERSION),
                ).fetchall()
                if len(states) != 2:
                    raise RuntimeError(
                        f"derived state cardinality mismatch for {match_id}: "
                        f"states={len(states)}"
                    )
                state_hashes: list[object] = []
                for state in states:
                    try:
                        state_hashes.append(
                            dict(json.loads(state[0])).get("opendota")
                        )
                    except (TypeError, ValueError):
                        state_hashes.append(None)
                if any(value != source.content_hash for value in state_hashes):
                    raise RuntimeError(
                        f"derived source hash mismatch for match {match_id}"
                    )


def strict_certification_authorities(
    connection: PostgresSession,
    match_ids: Collection[int],
) -> dict[int, StrictCertificationAuthority]:
    """Read the complete target certification authority in one snapshot."""

    selected = {int(value) for value in match_ids}
    if not selected:
        return {}
    placeholders = ",".join("?" for _ in selected)
    rows = connection.execute(
        f"""SELECT derived.match_id, derived.source_content_hash,
                   derived.role_assignment_version, derived.score_version,
                   derived.team_state_version, derived.profile_version,
                   derived.profile_cutoff, derived.derived_at,
                   derived.normalizer_version AS derived_normalizer_version,
                   derived.benchmark_version, derived.profile_context_hash,
                   status.event_id AS status_event_id,
                   status.latest_raw_content_hash,
                   status.normalizer_version AS status_normalizer_version,
                   eligible.event_id AS eligible_event_id,
                   eligible.player_readiness, eligible.state_readiness,
                   eligible.draft_readiness
              FROM strict_derived_status AS derived
              JOIN match_ingest_status AS status USING(match_id)
              JOIN formal_map_eligibility AS eligible USING(match_id)
             WHERE derived.match_id IN ({placeholders})""",
        tuple(sorted(selected)),
    ).fetchall()
    contexts = StrictDerivedPipeline._profile_context_hashes(
        connection,
        {str(row["status_event_id"]) for row in rows},
    )

    def text_value(value: object) -> str:
        return "" if value is None else str(value)

    return {
        int(row["match_id"]): StrictCertificationAuthority(
            match_id=int(row["match_id"]),
            source_content_hash=text_value(row["source_content_hash"]),
            role_assignment_version=text_value(row["role_assignment_version"]),
            score_version=text_value(row["score_version"]),
            team_state_version=text_value(row["team_state_version"]),
            profile_version=text_value(row["profile_version"]),
            profile_cutoff=text_value(row["profile_cutoff"]),
            derived_at=text_value(row["derived_at"]),
            derived_normalizer_version=text_value(
                row["derived_normalizer_version"]
            ),
            benchmark_version=text_value(row["benchmark_version"]),
            profile_context_hash=text_value(row["profile_context_hash"]),
            status_event_id=text_value(row["status_event_id"]),
            latest_raw_content_hash=text_value(row["latest_raw_content_hash"]),
            status_normalizer_version=text_value(row["status_normalizer_version"]),
            eligible_event_id=text_value(row["eligible_event_id"]),
            player_readiness=text_value(row["player_readiness"]),
            state_readiness=text_value(row["state_readiness"]),
            draft_readiness=text_value(row["draft_readiness"]),
            current_profile_context_hash=contexts.get(
                text_value(row["status_event_id"]),
                "",
            ),
        )
        for row in rows
    }


__all__ = [
    "CurrentDerivedScopes",
    "DerivedRunReport",
    "ROLE_VERSION",
    "SCORE_VERSION",
    "StrictCertificationAuthority",
    "StrictDerivedPipeline",
    "current_derived_scopes",
    "current_state_input_hashes",
    "profile_weighting_is_current",
    "refresh_draft_prediction_validations",
    "strict_certification_authorities",
]
