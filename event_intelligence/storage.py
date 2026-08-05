"""PostgreSQL storage for strict-event intelligence."""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy.engine import Engine

from database.engine import build_engine
from database.session import DatabaseResult, DatabaseRow, PostgresSession


CURRENT_SCHEMA_VERSION = 10
ALEMBIC_HEAD = "20260805_0026"



@dataclass(frozen=True)
class HistoricalRoshLineupScore:
    score_key: str
    match_id: int
    radiant_hero_ids: tuple[int, ...]
    dire_hero_ids: tuple[int, ...]
    radiant_player_ids: tuple[int, ...]
    dire_player_ids: tuple[int, ...]
    pure_lineup_score: float
    current_player_adjusted_lineup_score: float | None
    effective_lineup_score: float
    scoring_mode: str
    player_coverage_count: int
    source_name: str
    source_week: int
    source_as_of: datetime
    player_stats_as_of: datetime | None
    formula_version: str
    evidence: Mapping[str, Any]
    evidence_hash: str
    backtest_eligible: bool
    created_at: datetime


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _aware_datetime(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _identity_ids(value: Sequence[Any], name: str) -> tuple[int, ...]:
    if len(value) != 5 or any(type(item) is not int or item <= 0 for item in value):
        raise ValueError(f"{name} must contain five positive integer IDs")
    result = tuple(int(item) for item in value)
    if len(set(result)) != 5:
        raise ValueError(f"{name} must contain five unique IDs")
    return result


_ROSH_MINUTE_NUMERIC_FIELDS = (
    "advantage_percent",
    "radiant_advantage",
    "dire_advantage",
    "match_percentage",
    "win_rate_graph",
    "hero_adjustment",
    "hero_base_adjustment",
    "hero_tempo_adjustment",
    "synergy_adjustment",
    "player_adjustment",
)


def _valid_rosh_minute_table(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    previous_minute = 19
    for bucket in value:
        if not isinstance(bucket, Mapping):
            return False
        minute = bucket.get("minute")
        time_start = bucket.get("time_start")
        time_end = bucket.get("time_end")
        if (
            type(minute) is not int
            or type(time_start) is not int
            or type(time_end) is not int
            or not 20 <= time_start <= minute <= time_end <= 60
            or minute <= previous_minute
            or bucket.get("advantage_side") not in {"radiant", "dire", "even"}
        ):
            return False
        previous_minute = minute
        if any(
            isinstance(bucket.get(field), bool)
            or not isinstance(bucket.get(field), (int, float))
            or not math.isfinite(float(bucket[field]))
            for field in _ROSH_MINUTE_NUMERIC_FIELDS
        ):
            return False
    return True


def _valid_historical_rosh_evidence(
    evidence: Mapping[str, Any],
    *,
    match_id: int,
    pure_score: float,
    adjusted_score: float | None,
    effective_score: float,
    scoring_mode: str,
    player_coverage_count: int,
    source_name: str,
    source_week: int,
    source_as_of: str,
    player_stats_as_of: str | None,
    formula_version: str,
) -> bool:
    metadata_keys = {
        "historical_match_id",
        "source",
        "formula_version",
        "source_week",
        "source_as_of",
        "player_stats_as_of",
        "retrospective",
        "current_player_adjustment_only",
        "backtest_eligible",
    }
    if not metadata_keys.issubset(evidence):
        return False
    expected_metadata = {
        "historical_match_id": match_id,
        "source": source_name,
        "formula_version": formula_version,
        "source_week": source_week,
        "source_as_of": source_as_of,
        "player_stats_as_of": player_stats_as_of,
        "retrospective": True,
        "current_player_adjustment_only": True,
        "backtest_eligible": False,
    }
    if (
        any(evidence.get(key) != value for key, value in expected_metadata.items())
        or type(evidence.get("historical_match_id")) is not int
        or type(evidence.get("source_week")) is not int
        or evidence.get("retrospective") is not True
        or evidence.get("current_player_adjustment_only") is not True
        or evidence.get("backtest_eligible") is not False
    ):
        return False
    score = evidence.get("score")
    expected_score = {
        "pure_lineup_score": pure_score,
        "current_player_adjusted_lineup_score": adjusted_score,
        "effective_lineup_score": effective_score,
        "scoring_mode": scoring_mode,
        "player_coverage_count": player_coverage_count,
    }
    if (
        not isinstance(score, Mapping)
        or not set(expected_score).issubset(score)
        or any(score.get(key) != value for key, value in expected_score.items())
        or any(
            isinstance(score.get(key), bool)
            or not isinstance(score.get(key), (int, float))
            or not math.isfinite(float(score[key]))
            for key in (
                "pure_lineup_score",
                "effective_lineup_score",
                "player_coverage_count",
            )
        )
        or type(score.get("player_coverage_count")) is not int
        or (
            adjusted_score is not None
            and (
                isinstance(score.get("current_player_adjusted_lineup_score"), bool)
                or not isinstance(
                    score.get("current_player_adjusted_lineup_score"), (int, float)
                )
                or not math.isfinite(
                    float(score["current_player_adjusted_lineup_score"])
                )
            )
        )
    ):
        return False
    pure_table = evidence.get("pure_minute_table")
    if (
        not _valid_rosh_minute_table(pure_table)
        or float(pure_table[-1]["win_rate_graph"]) != pure_score
    ):
        return False
    if scoring_mode == "current_player_adjusted":
        adjusted_table = evidence.get("minute_table")
        return bool(
            _valid_rosh_minute_table(adjusted_table)
            and adjusted_score is not None
            and float(adjusted_table[-1]["win_rate_graph"]) == adjusted_score
            and [row["minute"] for row in adjusted_table]
            == [row["minute"] for row in pure_table]
        )
    return "minute_table" not in evidence


def _historical_rosh_score_from_row(
    row: DatabaseRow | Mapping[str, Any],
) -> HistoricalRoshLineupScore | None:
    try:
        payload = dict(row)
        radiant_heroes = _identity_ids(
            json.loads(str(payload["radiant_hero_ids_json"])), "radiant heroes"
        )
        dire_heroes = _identity_ids(
            json.loads(str(payload["dire_hero_ids_json"])), "dire heroes"
        )
        radiant_players = _identity_ids(
            json.loads(str(payload["radiant_player_ids_json"])), "radiant players"
        )
        dire_players = _identity_ids(
            json.loads(str(payload["dire_player_ids_json"])), "dire players"
        )
        if len(set((*radiant_heroes, *dire_heroes))) != 10:
            return None
        if len(set((*radiant_players, *dire_players))) != 10:
            return None
        evidence = json.loads(str(payload["evidence_json"]))
        if not isinstance(evidence, dict):
            return None
        evidence_hash = str(payload["evidence_hash"])
        if _sha256_json(evidence) != evidence_hash:
            return None
        source_as_of = _aware_datetime(payload["source_as_of"])
        created_at = _aware_datetime(payload["created_at"])
        raw_player_stats_as_of = payload["player_stats_as_of"]
        player_stats_as_of = (
            None
            if raw_player_stats_as_of is None
            else _aware_datetime(raw_player_stats_as_of)
        )
        if source_as_of is None or created_at is None:
            return None
        if raw_player_stats_as_of is not None and player_stats_as_of is None:
            return None
        pure = float(payload["pure_lineup_score"])
        raw_adjusted = payload["current_player_adjusted_lineup_score"]
        adjusted = None if raw_adjusted is None else float(raw_adjusted)
        effective = float(payload["effective_lineup_score"])
        score_values = (
            (pure, effective) if adjusted is None else (pure, effective, adjusted)
        )
        if any(not math.isfinite(value) for value in score_values):
            return None
        mode = str(payload["scoring_mode"])
        coverage = int(payload["player_coverage_count"])
        invariant = (
            mode == "current_player_adjusted"
            and coverage == 10
            and adjusted is not None
            and effective == adjusted
            and player_stats_as_of is not None
        ) or (
            mode == "pure"
            and 0 <= coverage < 10
            and adjusted is None
            and effective == pure
        )
        source_name = str(payload["source_name"])
        source_week = int(payload["source_week"])
        formula_version = str(payload["formula_version"])
        if (
            not invariant
            or int(payload["backtest_eligible"]) != 0
            or source_name != "stratz"
            or source_week <= 0
            or not formula_version.strip()
            or source_as_of > created_at
            or (player_stats_as_of is not None and player_stats_as_of > created_at)
            or not _valid_historical_rosh_evidence(
                evidence,
                match_id=int(payload["match_id"]),
                pure_score=pure,
                adjusted_score=adjusted,
                effective_score=effective,
                scoring_mode=mode,
                player_coverage_count=coverage,
                source_name=source_name,
                source_week=source_week,
                source_as_of=source_as_of.isoformat(),
                player_stats_as_of=(
                    None
                    if player_stats_as_of is None
                    else player_stats_as_of.isoformat()
                ),
                formula_version=formula_version,
            )
        ):
            return None
        return HistoricalRoshLineupScore(
            score_key=str(payload["score_key"]),
            match_id=int(payload["match_id"]),
            radiant_hero_ids=radiant_heroes,
            dire_hero_ids=dire_heroes,
            radiant_player_ids=radiant_players,
            dire_player_ids=dire_players,
            pure_lineup_score=pure,
            current_player_adjusted_lineup_score=adjusted,
            effective_lineup_score=effective,
            scoring_mode=mode,
            player_coverage_count=coverage,
            source_name=source_name,
            source_week=source_week,
            source_as_of=source_as_of,
            player_stats_as_of=player_stats_as_of,
            formula_version=formula_version,
            evidence=evidence,
            evidence_hash=evidence_hash,
            backtest_eligible=False,
            created_at=created_at,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def query_historical_rosh_lineup_score_for_match(
    connection: PostgresSession,
    *,
    match_id: int,
    formula_version: str,
) -> HistoricalRoshLineupScore | None:
    relation = connection.execute(
        "SELECT to_regclass(?)",
        ("public.historical_rosh_lineup_scores",),
    ).fetchone()
    if relation is None or relation[0] is None:
        return None
    rows = connection.execute(
        """SELECT * FROM historical_rosh_lineup_scores
            WHERE match_id=? AND formula_version=?
            ORDER BY created_at DESC, score_key DESC""",
        (match_id, formula_version),
    ).fetchall()
    for row in rows:
        parsed = _historical_rosh_score_from_row(row)
        if parsed is not None:
            return parsed
    return None


def query_historical_rosh_lineup_score(
    connection: PostgresSession,
    *,
    match_id: int,
    formula_version: str,
    radiant_hero_ids: Sequence[int],
    dire_hero_ids: Sequence[int],
    radiant_player_ids: Sequence[int],
    dire_player_ids: Sequence[int],
) -> HistoricalRoshLineupScore | None:
    try:
        expected = (
            _identity_ids(radiant_hero_ids, "radiant heroes"),
            _identity_ids(dire_hero_ids, "dire heroes"),
            _identity_ids(radiant_player_ids, "radiant players"),
            _identity_ids(dire_player_ids, "dire players"),
        )
    except ValueError:
        return None
    relation = connection.execute(
        "SELECT to_regclass(?)",
        ("public.historical_rosh_lineup_scores",),
    ).fetchone()
    if relation is None or relation[0] is None:
        return None
    rows = connection.execute(
        """SELECT * FROM historical_rosh_lineup_scores
            WHERE match_id=? AND formula_version=?
            ORDER BY created_at DESC, score_key DESC""",
        (match_id, formula_version),
    ).fetchall()
    for row in rows:
        parsed = _historical_rosh_score_from_row(row)
        if parsed is None:
            continue
        actual = (
            parsed.radiant_hero_ids,
            parsed.dire_hero_ids,
            parsed.radiant_player_ids,
            parsed.dire_player_ids,
        )
        if actual == expected:
            return parsed
    return None


class IntelligenceStorage:
    """PostgreSQL storage for event-intelligence runtime state."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        if database_url is not None and engine is not None:
            raise ValueError("database_url and engine are mutually exclusive")
        self.engine = engine or build_engine(database_url)
        self._owns_engine = engine is None
        self.connection = PostgresSession(self.engine)
        self._transaction_depth = 0

    def __enter__(self) -> "IntelligenceStorage":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()
        if self._owns_engine:
            self.engine.dispose()

    def init_schema(
        self,
        *,
        seed_events: bool = True,
        external_transaction: bool = False,
    ) -> None:
        revision = self.connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        if revision is None or str(revision[0]) != ALEMBIC_HEAD:
            actual = None if revision is None else str(revision[0])
            raise RuntimeError(
                f"PostgreSQL schema revision {actual!r} is not {ALEMBIC_HEAD}"
            )
        transaction = (
            self._external_transaction()
            if external_transaction
            else self.transaction()
        )
        with transaction:
            if seed_events:
                from .registry import EventRegistry

                registry = EventRegistry(self)
                registry.seed_approved_events()
                self._verify_seeded_events(registry)

    @contextmanager
    def _external_transaction(self) -> Iterator[None]:
        if not self.connection.in_transaction:
            raise RuntimeError("external transaction is not active")
        self._transaction_depth += 1
        try:
            yield
        finally:
            self._transaction_depth -= 1

    @staticmethod
    def _verify_seeded_events(registry: object) -> None:
        from .registry import (
            APPROVED_EVENT_SEEDS,
            AUDITED_AT,
            EXCLUDED_CATEGORIES,
            SCOPE_POLICY_VERSION,
        )

        events = {event.event_id: event for event in registry.formal_events()}
        expected_ids = {str(seed["event_id"]) for seed in APPROVED_EVENT_SEEDS}
        if set(events) != expected_ids:
            raise RuntimeError(
                "approved event seed set conflicts with the audited registry"
            )
        for seed in APPROVED_EVENT_SEEDS:
            event = events[str(seed["event_id"])]
            expected_map_count = seed["expected_map_count"]
            expected = (
                str(seed["canonical_name"]),
                str(seed["tier"]),
                int(seed["prize_pool_usd"]),
                datetime.fromisoformat(str(seed["main_event_start_at"])),
                datetime.fromisoformat(str(seed["main_event_end_at"])),
                int(seed["opendota_league_id"]),
                (),
                tuple(seed["official_evidence_urls"]),
                "manually_audited",
                SCOPE_POLICY_VERSION,
                "formal_main_event",
                "approved",
                "manual_event_audit",
                datetime.fromisoformat(AUDITED_AT),
                (int(expected_map_count) if expected_map_count is not None else None),
                tuple(seed["included_stages"]),
                tuple(EXCLUDED_CATEGORIES),
                bool(seed["include_internal_lcq"]),
            )
            actual = (
                event.canonical_name,
                event.tier,
                event.prize_pool_usd,
                event.main_event_start_at,
                event.main_event_end_at,
                event.opendota_league_id,
                event.secondary_provider_ids,
                event.official_evidence_urls,
                event.evidence_status.value,
                event.scope_policy_version,
                event.scope.value,
                event.approval_status.value,
                event.approved_by,
                event.approved_at,
                event.expected_map_count,
                tuple(stage.value for stage in event.included_stages),
                event.excluded_categories,
                event.include_internal_lcq,
            )
            if actual != expected:
                raise RuntimeError(
                    f"approved event seed policy drift for {event.event_id}"
                )

    def insert_historical_rosh_lineup_score(
        self,
        *,
        match_id: int,
        radiant_hero_ids: Sequence[int],
        dire_hero_ids: Sequence[int],
        radiant_player_ids: Sequence[int],
        dire_player_ids: Sequence[int],
        pure_lineup_score: float,
        current_player_adjusted_lineup_score: float | None,
        effective_lineup_score: float,
        scoring_mode: str,
        player_coverage_count: int,
        source_week: int,
        source_as_of: datetime,
        player_stats_as_of: datetime | None,
        formula_version: str,
        evidence: Mapping[str, Any],
        created_at: datetime,
        evidence_hash: str | None = None,
        source_name: str = "stratz",
    ) -> HistoricalRoshLineupScore:
        if type(match_id) is not int or match_id <= 0:
            raise ValueError("match_id must be a positive integer")
        radiant_heroes = _identity_ids(radiant_hero_ids, "radiant heroes")
        dire_heroes = _identity_ids(dire_hero_ids, "dire heroes")
        radiant_players = _identity_ids(radiant_player_ids, "radiant players")
        dire_players = _identity_ids(dire_player_ids, "dire players")
        if len(set((*radiant_heroes, *dire_heroes))) != 10:
            raise ValueError("historical Rosh hero IDs must be unique")
        if len(set((*radiant_players, *dire_players))) != 10:
            raise ValueError("historical Rosh player IDs must be unique")

        try:
            pure = float(pure_lineup_score)
            adjusted = (
                None
                if current_player_adjusted_lineup_score is None
                else float(current_player_adjusted_lineup_score)
            )
            effective = float(effective_lineup_score)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("historical Rosh scores must be finite numbers") from error
        score_values = (
            (pure, effective) if adjusted is None else (pure, effective, adjusted)
        )
        if any(not math.isfinite(value) for value in score_values):
            raise ValueError("historical Rosh scores must be finite numbers")
        if type(player_coverage_count) is not int:
            raise ValueError("player_coverage_count must be an integer")
        invariant = (
            scoring_mode == "current_player_adjusted"
            and player_coverage_count == 10
            and adjusted is not None
            and effective == adjusted
            and player_stats_as_of is not None
        ) or (
            scoring_mode == "pure"
            and 0 <= player_coverage_count < 10
            and adjusted is None
            and effective == pure
        )
        if not invariant:
            raise ValueError("historical Rosh scoring mode invariant failed")
        if source_name != "stratz":
            raise ValueError("historical Rosh source_name must be stratz")
        if type(source_week) is not int or source_week <= 0:
            raise ValueError("source_week must be a positive integer")
        if not isinstance(formula_version, str) or not formula_version.strip():
            raise ValueError("formula_version must be non-empty")

        source_at = _aware_datetime(source_as_of)
        player_stats_at = (
            None
            if player_stats_as_of is None
            else _aware_datetime(player_stats_as_of)
        )
        created = _aware_datetime(created_at)
        if source_at is None or created is None:
            raise ValueError("source_as_of and created_at must include timezones")
        if player_stats_as_of is not None and player_stats_at is None:
            raise ValueError("player_stats_as_of must include a timezone")
        if source_at > created or (
            player_stats_at is not None and player_stats_at > created
        ):
            raise ValueError("historical Rosh evidence cannot be newer than its row")

        evidence_payload = dict(evidence)
        if not _valid_historical_rosh_evidence(
            evidence_payload,
            match_id=match_id,
            pure_score=pure,
            adjusted_score=adjusted,
            effective_score=effective,
            scoring_mode=scoring_mode,
            player_coverage_count=player_coverage_count,
            source_name=source_name,
            source_week=source_week,
            source_as_of=source_at.isoformat(),
            player_stats_as_of=(
                None if player_stats_at is None else player_stats_at.isoformat()
            ),
            formula_version=formula_version,
        ):
            raise ValueError("historical Rosh evidence does not match score columns")
        try:
            evidence_json = _canonical_json(evidence_payload)
        except (TypeError, ValueError) as error:
            raise ValueError("evidence must be finite JSON") from error
        calculated_evidence_hash = hashlib.sha256(
            evidence_json.encode("utf-8")
        ).hexdigest()
        if evidence_hash is not None and evidence_hash != calculated_evidence_hash:
            raise ValueError("historical Rosh evidence hash mismatch")
        identity = {
            "match_id": match_id,
            "radiant_hero_ids": radiant_heroes,
            "dire_hero_ids": dire_heroes,
            "radiant_player_ids": radiant_players,
            "dire_player_ids": dire_players,
            "pure_lineup_score": pure,
            "current_player_adjusted_lineup_score": adjusted,
            "effective_lineup_score": effective,
            "scoring_mode": scoring_mode,
            "player_coverage_count": player_coverage_count,
            "source_name": source_name,
            "source_week": source_week,
            "source_as_of": source_at.isoformat(),
            "player_stats_as_of": (
                None if player_stats_at is None else player_stats_at.isoformat()
            ),
            "formula_version": formula_version,
            "evidence_hash": calculated_evidence_hash,
        }
        score_key = _sha256_json(identity)
        self.execute(
            """INSERT INTO historical_rosh_lineup_scores
               (score_key, match_id, radiant_hero_ids_json,
                dire_hero_ids_json, radiant_player_ids_json,
                dire_player_ids_json, pure_lineup_score,
                current_player_adjusted_lineup_score, effective_lineup_score,
                scoring_mode, player_coverage_count, source_name, source_week,
                source_as_of, player_stats_as_of, formula_version,
                evidence_json, evidence_hash, backtest_eligible, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT (score_key) DO NOTHING""",
            (
                score_key,
                match_id,
                _canonical_json(radiant_heroes),
                _canonical_json(dire_heroes),
                _canonical_json(radiant_players),
                _canonical_json(dire_players),
                pure,
                adjusted,
                effective,
                scoring_mode,
                player_coverage_count,
                source_name,
                source_week,
                source_at.isoformat(),
                None if player_stats_at is None else player_stats_at.isoformat(),
                formula_version,
                evidence_json,
                calculated_evidence_hash,
                created.isoformat(),
            ),
        )
        row = self.connection.execute(
            "SELECT * FROM historical_rosh_lineup_scores WHERE score_key=?",
            (score_key,),
        ).fetchone()
        parsed = None if row is None else _historical_rosh_score_from_row(row)
        if parsed is None:
            raise RuntimeError("stored historical Rosh score failed validation")
        return parsed

    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> DatabaseResult:
        try:
            cursor = self.connection.execute(sql, parameters)
        except Exception:
            if self._transaction_depth == 0:
                self.connection.rollback()
            raise
        if self._transaction_depth == 0:
            self.connection.commit()
        return cursor

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.connection.transaction():
            self._transaction_depth += 1
            try:
                yield
            finally:
                self._transaction_depth -= 1
