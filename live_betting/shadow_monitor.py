"""Read-only live monitor that records comeback decisions and shadow fills."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from event_intelligence.benchmarks import BENCHMARK_VERSION
from event_intelligence.incremental import (
    ROLE_VERSION,
    SCORE_VERSION,
    current_derived_scopes,
    current_state_input_hashes,
    profile_weighting_is_current,
)
from event_intelligence.team_profiles import PROFILE_VERSION

from .alignment import align_snapshots
from .comeback import no_signal_decision
from .draft_authority import DraftLandmarkAuthority, authority_from_curve
from .health import record_health
from .live_player_identity import LivePlayerIdentity, LivePlayerIdentityResolver
from .market_state import build_market_surface
from .models import Market, OddsSnapshot, RoshLineupScore, ShadowOrder
from .profiles import (
    PlayerForm,
    TeamStyleProfile,
    build_draft_curve,
)
from .research import (
    append_research_successor_price_labels,
    record_research_prediction,
)
from .shadow_strategy import ComebackShadowStrategy
from .service_coordination import (
    add_single_database_argument,
    database_writer_authority,
)
from .storage import LiveBettingStore
from .stratz_rosh_client import (
    FetchedRoshLineupScore,
    ROSH_FORMULA_VERSION,
    StratzRoshClient,
    rosh_cache_week_start,
)
from .strict_eligibility import query_strict_live_eligibility
from .vision import VisionComebackState, VisionObservation, read_jsonl


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
MAX_VISION_AGE = timedelta(seconds=30)
MAX_ODDS_TRANSPORT_AGE = timedelta(seconds=15)
ROSH_BACKGROUND_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True)
class TransportRef:
    observation_key: str
    observed_at: datetime
    state_hash: str


@dataclass(frozen=True)
class RoshFetchKey:
    radiant_heroes: tuple[int, ...]
    dire_heroes: tuple[int, ...]
    radiant_players: tuple[int | None, ...]
    dire_players: tuple[int | None, ...]
    player_identity_evidence_hash: str | None
    cache_week_start: int


@dataclass
class _PendingRoshFetch:
    future: Future[FetchedRoshLineupScore]
    submitted_at: float
    timeout_logged: bool = False


class RoshFetchCoordinator:
    """One-worker, non-blocking coordinator; background code never sees SQLite."""

    def __init__(
        self,
        *,
        executor: ThreadPoolExecutor | None = None,
        client_factory: Callable[[], StratzRoshClient] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_seconds: float = ROSH_BACKGROUND_TIMEOUT_SECONDS,
    ) -> None:
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="stratz-rosh",
        )
        self._owns_executor = executor is None
        self._client_factory = client_factory or (
            lambda: StratzRoshClient(timeout_seconds=5.0)
        )
        self._monotonic = monotonic
        self._timeout_seconds = timeout_seconds
        self._active: tuple[RoshFetchKey, _PendingRoshFetch] | None = None
        self._closed = False

    def poll_or_submit(
        self,
        key: RoshFetchKey,
        *,
        radiant_heroes: tuple[int, ...],
        dire_heroes: tuple[int, ...],
        query_started_at: datetime,
        radiant_players: tuple[int | None, ...],
        dire_players: tuple[int | None, ...],
        player_identity_evidence: Mapping[str, Any] | None,
    ) -> FetchedRoshLineupScore | None:
        if self._closed:
            return None
        now = self._monotonic()
        if self._active is not None:
            active_key, pending = self._active
            expired = now - pending.submitted_at > self._timeout_seconds
            if not pending.future.done():
                if expired and not pending.timeout_logged:
                    pending.timeout_logged = True
                    logger.warning("STRATZ Rosh background fetch timed out")
                return None
            self._active = None
            if expired or active_key != key:
                try:
                    pending.future.result()
                except Exception as error:
                    logger.warning(
                        "STRATZ Rosh background fetch failed (%s)",
                        type(error).__name__,
                    )
                # The completed result is stale for this poll. After sweeping
                # it, the current key may occupy the sole worker slot below.
            else:
                try:
                    return pending.future.result()
                except Exception as error:
                    logger.warning(
                        "STRATZ Rosh background fetch failed (%s)",
                        type(error).__name__,
                    )
                    return None
        future = self._executor.submit(
            self._fetch,
            radiant_heroes,
            dire_heroes,
            query_started_at,
            radiant_players,
            dire_players,
            dict(player_identity_evidence) if player_identity_evidence else None,
        )
        self._active = (key, _PendingRoshFetch(future, now))
        return None

    def _fetch(
        self,
        radiant_heroes: tuple[int, ...],
        dire_heroes: tuple[int, ...],
        query_started_at: datetime,
        radiant_players: tuple[int | None, ...],
        dire_players: tuple[int | None, ...],
        player_identity_evidence: Mapping[str, Any] | None,
    ) -> FetchedRoshLineupScore:
        return self._client_factory().fetch_lineup_score(
            radiant_heroes,
            dire_heroes,
            as_of=query_started_at,
            radiant_player_ids=radiant_players,
            dire_player_ids=dire_players,
            player_identity_evidence=player_identity_evidence,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._active is not None:
            self._active[1].future.cancel()
            self._active = None
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "RoshFetchCoordinator":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True)
class _VersionedTeamStyle(TeamStyleProfile):
    profile_cutoff: str = ""
    profile_version: str = ""
    input_hash: str = ""
    effective_sample_size: float = 0.0


@dataclass(frozen=True)
class _VersionedPlayerForm(PlayerForm):
    score_refs: tuple[tuple[int, int, str, str, str], ...] = ()
    cutoff: str = ""


def _snapshot(row: sqlite3.Row) -> OddsSnapshot:
    market = Market(
        str(row["market_type"]), str(row["period"]), row["side"], row["line"],
        str(row["outcome_key"]), bool(row["supported"]),
    )
    return OddsSnapshot(
        str(row["raybet_match_id"]), str(row["odds_id"]), row["odds_group_id"],
        datetime.fromisoformat(str(row["received_at"])), float(row["price"]),
        row["status"], market, row["last_update"],
        json.loads(row["raw_json"]) if row["raw_json"] is not None else {},
    )


def latest_market_state(
    connection: sqlite3.Connection, match_id: str, map_number: int,
    *, as_of: datetime | None = None,
) -> list[OddsSnapshot]:
    as_of = as_of or datetime.now(timezone.utc)
    period = f"map_{map_number}"
    rows = connection.execute(
        """WITH ranked AS (
               SELECT o.*, ROW_NUMBER() OVER (
                   PARTITION BY odds_id ORDER BY received_at DESC, id DESC
               ) AS state_rank
               FROM odds_snapshots o
               WHERE raybet_match_id=? AND period=? AND received_at<=?
           )
           SELECT * FROM ranked o
           WHERE state_rank=1
             AND o.status IN ('1', 'open', 'active', 'running')""",
        (match_id, period, as_of.isoformat()),
    ).fetchall()
    snapshots = [_snapshot(row) for row in rows]
    groups: dict[str, list[OddsSnapshot]] = {}
    for row in snapshots:
        if row.market.market_type == "winner" and row.odds_group_id:
            groups.setdefault(row.odds_group_id, []).append(row)
    complete = [rows for rows in groups.values()
                if {row.market.side for row in rows} == {"team_one", "team_two"}]
    if not complete:
        return []
    winner_group = max(complete, key=lambda group: max(row.received_at for row in group))
    winner_ids = {row.odds_id for row in winner_group}
    return [row for row in snapshots
            if row.market.market_type != "winner" or row.odds_id in winner_ids]


def _observation(row: sqlite3.Row) -> VisionObservation:
    stored_confirmed = (
        bool(row["confirmed"]) if "confirmed" in row.keys() else True
    )
    return VisionObservation(
        str(row["raybet_match_id"]), row["map_number"],
        datetime.fromisoformat(str(row["captured_at"])), row["game_clock_seconds"],
        None if row["is_paused"] is None else bool(row["is_paused"]),
        tuple(json.loads(row["radiant_hero_ids"])),
        tuple(json.loads(row["dire_hero_ids"])),
        float(row["clock_confidence"]) if stored_confirmed else 0.0,
        float(row["draft_confidence"]) if stored_confirmed else 0.0,
        str(row["source_frame_ref"]),
        str(row["screen_state"]), row["radiant_team_side"],
        source_frame_sha256=(
            None
            if "source_frame_sha256" not in row.keys()
            or row["source_frame_sha256"] is None
            else str(row["source_frame_sha256"])
        ),
        source_frame_bytes=(
            None
            if "source_frame_bytes" not in row.keys()
            or row["source_frame_bytes"] is None
            else int(row["source_frame_bytes"])
        ),
    )


def _persist_decision(
    store: LiveBettingStore,
    decision: Any,
    *,
    draft_authority: DraftLandmarkAuthority | None = None,
    vision_observation: VisionObservation | None = None,
    vision_transport_key: str | None = None,
) -> bool:
    """Persist full inputs without changing the public numeric contributions."""
    inputs = getattr(decision, "inputs", None)
    if inputs is None or not hasattr(decision, "contributions"):
        return store.insert_decision(
            decision,
            draft_authority=draft_authority,
            vision_observation=vision_observation,
            vision_transport_key=vision_transport_key,
        )
    audit_contributions: dict[str, Any] = {
        **decision.contributions,
        "__inputs__": inputs,
    }
    conservative = inputs.get("conservative_contributions")
    if conservative is not None:
        audit_contributions["__conservative__"] = conservative
    return store.insert_decision(
        replace(decision, contributions=audit_contributions),
        draft_authority=draft_authority,
        vision_observation=vision_observation,
        vision_transport_key=vision_transport_key,
    )


def _source_vision_observations(path: Path) -> list[VisionObservation]:
    if not path.exists():
        return []
    paths = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    return [
        row
        for item in paths
        for row in read_jsonl(item)
    ]


def ingest_vision(store: LiveBettingStore, path: Path) -> int:
    return sum(
        store.insert_vision_observation(row)
        for row in _source_vision_observations(path)
    )


def _vision_observation_identity(
    observation: VisionObservation,
) -> tuple[str, int | None, datetime, str]:
    return (
        observation.raybet_match_id,
        observation.map_number,
        observation.captured_at.astimezone(timezone.utc),
        observation.source_frame_ref,
    )


def _source_comeback_state_index(
    path: Path,
) -> dict[tuple[str, int | None, datetime, str], VisionComebackState]:
    result: dict[
        tuple[str, int | None, datetime, str], VisionComebackState
    ] = {}
    conflicts: set[tuple[str, int | None, datetime, str]] = set()
    for observation in _source_vision_observations(path):
        identity = _vision_observation_identity(observation)
        previous = result.get(identity)
        if previous is not None and previous != observation.comeback_state:
            conflicts.add(identity)
            continue
        result[identity] = observation.comeback_state
    for identity in conflicts:
        result.pop(identity, None)
    return result


def _bind_source_comeback_state(
    observation: VisionObservation,
    states: Mapping[
        tuple[str, int | None, datetime, str], VisionComebackState
    ],
) -> VisionObservation:
    state = states.get(_vision_observation_identity(observation))
    return observation if state is None else replace(observation, comeback_state=state)


def _as_of_iso(as_of: datetime | None) -> str:
    """Return a strict UTC cutoff for causal reads."""
    resolved = as_of or datetime.now(timezone.utc)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return resolved.astimezone(timezone.utc).isoformat()


def persist_alignments(
    store: LiveBettingStore,
    match_id: str,
    as_of: datetime | None = None,
) -> int:
    cutoff = _as_of_iso(as_of)
    odds_rows = store.connection.execute(
        """SELECT o.* FROM odds_snapshots o LEFT JOIN odds_alignments a
             ON a.odds_snapshot_id=o.id
           WHERE o.raybet_match_id=?
             AND a.odds_snapshot_id IS NULL
             AND julianday(o.received_at)<=julianday(?)
           ORDER BY o.received_at, o.id LIMIT 2000""",
        (match_id, cutoff),
    ).fetchall()
    observations = [_observation(row) for row in store.connection.execute(
        """SELECT observation.* FROM vision_observations AS observation
             LEFT JOIN vision_draft_anchors AS anchor
               ON anchor.raybet_match_id=observation.raybet_match_id
              AND anchor.map_number=observation.map_number
            WHERE observation.raybet_match_id=?
              AND julianday(observation.captured_at)<=julianday(?)
              AND (
                    observation.map_number IS NULL
                    OR anchor.status='anchored'
                    OR (
                        anchor.status='conflict'
                        AND anchor.conflict_at IS NOT NULL
                        AND julianday(anchor.conflict_at) IS NOT NULL
                        AND julianday(anchor.conflict_at)>julianday(?)
                        AND NOT EXISTS (
                            SELECT 1 FROM vision_draft_conflicts AS conflict
                             WHERE conflict.raybet_match_id=anchor.raybet_match_id
                               AND conflict.map_number=anchor.map_number
                               AND (
                                     julianday(conflict.captured_at) IS NULL
                                     OR julianday(conflict.captured_at)<=julianday(?)
                               )
                        )
                    )
              )
           ORDER BY observation.captured_at""", (match_id, cutoff, cutoff, cutoff)
    )]
    aligned = align_snapshots(
        [(int(row["id"]), _snapshot(row)) for row in odds_rows], observations
    )
    return sum(store.insert_alignment(row) for row in aligned)


def _neutral_style() -> TeamStyleProfile:
    return TeamStyleProfile(0, 0, 0.18, 0.16, 0.84, 0.35, 36.0, 0.0)


def _neutral_form() -> PlayerForm:
    return PlayerForm((), 0.0, {}, 0, 0.0)


def _parse_utc(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _latest_versioned_style(
    connection: sqlite3.Connection,
    team_id: int,
    cutoff: datetime,
    *,
    valid_profile_cutoffs: frozenset[str] | None = None,
    state_hashes: dict[int, frozenset[str]] | None = None,
) -> TeamStyleProfile:
    if valid_profile_cutoffs is not None and not valid_profile_cutoffs:
        return _neutral_style()
    cutoff_iso = cutoff.isoformat()
    cutoff_filter = ""
    parameters: list[object] = [team_id, cutoff_iso, cutoff_iso, PROFILE_VERSION]
    if valid_profile_cutoffs is not None:
        placeholders = ",".join("?" for _ in valid_profile_cutoffs)
        cutoff_filter = f" AND profile_cutoff IN ({placeholders})"
        parameters.extend(sorted(valid_profile_cutoffs))
    try:
        rows = connection.execute(
            f"""SELECT * FROM team_style_profiles
               WHERE team_id=? AND profile_cutoff<=? AND created_at<=?
                 AND profile_version=?{cutoff_filter}
               ORDER BY profile_cutoff DESC, created_at DESC, profile_id DESC""",
            tuple(parameters),
        ).fetchall()
    except sqlite3.OperationalError:
        return _neutral_style()
    for row in rows:
        try:
            rates = {
                str(item["metric"]): item
                for item in json.loads(str(row["posterior_rates_json"]))
            }
            durations = {
                str(item["group"]): item
                for item in json.loads(str(row["duration_quantiles_json"]))
            }
            weighting = json.loads(str(row["weighting_json"]))
            if state_hashes is not None and not profile_weighting_is_current(
                row["weighting_json"], state_hashes
            ):
                continue
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue

        def rate(metric: str, default: float) -> float:
            value = rates.get(metric, {}).get("mean")
            return float(value) if isinstance(value, (int, float)) else default

        duration_values = [
            float(value["p50"])
            for key, value in durations.items()
            if key in {"win", "loss", "even"}
            and isinstance(value.get("p50"), (int, float))
        ]
        effective_sample_size = max(0.0, float(row["effective_sample_size"]))
        quality = min(1.0, (effective_sample_size / 100.0) ** 0.5)
        maps = weighting.get("maps", []) if isinstance(weighting, dict) else []
        return _VersionedTeamStyle(
            team_id=team_id,
            matches=len(maps) if isinstance(maps, list) else 0,
            comeback_rate=rate("comeback_after_5000_deficit", 0.18),
            throw_rate=rate("throw_after_5000_lead", 0.16),
            closeout_rate=rate("closeout_after_5000_lead", 0.84),
            late_game_rate=rate("reach_40_minutes", 0.35),
            average_duration_minutes=(
                sum(duration_values) / len(duration_values) / 60.0
                if duration_values
                else 36.0
            ),
            quality=quality,
            profile_cutoff=str(row["profile_cutoff"]),
            profile_version=str(row["profile_version"]),
            input_hash=str(row["input_hash"]),
            effective_sample_size=effective_sample_size,
        )
    return _neutral_style()


def _latest_completed_roster(
    connection: sqlite3.Connection,
    team_id: int,
    cutoff_epoch: int,
) -> tuple[int, ...]:
    try:
        cutoff_iso = datetime.fromtimestamp(
            cutoff_epoch, tz=timezone.utc
        ).isoformat()
        row = connection.execute(
            """SELECT m.match_id
               FROM formal_map_eligibility AS f
               JOIN matches AS m ON m.match_id=f.match_id
               JOIN match_ingest_status AS status ON status.match_id=m.match_id
               JOIN raw_source_artifacts AS artifact
                 ON artifact.artifact_id=status.latest_raw_artifact_id
                AND artifact.content_hash=status.latest_raw_content_hash
               WHERE (m.radiant_team_id=? OR m.dire_team_id=?)
                 AND m.duration IS NOT NULL
                 AND m.start_time + m.duration < ?
                 AND status.player_readiness='ready'
                 AND artifact.first_usable_at IS NOT NULL
                 AND artifact.first_usable_at<=?
               ORDER BY m.start_time + m.duration DESC, m.match_id DESC
               LIMIT 1""",
            (team_id, team_id, cutoff_epoch, cutoff_iso),
        ).fetchone()
    except sqlite3.OperationalError:
        return ()
    if row is None:
        return ()
    players = connection.execute(
        """SELECT account_id FROM match_players
           WHERE match_id=? AND team_id=? AND account_id IS NOT NULL
           ORDER BY player_slot""",
        (int(row["match_id"]), team_id),
    ).fetchall()
    roster = tuple(int(player["account_id"]) for player in players)
    return (
        roster
        if len(roster) == 5
        and len(set(roster)) == 5
        and all(account_id > 0 for account_id in roster)
        else ()
    )


def _versioned_player_form(
    connection: sqlite3.Connection,
    account_ids: tuple[int, ...],
    cutoff: datetime,
    *,
    half_life_days: float = 30.0,
    allowed_match_ids: frozenset[int] | None = None,
) -> PlayerForm:
    if not account_ids:
        return _neutral_form()
    if allowed_match_ids is not None and not allowed_match_ids:
        return _neutral_form()
    cutoff_epoch = int(cutoff.timestamp())
    cutoff_iso = cutoff.isoformat()
    placeholders = ",".join("?" for _ in account_ids)
    lineage_join = ""
    lineage_filter = ""
    lineage_parameters: tuple[str, ...] = ()
    if allowed_match_ids is not None:
        lineage_join = "JOIN strict_derived_status AS derived ON derived.match_id=score.match_id"
        lineage_filter = """
                     AND derived.source_content_hash=status.latest_raw_content_hash
                     AND derived.role_assignment_version=?
                     AND derived.score_version=?
                     AND derived.profile_version=?
                     AND derived.normalizer_version=status.normalizer_version
                     AND derived.benchmark_version=?
                     AND derived.profile_context_hash IS NOT NULL"""
        lineage_parameters = (ROLE_VERSION, SCORE_VERSION, PROFILE_VERSION, BENCHMARK_VERSION)
    try:
        rows = connection.execute(
            f"""WITH available AS (
                   SELECT score.*, m.start_time + m.duration AS completed_at,
                          ROW_NUMBER() OVER (
                              PARTITION BY score.account_id, score.match_id,
                                           score.player_slot
                              ORDER BY score.created_at DESC, score.score_id DESC
                          ) AS version_rank
                   FROM player_map_scores AS score
                   JOIN formal_map_eligibility AS f ON f.match_id=score.match_id
                   JOIN matches AS m ON m.match_id=score.match_id
                   JOIN match_ingest_status AS status
                     ON status.match_id=score.match_id
                   {lineage_join}
                   WHERE score.account_id IN ({placeholders})
                     AND score.score_version=?
                     AND score.position IS NOT NULL
                     AND m.duration IS NOT NULL
                     AND m.start_time + m.duration < ?
                     AND score.created_at<=?
                     AND score.benchmark_cutoff<=?
                     AND status.player_readiness='ready'
                     {lineage_filter}
               ), ranked AS (
                   SELECT available.*, ROW_NUMBER() OVER (
                       PARTITION BY account_id
                       ORDER BY completed_at DESC, match_id DESC, player_slot
                   ) AS recent_rank
                   FROM available WHERE version_rank=1
               )
               SELECT * FROM ranked WHERE recent_rank<=20
               ORDER BY completed_at DESC, match_id DESC, player_slot""",
            (
                *account_ids,
                SCORE_VERSION,
                cutoff_epoch,
                cutoff_iso,
                cutoff_iso,
                *lineage_parameters,
            ),
        ).fetchall()
    except sqlite3.OperationalError:
        return _neutral_form()
    if not rows:
        return _neutral_form()

    weighted_values: list[tuple[float, float, str]] = []
    refs: list[tuple[int, int, str, str, str]] = []
    for row in rows:
        if allowed_match_ids is not None and int(row["match_id"]) not in allowed_match_ids:
            continue
        age_days = max(0.0, (cutoff_epoch - int(row["completed_at"])) / 86400.0)
        time_weight = 0.5 ** (age_days / half_life_days)
        reliability = max(
            0.0,
            min(1.0, float(row["coverage"]) * float(row["role_confidence"])),
        )
        weight = time_weight * reliability
        if weight <= 0.0:
            continue
        normalized = max(
            -1.0, min(1.0, (float(row["execution_score"]) - 50.0) / 50.0)
        )
        role = f"position_{int(row['position'])}"
        weighted_values.append((normalized, weight, role))
        refs.append(
            (
                int(row["match_id"]),
                int(row["player_slot"]),
                str(row["input_hash"]),
                str(row["score_version"]),
                str(row["created_at"]),
            )
        )
    if not weighted_values:
        return _neutral_form()
    total_weight = sum(weight for _, weight, _ in weighted_values)
    score = sum(value * weight for value, weight, _ in weighted_values) / total_weight
    role_scores = {}
    for role in {item[2] for item in weighted_values}:
        selected = [item for item in weighted_values if item[2] == role]
        role_scores[role] = sum(value * weight for value, weight, _ in selected) / sum(
            weight for _, weight, _ in selected
        )
    # Current starters are not independently confirmed by RayBet. The latest
    # earlier completed strict roster is therefore useful but deliberately weak.
    coverage = min(1.0, (len(weighted_values) / (len(account_ids) * 20.0)) ** 0.5)
    quality = coverage * 0.35
    return _VersionedPlayerForm(
        account_ids=account_ids,
        score=score,
        role_scores=role_scores,
        matches=len(weighted_values),
        quality=quality,
        score_refs=tuple(refs),
        cutoff=cutoff_iso,
    )


def _profiles(
    connection: sqlite3.Connection, team_id: int | None, as_of: int
) -> tuple[TeamStyleProfile, PlayerForm]:
    if team_id is None:
        return _neutral_style(), _neutral_form()
    cutoff = datetime.fromtimestamp(as_of, tz=timezone.utc)
    roster = _latest_completed_roster(connection, team_id, as_of)
    valid_profile_cutoffs: frozenset[str] | None = None
    allowed_match_ids: frozenset[int] | None = None
    state_hashes: dict[int, frozenset[str]] | None = None
    if _table_exists(connection, "strict_derived_status"):
        try:
            scopes = current_derived_scopes(connection)
            if not scopes.available:
                return _neutral_style(), _neutral_form()
            valid_profile_cutoffs = scopes.valid_profile_cutoffs
            allowed_match_ids = scopes.player
            state_hashes = current_state_input_hashes(connection, scopes)
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            return _neutral_style(), _neutral_form()
    return (
        _latest_versioned_style(
            connection,
            team_id,
            cutoff,
            valid_profile_cutoffs=valid_profile_cutoffs,
            state_hashes=state_hashes,
        ),
        _versioned_player_form(
            connection,
            roster,
            cutoff,
            allowed_match_ids=allowed_match_ids,
        ),
    )


def _profile_refs(
    style: TeamStyleProfile,
    form: PlayerForm,
) -> dict[str, Any]:
    style_refs = (
        {
            "team_id": style.team_id,
            "profile_cutoff": style.profile_cutoff,
            "profile_version": style.profile_version,
            "input_hash": style.input_hash,
            "effective_sample_size": style.effective_sample_size,
        }
        if isinstance(style, _VersionedTeamStyle)
        else {"team_id": style.team_id, "status": "versioned_profile_unavailable"}
    )
    form_refs = (
        {
            "account_ids": list(form.account_ids),
            "cutoff": form.cutoff,
            "score_refs": [
                {
                    "match_id": match_id,
                    "player_slot": player_slot,
                    "input_hash": input_hash,
                    "score_version": score_version,
                    "created_at": created_at,
                }
                for match_id, player_slot, input_hash, score_version, created_at
                in form.score_refs
            ],
        }
        if isinstance(form, _VersionedPlayerForm)
        else {"account_ids": list(form.account_ids), "status": "versioned_scores_unavailable"}
    )
    return {"team_style": style_refs, "player_form": form_refs}


def _shadow_order(row: sqlite3.Row) -> ShadowOrder:
    market_type, period, side, line = str(row["market_key"]).split("|", 3)
    outcome_key = str(row["signal_outcome_key"] or side)
    market = Market(
        market_type,
        period,
        side or None,
        float(line) if line else None,
        outcome_key,
        True,
    )
    return ShadowOrder(
        order_key=str(row["order_key"]),
        raybet_match_id=str(row["raybet_match_id"]),
        odds_id=str(row["odds_id"]),
        market=market,
        signaled_at=datetime.fromisoformat(str(row["signaled_at"])),
        model_probability=float(row["model_probability"]),
        market_probability=float(row["market_probability"]),
        signal_price=float(row["signal_price"]),
        signal_transport_key=str(row["signal_transport_key"]),
        signal_transport_at=datetime.fromisoformat(str(row["signal_transport_at"])),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        signal_odds_group_id=row["signal_odds_group_id"],
        signal_outcome_key=row["signal_outcome_key"],
        signal_identity_verified=bool(row["signal_identity_verified"]),
        stake=float(row["stake"]),
        status=str(row["status"]),
        fill_price=row["fill_price"],
        filled_at=(
            datetime.fromisoformat(str(row["filled_at"]))
            if row["filled_at"]
            else None
        ),
        rejection_reason=row["rejection_reason"],
    )


def _pending_orders(store: LiveBettingStore) -> list[ShadowOrder]:
    rows = store.connection.execute(
        """SELECT o.* FROM shadow_orders o JOIN shadow_map_attempts a
             ON a.order_key=o.order_key
           WHERE o.status='pending' ORDER BY o.signaled_at, o.order_key"""
    ).fetchall()
    return [_shadow_order(row) for row in rows]


def _process_pending_order(
    store: LiveBettingStore, *, as_of: datetime,
) -> ShadowOrder | None:
    for pending in _pending_orders(store):
        watermark = store.processed_transport_watermark(
            pending.raybet_match_id, as_of=as_of
        )
        if watermark is None:
            continue
        resolved = store.process_pending_successor(pending, watermark=watermark)
        if resolved is not None:
            return resolved
    return None


def _transport_refs(
    connection: sqlite3.Connection,
    match_id: str,
    as_of: datetime,
) -> list[TransportRef]:
    rows = connection.execute(
        """SELECT observation_key, observed_at, normalized_state_hash
             FROM odds_transport_observations
           WHERE raybet_match_id=? AND source='direct' AND observed_at<=?
               AND timing_status='on_time' AND processing_status='processed'
           ORDER BY observed_at DESC, observation_key DESC LIMIT 2""",
        (match_id, as_of.isoformat()),
    ).fetchall()
    return [
        TransportRef(
            str(row["observation_key"]),
            datetime.fromisoformat(str(row["observed_at"])),
            str(row["normalized_state_hash"]),
        )
        for row in rows
    ]


def market_state_for_transport(
    connection: sqlite3.Connection,
    transport: TransportRef,
    match_id: str,
    map_number: int,
) -> list[OddsSnapshot]:
    """Read only the exact normalized membership of one captured response."""
    period = f"map_{map_number}"
    try:
        rows = connection.execute(
            """SELECT * FROM odds_response_outcomes_effective
               WHERE observation_key=? AND raybet_match_id=? AND period=?""",
            (transport.observation_key, match_id, period),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_snapshot(row) for row in rows]


def _aligned_transport_observation(
    snapshots: list[OddsSnapshot],
    observed_at: datetime,
    observations: list[VisionObservation],
) -> tuple[VisionObservation | None, str]:
    synthetic = [
        (-(index + 1), replace(snapshot, received_at=observed_at))
        for index, snapshot in enumerate(snapshots)
    ]
    aligned = align_snapshots(synthetic, observations)
    unusable = next((row for row in aligned if not row.usable), None)
    if unusable is not None:
        return None, unusable.reason
    anchors = {row.observation_captured_at for row in aligned}
    clocks = {row.game_clock_seconds for row in aligned}
    maps = {row.map_number for row in aligned}
    if len(anchors) != 1 or len(clocks) != 1 or len(maps) != 1:
        return None, "inconsistent_transport_alignment"
    anchor_at = next(iter(anchors), None)
    game_clock = next(iter(clocks), None)
    map_number = next(iter(maps), None)
    anchor = next(
        (
            row for row in observations
            if row.captured_at == anchor_at and row.map_number == map_number
            and row.is_confirmed and row.screen_state == "game"
        ),
        None,
    )
    if anchor is None or game_clock is None:
        return None, "aligned_observation_missing"
    return replace(anchor, game_clock_seconds=game_clock), "ok"


def _rosh_score_for_trusted_draft(
    store: LiveBettingStore,
    observation: VisionObservation,
    *,
    strict_mapping_id: int,
    as_of: datetime,
    fetch_started_at: datetime,
    player_identity: LivePlayerIdentity | None = None,
    fetch_coordinator: RoshFetchCoordinator | None = None,
) -> RoshLineupScore | None:
    """Load or fetch Rosh with only exact live player identity."""
    draft_hash = store.rosh_draft_hash(
        observation.radiant_hero_ids,
        observation.dire_hero_ids,
    )
    missing_players: tuple[None, ...] = (None,) * 5
    if (
        player_identity is not None
        and player_identity.radiant_hero_ids == observation.radiant_hero_ids
        and player_identity.dire_hero_ids == observation.dire_hero_ids
    ):
        radiant_players: tuple[int | None, ...] = (
            player_identity.radiant_player_ids
        )
        dire_players: tuple[int | None, ...] = player_identity.dire_player_ids
        player_identity_evidence: dict[str, Any] | None = {
            "radiant_team_id": player_identity.radiant_team_id,
            "dire_team_id": player_identity.dire_team_id,
            "source_name": player_identity.source_name,
            "source_match_id": player_identity.source_match_id,
            "fetched_at": player_identity.fetched_at,
            "evidence_hash": player_identity.evidence_hash,
        }
    else:
        radiant_players = missing_players
        dire_players = missing_players
        player_identity_evidence = None
    cached = store.find_rosh_lineup_score(
        draft_hash=draft_hash,
        formula_version=ROSH_FORMULA_VERSION,
        cache_week_start=rosh_cache_week_start(as_of),
        radiant_hero_ids=observation.radiant_hero_ids,
        dire_hero_ids=observation.dire_hero_ids,
        radiant_player_ids=radiant_players,
        dire_player_ids=dire_players,
        as_of=as_of,
    )
    if cached is not None:
        rebound = store.insert_rosh_lineup_score(
            cached,
            raybet_match_id=observation.raybet_match_id,
            map_number=int(observation.map_number or 0),
            strict_mapping_id=strict_mapping_id,
            draft_hash=draft_hash,
            radiant_hero_ids=observation.radiant_hero_ids,
            dire_hero_ids=observation.dire_hero_ids,
            radiant_player_ids=radiant_players,
            dire_player_ids=dire_players,
            created_at=as_of,
        )
        if rebound is not None:
            return rebound
    already_fetched = store.find_rosh_lineup_score(
        draft_hash=draft_hash,
        formula_version=ROSH_FORMULA_VERSION,
        cache_week_start=rosh_cache_week_start(fetch_started_at),
        radiant_hero_ids=observation.radiant_hero_ids,
        dire_hero_ids=observation.dire_hero_ids,
        radiant_player_ids=radiant_players,
        dire_player_ids=dire_players,
        as_of=fetch_started_at,
    )
    if already_fetched is not None:
        store.insert_rosh_lineup_score(
            already_fetched,
            raybet_match_id=observation.raybet_match_id,
            map_number=int(observation.map_number or 0),
            strict_mapping_id=strict_mapping_id,
            draft_hash=draft_hash,
            radiant_hero_ids=observation.radiant_hero_ids,
            dire_hero_ids=observation.dire_hero_ids,
            radiant_player_ids=radiant_players,
            dire_player_ids=dire_players,
            created_at=fetch_started_at,
        )
        return None
    if fetch_coordinator is None:
        return None
    fetch_key = RoshFetchKey(
        radiant_heroes=observation.radiant_hero_ids,
        dire_heroes=observation.dire_hero_ids,
        radiant_players=radiant_players,
        dire_players=dire_players,
        player_identity_evidence_hash=(
            player_identity.evidence_hash if player_identity is not None else None
        ),
        cache_week_start=rosh_cache_week_start(fetch_started_at),
    )
    fetched = fetch_coordinator.poll_or_submit(
        fetch_key,
        radiant_heroes=observation.radiant_hero_ids,
        dire_heroes=observation.dire_hero_ids,
        query_started_at=fetch_started_at,
        radiant_players=radiant_players,
        dire_players=dire_players,
        player_identity_evidence=player_identity_evidence,
    )
    if fetched is None:
        return None
    store.insert_rosh_lineup_score(
        fetched,
        raybet_match_id=observation.raybet_match_id,
        map_number=int(observation.map_number or 0),
        strict_mapping_id=strict_mapping_id,
        draft_hash=draft_hash,
        radiant_hero_ids=observation.radiant_hero_ids,
        dire_hero_ids=observation.dire_hero_ids,
        radiant_player_ids=radiant_players,
        dire_player_ids=dire_players,
        created_at=max(fetch_started_at, fetched.source_as_of),
    )
    # The response did not exist at the current transport cutoff. It becomes
    # eligible only for a later transport after both source and creation time.
    return None


def _canonical_live_team_ids(
    mapping: Any,
    radiant_team_side: str | None,
) -> tuple[int, int] | None:
    if radiant_team_side == "team_one":
        return mapping.canonical_team_one_id, mapping.canonical_team_two_id
    if radiant_team_side == "team_two":
        return mapping.canonical_team_two_id, mapping.canonical_team_one_id
    return None


def run_once(
    store: LiveBettingStore,
    strategy: ComebackShadowStrategy,
    vision_path: Path,
    *,
    now: datetime | None = None,
    player_identity_resolver: LivePlayerIdentityResolver | None = None,
    rosh_fetch_coordinator: RoshFetchCoordinator | None = None,
) -> dict[str, object]:
    run_at = now or datetime.now(timezone.utc)
    comeback_states: dict[
        tuple[str, int | None, datetime, str], VisionComebackState
    ] = {}
    try:
        ingested = ingest_vision(store, vision_path)
        comeback_states = _source_comeback_state_index(vision_path)
    except (OSError, TypeError, ValueError, KeyError) as error:
        # A malformed vision line must not prevent a previously pending shadow
        # order from resolving from its persisted odds successor.
        logger.warning("vision ingestion skipped malformed input (%s)", type(error).__name__)
        ingested = 0
    pending = _process_pending_order(store, as_of=run_at)
    if pending is not None:
        return {
            "status": f"shadow_{pending.status}",
            "order_key": pending.order_key,
            "vision_ingested": ingested,
        }
    row = store.connection.execute(
        """WITH ranked_transport AS (
               SELECT transport.raybet_match_id, transport.observed_at,
                      ROW_NUMBER() OVER (
                          PARTITION BY transport.raybet_match_id
                          ORDER BY transport.observed_at DESC,
                                   transport.observation_key DESC
                      ) AS transport_rank
                 FROM odds_transport_observations AS transport
                WHERE transport.observed_at<=?
                  AND transport.source='direct'
                  AND transport.timing_status='on_time'
                  AND transport.processing_status='processed'
           ), current_transport AS (
               SELECT raybet_match_id, observed_at
                 FROM ranked_transport WHERE transport_rank=1
           )
           SELECT observation.*
             FROM vision_observations AS observation
             JOIN vision_draft_anchors AS anchor
               ON anchor.raybet_match_id=observation.raybet_match_id
              AND anchor.map_number=observation.map_number
             LEFT JOIN current_transport AS transport
               ON transport.raybet_match_id=observation.raybet_match_id
            WHERE observation.confirmed=1
              AND observation.screen_state='game'
              AND (
                    anchor.status='anchored'
                    OR (
                        anchor.status='conflict'
                        AND anchor.conflict_at IS NOT NULL
                        AND julianday(anchor.conflict_at) IS NOT NULL
                        AND julianday(anchor.conflict_at)>julianday(
                            COALESCE(transport.observed_at, ?)
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM vision_draft_conflicts AS conflict
                             WHERE conflict.raybet_match_id=anchor.raybet_match_id
                               AND conflict.map_number=anchor.map_number
                               AND (
                                     julianday(conflict.captured_at) IS NULL
                                     OR julianday(conflict.captured_at)<=julianday(
                                         COALESCE(transport.observed_at, ?)
                                     )
                               )
                        )
                    )
              )
              AND julianday(observation.captured_at)<=julianday(?)
           ORDER BY observation.captured_at DESC LIMIT 1""",
        (
            run_at.isoformat(),
            run_at.isoformat(),
            run_at.isoformat(),
            run_at.isoformat(),
        ),
    ).fetchone()
    if not row:
        return {"status": "waiting_for_confirmed_vision", "vision_ingested": ingested}
    latest_observation = _observation(row)
    match_id = latest_observation.raybet_match_id
    transports = _transport_refs(store.connection, match_id, run_at)
    if not transports:
        return {"status": "waiting_for_odds_transport"}
    current_transport = transports[0]
    current_transport_at = current_transport.observed_at
    if run_at - current_transport_at > MAX_ODDS_TRANSPORT_AGE:
        return {"status": "waiting_for_fresh_odds"}

    causal_row = store.connection.execute(
        """SELECT observation.*
             FROM vision_observations AS observation
             JOIN vision_draft_anchors AS anchor
               ON anchor.raybet_match_id=observation.raybet_match_id
              AND anchor.map_number=observation.map_number
              AND (
                    anchor.status='anchored'
                    OR (
                        anchor.status='conflict'
                        AND anchor.conflict_at IS NOT NULL
                        AND julianday(anchor.conflict_at) IS NOT NULL
                        AND julianday(anchor.conflict_at)>julianday(?)
                        AND NOT EXISTS (
                            SELECT 1 FROM vision_draft_conflicts AS conflict
                             WHERE conflict.raybet_match_id=anchor.raybet_match_id
                               AND conflict.map_number=anchor.map_number
                               AND (
                                     julianday(conflict.captured_at) IS NULL
                                     OR julianday(conflict.captured_at)<=julianday(?)
                               )
                        )
                    )
              )
            WHERE observation.raybet_match_id=?
              AND observation.confirmed=1
              AND observation.screen_state='game'
              AND julianday(observation.captured_at)<=julianday(?)
           ORDER BY observation.captured_at DESC LIMIT 1""",
        (
            current_transport_at.isoformat(),
            current_transport_at.isoformat(),
            match_id,
            current_transport_at.isoformat(),
        ),
    ).fetchone()
    if not causal_row:
        if (
            latest_observation.map_number is not None
            and store._draft_conflict_at_or_before(
                match_id,
                int(latest_observation.map_number),
                current_transport_at,
            )
        ):
            return {
                "status": "waiting_for_confirmed_vision",
                "vision_ingested": ingested,
            }
        return {
            "status": "waiting_for_usable_alignment",
            "reason": "no_prior_confirmed_observation",
        }
    map_number = int(causal_row["map_number"])
    aligned = persist_alignments(store, match_id, as_of=current_transport_at)
    snapshots = market_state_for_transport(
        store.connection, current_transport, match_id, map_number
    )
    if not snapshots:
        return {
            "status": "waiting_for_exact_transport_market",
            "reason": "exact_response_membership_missing",
            "transport_key": current_transport.observation_key,
            "aligned": aligned,
        }
    try:
        surface = build_market_surface(snapshots)
    except ValueError:
        return {
            "status": "waiting_for_complete_winner_market",
            "transport_key": current_transport.observation_key,
            "aligned": aligned,
        }
    successor_price_labels = append_research_successor_price_labels(
        store,
        raybet_match_id=match_id,
        map_number=map_number,
        transport_key=current_transport.observation_key,
        transport_hash=current_transport.state_hash,
        transport_at=current_transport_at,
        snapshots=snapshots,
        created_at=run_at,
    )

    observations = [
        _bind_source_comeback_state(_observation(item), comeback_states)
        for item in store.connection.execute(
            """SELECT observation.*
             FROM vision_observations AS observation
             LEFT JOIN vision_draft_anchors AS anchor
               ON anchor.raybet_match_id=observation.raybet_match_id
              AND anchor.map_number=observation.map_number
            WHERE observation.raybet_match_id=?
              AND julianday(observation.captured_at)<=julianday(?)
              AND (
                    observation.map_number IS NULL
                    OR anchor.status='anchored'
                    OR (
                        anchor.status='conflict'
                        AND anchor.conflict_at IS NOT NULL
                        AND julianday(anchor.conflict_at) IS NOT NULL
                        AND julianday(anchor.conflict_at)>julianday(?)
                        AND NOT EXISTS (
                            SELECT 1 FROM vision_draft_conflicts AS conflict
                             WHERE conflict.raybet_match_id=anchor.raybet_match_id
                               AND conflict.map_number=anchor.map_number
                               AND (
                                     julianday(conflict.captured_at) IS NULL
                                     OR julianday(conflict.captured_at)<=julianday(?)
                               )
                        )
                    )
              )
           ORDER BY observation.captured_at""",
            (
                match_id,
                current_transport_at.isoformat(),
                current_transport_at.isoformat(),
                current_transport_at.isoformat(),
            ),
        )
    ]
    observation, alignment_reason = _aligned_transport_observation(
        snapshots, current_transport_at, observations
    )
    if observation is None:
        return {
            "status": "waiting_for_usable_alignment",
            "reason": alignment_reason,
            "aligned": aligned,
        }
    if run_at - observation.captured_at > MAX_VISION_AGE:
        return {"status": "waiting_for_fresh_vision"}

    strict = query_strict_live_eligibility(
        store.connection,
        raybet_match_id=match_id,
        map_number=map_number,
        transport_observed_at=current_transport_at,
    )
    strict_inputs: dict[str, Any] = {
        "strict_live_eligibility": {
            "eligible": strict.eligible,
            "reason": strict.reason,
            "mapping_refs": strict.input_refs(),
        },
        "transport": {
            "current_key": current_transport.observation_key,
            "current_at": current_transport_at.isoformat(),
        },
    }
    if not strict.eligible or strict.mapping is None:
        decision = no_signal_decision(
            observation=observation,
            surface=surface,
            decided_at=current_transport_at,
            reason=f"strict_live_ineligible:{strict.reason}",
            inputs=strict_inputs,
        )
        _persist_decision(store, decision)
        return {
            "status": "no_signal",
            "reason": "strict_live_ineligible",
            "reason_code": strict.reason,
            "decision_key": decision.decision_key,
            "inputs": decision.inputs,
        }

    as_of = int(current_transport_at.timestamp())
    draft = build_draft_curve(
        store.connection,
        observation.radiant_hero_ids,
        observation.dire_hero_ids,
        as_of,
        raybet_match_id=match_id,
        map_number=map_number,
        strict_mapping_id=strict.mapping.mapping_id,
    )
    research = record_research_prediction(
        store,
        snapshots=snapshots,
        surface=surface,
        observation=observation,
        draft_curve=draft,
        strict_mapping=strict.mapping,
        transport_key=current_transport.observation_key,
        transport_hash=current_transport.state_hash,
        transport_at=current_transport_at,
        created_at=run_at,
    )
    research_payload = (
        None
        if research is None
        else {
            "prediction_key": research.prediction_key,
            "inserted": research.inserted,
            "price_labels_inserted": research.price_labels_inserted,
            "gate_status": research.gate_status,
            "gate_failures": list(research.gate_failures),
            "actionability": "research_only",
        }
    )
    if research_payload is not None:
        research_payload["price_labels_inserted"] = (
            int(research_payload["price_labels_inserted"])
            + successor_price_labels
        )
    active_draft = draft.at(observation.game_clock_seconds or 0)
    if active_draft is None:
        draft_reason = draft.wait_reason(observation.game_clock_seconds or 0)
        draft_inputs = {
            **strict_inputs,
            "draft_curve": {
                "source_ref": draft.source_ref,
                "unavailable_reason": draft.unavailable_reason,
                "selection_reason": draft_reason,
            },
        }
        decision = no_signal_decision(
            observation=observation,
            surface=surface,
            decided_at=current_transport_at,
            reason=f"draft_landmark_unavailable:{draft_reason}",
            inputs=draft_inputs,
        )
        _persist_decision(store, decision)
        return {
            "status": "no_signal",
            "reason": "draft_landmark_unavailable",
            "reason_code": draft_reason,
            "decision_key": decision.decision_key,
            "inputs": decision.inputs,
            "research": research_payload,
        }
    draft_authority = authority_from_curve(
        draft,
        active_draft,
        radiant_team_side=observation.radiant_team_side,
    )
    if draft_authority is None:
        decision = no_signal_decision(
            observation=observation,
            surface=surface,
            decided_at=current_transport_at,
            reason="draft_authority_unavailable",
            inputs={
                **strict_inputs,
                "draft_curve": {"source_ref": draft.source_ref},
            },
        )
        _persist_decision(store, decision)
        return {
            "status": "no_signal",
            "reason": "draft_authority_unavailable",
            "decision_key": decision.decision_key,
            "inputs": decision.inputs,
            "research": research_payload,
        }

    player_identity = None
    if player_identity_resolver is not None:
        live_team_ids = _canonical_live_team_ids(
            strict.mapping, observation.radiant_team_side
        )
        if live_team_ids is not None:
            player_identity = player_identity_resolver.resolve(
                radiant_team_id=live_team_ids[0],
                dire_team_id=live_team_ids[1],
                radiant_hero_ids=observation.radiant_hero_ids,
                dire_hero_ids=observation.dire_hero_ids,
                as_of=current_transport_at,
            )
        strict_inputs["live_player_identity"] = (
            {"status": "unavailable"}
            if player_identity is None
            else {
                "status": "resolved",
                "source": player_identity.source_name,
                "source_match_id": player_identity.source_match_id,
                "fetched_at": player_identity.fetched_at.isoformat(),
                "evidence_hash": player_identity.evidence_hash,
            }
        )

    rosh_lineup_score = _rosh_score_for_trusted_draft(
        store,
        observation,
        strict_mapping_id=strict.mapping.mapping_id,
        as_of=current_transport_at,
        fetch_started_at=run_at,
        player_identity=player_identity,
        fetch_coordinator=rosh_fetch_coordinator,
    )

    previous_snapshots = None
    previous_observation = None
    previous_transport_key = None
    previous_transport_at = None
    if len(transports) == 2:
        candidate = transports[1]
        candidate_at = candidate.observed_at
        candidate_snapshots = market_state_for_transport(
            store.connection, candidate, match_id, map_number
        )
        if candidate_snapshots:
            candidate_observation, _ = _aligned_transport_observation(
                candidate_snapshots, candidate_at, observations
            )
            if candidate_observation is not None:
                previous_snapshots = candidate_snapshots
                previous_observation = candidate_observation
                previous_transport_key = candidate.observation_key
                previous_transport_at = candidate_at

    team_ids = [
        strict.mapping.canonical_team_one_id,
        strict.mapping.canonical_team_two_id,
    ]
    styles, forms = zip(*[_profiles(store.connection, team_id, as_of) for team_id in team_ids])
    surface_underdog = surface.underdog_side
    underdog_index = 0 if surface_underdog == "team_one" else 1
    favorite_index = 1 - underdog_index
    intelligence_refs = {
        **strict_inputs,
        "team_one_intelligence": _profile_refs(styles[0], forms[0]),
        "team_two_intelligence": _profile_refs(styles[1], forms[1]),
        "draft_curve": {
            "source_ref": draft.source_ref,
            "unavailable_reason": draft.unavailable_reason,
        },
        "draft_authority": {
            **asdict(draft_authority),
        },
    }
    result = strategy.evaluate(
        snapshots=snapshots, observation=observation,
        underdog_style=styles[underdog_index], favorite_style=styles[favorite_index],
        underdog_form=forms[underdog_index], favorite_form=forms[favorite_index],
        draft_curve=draft, decided_at=current_transport_at,
        map_already_attempted=store.has_map_attempt(match_id, map_number),
        previous_snapshots=previous_snapshots,
        previous_observation=previous_observation,
        snapshot_observed_at=current_transport_at,
        previous_snapshot_observed_at=previous_transport_at,
        signal_transport_key=current_transport.observation_key,
        previous_transport_key=previous_transport_key,
        input_refs=intelligence_refs,
        rosh_lineup_score=rosh_lineup_score,
    )
    _persist_decision(
        store,
        result.decision,
        draft_authority=draft_authority,
        vision_observation=observation,
        vision_transport_key=current_transport.observation_key,
    )
    if result.order and store.insert_map_order(
        result.order,
        map_number,
        strict_mapping_id=strict.mapping.mapping_id,
        decision_key=result.decision.decision_key,
        draft_authority=draft_authority,
    ):
        return {
            "status": "shadow_pending", "order_key": result.order.order_key,
            "model_probability": result.decision.model_probability,
            "market_probability": result.decision.market_probability,
            "edge": result.decision.edge,
            "inputs": result.decision.inputs,
            "research": research_payload,
        }
    return {
        "status": "no_signal", "reason": result.decision.reason,
        "edge": result.decision.edge, "quality": result.decision.data_quality,
        "decision_key": result.decision.decision_key,
        "inputs": result.decision.inputs,
        "research": research_payload,
    }


def _run_cli(args: argparse.Namespace) -> int:
    strategy = ComebackShadowStrategy()
    player_identity_resolver = LivePlayerIdentityResolver()
    with (
        LiveBettingStore(args.database) as store,
        RoshFetchCoordinator() as rosh_fetch_coordinator,
    ):
        if not getattr(args, "schema_prepared", False):
            store.init_schema()
        started_at = datetime.now(timezone.utc)
        record_health(
            store.connection,
            "shadow_worker",
            "starting",
            heartbeat_at=started_at,
            details={"source": "worker"},
        )
        while True:
            try:
                result = run_once(
                    store,
                    strategy,
                    args.vision_jsonl,
                    player_identity_resolver=player_identity_resolver,
                    rosh_fetch_coordinator=rosh_fetch_coordinator,
                )
                succeeded_at = datetime.now(timezone.utc)
                record_health(
                    store.connection,
                    "shadow_worker",
                    "healthy",
                    heartbeat_at=succeeded_at,
                    success_at=succeeded_at,
                    details={
                        "source": "worker",
                        "run_status": str(result.get("status", "unknown")),
                    },
                )
                print(json.dumps(result, ensure_ascii=False, default=str))
            except Exception as error:
                failed_at = datetime.now(timezone.utc)
                record_health(
                    store.connection,
                    "shadow_worker",
                    "degraded",
                    heartbeat_at=failed_at,
                    error_at=failed_at,
                    error=type(error).__name__,
                    details={"source": "worker"},
                )
                logger.exception("shadow monitor iteration failed")
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(parser, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--vision-jsonl", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    with database_writer_authority(args.database):
        return _run_cli(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
