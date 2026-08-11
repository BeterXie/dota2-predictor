"""PostgreSQL persistence for RayBet collection and Vision evidence."""

from __future__ import annotations

import hashlib
import gzip
import json
import math
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from contracts.live_observation import MAP_START_EVIDENCE_WINDOW_SECONDS
from database.engine import build_engine
from database.session import DatabaseResult, DatabaseRow, PostgresSession

from event_intelligence.raw_archive import (
    ArtifactReceipt,
    RawArchive,
    canonical_json_value_bytes,
    schema_fingerprint,
)

from .models import (
    OddsSnapshot,
    ProviderMatch,
)
from .live_match_state import append_live_game_snapshot
from .odds_response_authority import (
    canonical_state_outcomes,
    response_artifact_identity as canonical_response_artifact_identity,
    response_state_identity as canonical_response_state_identity,
    snapshot_derived_payload,
)
from .raybet_state import explicit_raybet_map_times
from .sanitize import (
    PUBLIC_STREAM_EVIDENCE_KEY,
    public_stream_evidence,
    sanitize_raybet_payload,
)
from .strict_eligibility import (
    RAYBET_MATCH_NON_HEAD_TO_HEAD,
    classify_raybet_match_format,
    strict_raybet_head_to_head_teams,
)
from .vision_frame_registry import (
    VisionFrameReceipt,
    register_vision_frame_artifact,
)


CURRENT_SCHEMA_VERSION = 12
ALEMBIC_HEAD = "20260807_0035"
VISION_DRAFT_CONFLICT_REASON = "confirmed_draft_conflict"
_DIRECT_RESPONSE_ENDPOINTS = {
    "live_match_list": "https://raybet.local/v2/match/live",
    "completed_match_list": "https://raybet.local/v2/match/completed",
    "live_odds": "https://raybet.local/v2/odds",
    "completed_odds": "https://raybet.local/v2/odds",
    "final_odds": "https://raybet.local/v2/odds",
}


def _verified_vision_map_start(
    connection: PostgresSession,
    *,
    raybet_match_id: str,
    map_number: int,
    captured_at: datetime,
) -> bool:
    if map_number == 1:
        return True
    row = connection.execute(
        """SELECT 1
             FROM vision_observations AS observation
             JOIN active_vision_frame_artifacts AS frame
               ON frame.frame_ref=observation.source_frame_ref
              AND frame.content_sha256=observation.source_frame_sha256
              AND frame.byte_length=observation.source_frame_bytes
            WHERE observation.raybet_match_id=?
              AND observation.map_number=?
              AND observation.game_clock_seconds BETWEEN ? AND ?
              AND observation.screen_state='game'
              AND observation.clock_confidence>=0.9
              AND observation.source_frame_ref=
                  'vision-frame:sha256:' || frame.content_sha256
              AND live_text_timestamp_utc(observation.captured_at)<=
                  CAST(? AS timestamptz)
              AND NOT EXISTS (
                  SELECT 1
                    FROM vision_observation_invalidations AS invalidation
                   WHERE invalidation.raybet_match_id=
                         observation.raybet_match_id
                     AND invalidation.captured_at=observation.captured_at
                     AND invalidation.source_frame_ref=observation.source_frame_ref
              )
            LIMIT 1""",
        (
            raybet_match_id,
            map_number,
            -MAP_START_EVIDENCE_WINDOW_SECONDS,
            MAP_START_EVIDENCE_WINDOW_SECONDS,
            captured_at.isoformat(),
        ),
    ).fetchone()
    if row is not None:
        return True
    provider = connection.execute(
        "SELECT raw_json, best_of FROM raybet_matches WHERE raybet_match_id=?",
        (raybet_match_id,),
    ).fetchone()
    if provider is None or type(provider[1]) is not int:
        return False
    try:
        payload = json.loads(str(provider[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    provider_started_at = explicit_raybet_map_times(
        payload,
        int(provider[1]),
    ).get(map_number)
    return provider_started_at is not None and captured_at >= provider_started_at


def _load_odds_raw_artifact(
    connection: PostgresSession,
    raw_archive_root: Path,
    artifact_hash: str,
) -> Any:
    row = connection.execute(
        """SELECT storage_path, uncompressed_bytes
             FROM odds_raw_artifacts WHERE artifact_hash=?""",
        (artifact_hash,),
    ).fetchone()
    if row is None:
        raise RuntimeError("response raw artifact metadata is missing")
    relative_path = Path(str(row["storage_path"]))
    if relative_path.is_absolute():
        raise RuntimeError("response raw artifact path must be relative")
    path = (raw_archive_root / relative_path).resolve()
    try:
        path.relative_to(raw_archive_root)
    except ValueError as error:
        raise RuntimeError("response raw artifact path escapes archive root") from error
    if path.name != f"{artifact_hash}.json.gz":
        raise RuntimeError("response raw artifact path is invalid")
    try:
        RawArchive._verify(path, artifact_hash)
        payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("response raw artifact is corrupt") from error
    encoded = canonical_json_value_bytes(payload)
    if len(encoded) != int(row["uncompressed_bytes"]):
        raise RuntimeError("response raw artifact byte count mismatch")
    return payload


def read_browser_event_payload(
    connection: PostgresSession,
    raw_archive_root: str | Path,
    event_id: str,
) -> dict[str, Any]:
    """Load one browser event through external-v2 or legacy-inline storage."""
    row = connection.execute(
        """SELECT payload_storage, payload_artifact_hash, payload_json,
                  payload_hash, payload_bytes
             FROM browser_events WHERE event_id=?""",
        (event_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("browser event is missing")
    if str(row["payload_storage"]) == "external":
        if row["payload_artifact_hash"] is None:
            raise RuntimeError("browser event artifact reference is missing")
        payload = _load_odds_raw_artifact(
            connection,
            Path(raw_archive_root).resolve(),
            str(row["payload_artifact_hash"]),
        )
    else:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("legacy browser payload is invalid") from error
    if not isinstance(payload, dict):
        raise RuntimeError("browser payload is not an object")
    from .browser_contract import canonical_json, payload_sha256

    if payload_sha256(payload) != str(row["payload_hash"]):
        raise RuntimeError("browser payload hash mismatch")
    if len(canonical_json(payload)) != int(row["payload_bytes"]):
        raise RuntimeError("browser payload byte count mismatch")
    return payload


def _valid_confirmed_vision_payload(
    radiant_hero_ids: object,
    dire_hero_ids: object,
    source_frame_ref: object,
) -> bool:
    """Validate the immutable inputs required for a confirmed draft frame."""
    if not isinstance(radiant_hero_ids, (list, tuple)):
        return False
    if not isinstance(dire_hero_ids, (list, tuple)):
        return False
    if not isinstance(source_frame_ref, str) or not source_frame_ref.strip():
        return False
    heroes = tuple(radiant_hero_ids) + tuple(dire_hero_ids)
    return (
        len(radiant_hero_ids) == 5
        and len(dire_hero_ids) == 5
        and all(type(hero_id) is int and hero_id > 0 for hero_id in heroes)
        and len(set(heroes)) == 10
    )


class LiveBettingStore:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        raw_archive_root: str | Path | None = None,
    ) -> None:
        if database_url is not None and engine is not None:
            raise ValueError("database_url and engine are mutually exclusive")
        self.engine = engine or build_engine(database_url)
        self._owns_engine = engine is None
        self.connection = PostgresSession(self.engine)
        if raw_archive_root is None:
            raw_archive_root = Path("data") / "live_betting" / "raw-v2"
        self.raw_archive_root = Path(raw_archive_root).resolve()
        self.raw_archive = RawArchive(self.raw_archive_root)
        self._transaction_depth = 0

    def close(self) -> None:
        self.connection.close()
        if self._owns_engine:
            self.engine.dispose()

    def __enter__(self) -> "LiveBettingStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def init_schema(self, *, external_transaction: bool = False) -> None:
        if external_transaction and not self.connection.in_transaction:
            raise RuntimeError("external transaction is not active")
        revision = self.connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        if revision is None or str(revision[0]) != ALEMBIC_HEAD:
            actual = None if revision is None else str(revision[0])
            raise RuntimeError(
                f"PostgreSQL schema revision {actual!r} is not {ALEMBIC_HEAD}"
            )
        live_version = self.connection.execute(
            "SELECT MAX(version) FROM live_schema_version"
        ).fetchone()
        actual_live = None if live_version is None else live_version[0]
        if actual_live is None or int(actual_live) != CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"live schema version {actual_live!r} is not "
                f"{CURRENT_SCHEMA_VERSION}"
            )

    @staticmethod
    def json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

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
        """Commit a unit of work atomically while supporting nested callers."""
        with self.connection.transaction():
            self._transaction_depth += 1
            try:
                yield
            finally:
                self._transaction_depth -= 1

    @contextmanager
    def savepoint(self, name: str) -> Iterator[None]:
        """Create a named rollback boundary inside an active transaction."""
        if self._transaction_depth == 0:
            raise RuntimeError("savepoint requires an active transaction")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("invalid savepoint name")
        with self.connection.transaction():
            yield

    @staticmethod
    def _event_value(event: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
        if isinstance(event, Mapping):
            return event.get(name, default)
        return getattr(event, name, default)

    @staticmethod
    def _iso(value: datetime | str) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc)
            return value.isoformat()
        return str(value)

    def _draft_conflict_state(
        self, raybet_match_id: str, map_number: int,
    ) -> tuple[bool, str | None]:
        """Return whether a map has a draft conflict and its earliest cutoff.

        Conflict rows can arrive out of capture order.  Causal readers derive
        the cutoff from rows that conflict with the rebuilt canonical anchor
        and fail closed on missing schema or malformed timestamps.  If an
        operator froze a map without an intrinsic draft mismatch, every audit
        row remains effective.
        """
        try:
            anchor = self.connection.execute(
                """SELECT draft_hash, radiant_team_side, status, conflict_at
                     FROM vision_draft_anchors
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            ).fetchone()
            rows = self.connection.execute(
                """SELECT captured_at, observed_draft_hash,
                          observed_radiant_team_side
                     FROM vision_draft_conflicts
                    WHERE raybet_match_id=? AND map_number=?
                    ORDER BY conflict_id""",
                (raybet_match_id, map_number),
            ).fetchall()
        except SQLAlchemyError:
            return True, None
        if anchor is None:
            return (True, None) if rows else (False, None)
        status = str(anchor["status"])
        if status not in {"anchored", "conflict"}:
            return True, None
        parsed_rows: list[tuple[datetime, str, bool]] = []
        for row in rows:
            value = str(row["captured_at"])
            try:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return True, None
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                return True, None
            intrinsic = str(row["observed_draft_hash"]) != str(
                anchor["draft_hash"]
            ) or (
                anchor["radiant_team_side"] in {"team_one", "team_two"}
                and row["observed_radiant_team_side"]
                in {"team_one", "team_two"}
                and row["observed_radiant_team_side"]
                != anchor["radiant_team_side"]
            )
            normalized = timestamp.astimezone(timezone.utc)
            parsed_rows.append((normalized, normalized.isoformat(), intrinsic))
        if status == "anchored" and not parsed_rows:
            return False, None

        parsed: list[tuple[datetime, str]] = [
            (timestamp, value)
            for timestamp, value, intrinsic in parsed_rows
            if intrinsic
        ]
        if not parsed:
            parsed = [
                (timestamp, value) for timestamp, value, _intrinsic in parsed_rows
            ]
        if status == "conflict" and anchor["conflict_at"] is not None:
            value = str(anchor["conflict_at"])
            try:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return True, None
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                return True, None
            normalized = timestamp.astimezone(timezone.utc)
            parsed.append((normalized, normalized.isoformat()))
        if not parsed:
            return True, None
        return True, min(parsed)[1]

    @staticmethod
    def _draft_event_key(captured_at: object, source_frame_ref: object) -> tuple[datetime, str] | None:
        """Return the deterministic event-time ordering key for one frame."""
        try:
            parsed = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc), str(source_frame_ref)

    def _rebuild_vision_draft_anchor(
        self, observation: Any, anchor: DatabaseRow,
    ) -> bool:
        """Rebuild a draft anchor in deterministic browser event-time order.

        Browser capture time is the event order.  Rebuilding for every confirmed
        frame is necessary because a frame can arrive before an already-recorded
        conflict or team-side observation while still being later than the draft
        anchor.  All candidate facts remain append-only in the observation and
        conflict tables.
        """
        match_id = str(observation.raybet_match_id)
        map_number = int(observation.map_number)
        candidates: dict[tuple[str, str], dict[str, Any]] = {}

        def add_candidate(
            captured_at: object,
            source_frame_ref: object,
            draft_hash: object,
            radiant_hero_ids: object,
            dire_hero_ids: object,
            radiant_team_side: object,
        ) -> None:
            key = (str(captured_at), str(source_frame_ref))
            event_key = self._draft_event_key(*key)
            if event_key is None or not str(source_frame_ref).strip():
                return
            try:
                radiant = json.loads(str(radiant_hero_ids))
                dire = json.loads(str(dire_hero_ids))
            except (TypeError, ValueError, json.JSONDecodeError):
                return
            if not _valid_confirmed_vision_payload(
                radiant, dire, source_frame_ref
            ):
                return
            payload = self.json({"radiant": radiant, "dire": dire})
            calculated_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if str(draft_hash) != calculated_hash:
                return
            side = radiant_team_side if radiant_team_side in {"team_one", "team_two"} else None
            candidates[key] = {
                "captured_at": event_key[0],
                "source_frame_ref": str(source_frame_ref),
                "draft_hash": calculated_hash,
                "radiant_json": self.json(radiant),
                "dire_json": self.json(dire),
                "radiant_team_side": side,
            }

        add_candidate(
            anchor["anchored_at"],
            anchor["source_frame_ref"],
            anchor["draft_hash"],
            anchor["radiant_hero_ids"],
            anchor["dire_hero_ids"],
            anchor["radiant_team_side"],
        )
        conflict_rows = self.connection.execute(
            """SELECT captured_at, source_frame_ref, observed_draft_hash,
                      radiant_hero_ids, dire_hero_ids,
                      observed_radiant_team_side
                 FROM vision_draft_conflicts
                WHERE raybet_match_id=? AND map_number=?""",
            (match_id, map_number),
        ).fetchall()
        conflict_keys = {
            (str(row["captured_at"]), str(row["source_frame_ref"]))
            for row in conflict_rows
        }
        for row in conflict_rows:
            add_candidate(
                row["captured_at"],
                row["source_frame_ref"],
                row["observed_draft_hash"],
                row["radiant_hero_ids"],
                row["dire_hero_ids"],
                row["observed_radiant_team_side"],
            )
        for row in self.connection.execute(
            """SELECT captured_at, source_frame_ref, radiant_hero_ids,
                      dire_hero_ids, radiant_team_side
                 FROM vision_observations
                WHERE raybet_match_id=? AND map_number=? AND confirmed=1""",
            (match_id, map_number),
        ).fetchall():
            key = (str(row["captured_at"]), str(row["source_frame_ref"]))
            if key not in conflict_keys:
                try:
                    payload = self.json({
                        "radiant": json.loads(str(row["radiant_hero_ids"])),
                        "dire": json.loads(str(row["dire_hero_ids"])),
                    })
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                add_candidate(
                    row["captured_at"],
                    row["source_frame_ref"],
                    hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                    row["radiant_hero_ids"],
                    row["dire_hero_ids"],
                    row["radiant_team_side"],
                )
        add_candidate(
            self._iso(observation.captured_at),
            observation.source_frame_ref,
            hashlib.sha256(
                self.json({
                    "radiant": list(observation.radiant_hero_ids),
                    "dire": list(observation.dire_hero_ids),
                }).encode("utf-8")
            ).hexdigest(),
            self.json(list(observation.radiant_hero_ids)),
            self.json(list(observation.dire_hero_ids)),
            observation.radiant_team_side,
        )

        invalidated = {
            (str(row["captured_at"]), str(row["source_frame_ref"]))
            for row in self.connection.execute(
                """SELECT invalidation.captured_at,
                          invalidation.source_frame_ref
                     FROM vision_observation_invalidations AS invalidation
                     JOIN vision_observations AS observation
                       ON observation.raybet_match_id=
                          invalidation.raybet_match_id
                      AND observation.captured_at=invalidation.captured_at
                      AND observation.source_frame_ref=
                          invalidation.source_frame_ref
                    WHERE invalidation.raybet_match_id=?
                      AND observation.map_number=?""",
                (match_id, map_number),
            ).fetchall()
        }
        candidates = {
            key: value for key, value in candidates.items() if key not in invalidated
        }
        ordered = sorted(
            candidates.values(),
            key=lambda value: (value["captured_at"], value["source_frame_ref"]),
        )
        if not ordered:
            return False
        canonical = ordered[0]
        canonical_hash = canonical["draft_hash"]
        canonical_side: str | None = None
        side_source: dict[str, Any] | None = None
        conflict_candidates: list[dict[str, Any]] = []
        for candidate in ordered:
            if candidate["draft_hash"] != canonical_hash:
                conflict_candidates.append(candidate)
                continue
            side = candidate["radiant_team_side"]
            if side is None:
                continue
            if canonical_side is None:
                canonical_side = side
                side_source = candidate
            elif side != canonical_side:
                conflict_candidates.append(candidate)

        existing_conflict = bool(anchor["status"] == "conflict")
        conflict_cutoff = min(
            (candidate["captured_at"] for candidate in conflict_candidates),
            default=None,
        )
        if existing_conflict and conflict_cutoff is None:
            old_cutoff = self._draft_event_key(
                anchor["conflict_at"], anchor["source_frame_ref"]
            )
            if old_cutoff is not None:
                conflict_cutoff = old_cutoff[0]
            else:
                conflict_cutoff = min(
                    (value["captured_at"] for value in candidates.values()),
                    default=None,
                )
        has_conflict = conflict_cutoff is not None
        for candidate in conflict_candidates:
            self.connection.execute(
                """INSERT INTO vision_draft_conflicts
                   (raybet_match_id, map_number, captured_at,
                    source_frame_ref, observed_draft_hash,
                    radiant_hero_ids, dire_hero_ids,
                    observed_radiant_team_side, reason, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (
                    match_id,
                    map_number,
                    candidate["captured_at"].isoformat(),
                    candidate["source_frame_ref"],
                    candidate["draft_hash"],
                    candidate["radiant_json"],
                    candidate["dire_json"],
                    candidate["radiant_team_side"],
                    VISION_DRAFT_CONFLICT_REASON,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        if has_conflict:
            cutoff = conflict_cutoff
            for candidate in ordered:
                if candidate["captured_at"] < cutoff:
                    continue
                if candidate["draft_hash"] != canonical_hash or candidate in conflict_candidates:
                    continue
                if (
                    candidate["captured_at"], candidate["source_frame_ref"]
                ) == (
                    canonical["captured_at"], canonical["source_frame_ref"]
                ):
                    continue
                self.connection.execute(
                    """INSERT INTO vision_draft_conflicts
                       (raybet_match_id, map_number, captured_at,
                        source_frame_ref, observed_draft_hash,
                        radiant_hero_ids, dire_hero_ids,
                        observed_radiant_team_side, reason, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT DO NOTHING""",
                    (
                        match_id,
                        map_number,
                        candidate["captured_at"].isoformat(),
                        candidate["source_frame_ref"],
                        candidate["draft_hash"],
                        candidate["radiant_json"],
                        candidate["dire_json"],
                        candidate["radiant_team_side"],
                        VISION_DRAFT_CONFLICT_REASON,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

        side_time = side_source["captured_at"].isoformat() if side_source else None
        side_ref = side_source["source_frame_ref"] if side_source else None
        status = "conflict" if has_conflict else "anchored"
        conflict_at = conflict_cutoff.isoformat() if conflict_cutoff else None
        self.connection.execute(
            "SET LOCAL dota2.allow_vision_anchor_rebuild = 'on'"
        )
        self.connection.execute(
            """UPDATE vision_draft_anchors
                  SET draft_hash=?, radiant_hero_ids=?, dire_hero_ids=?,
                      radiant_team_side=?, team_side_anchored_at=?,
                      team_side_source_frame_ref=?, anchored_at=?,
                      source_frame_ref=?, status=?, conflict_at=?
                WHERE raybet_match_id=? AND map_number=?""",
            (
                canonical["draft_hash"],
                canonical["radiant_json"],
                canonical["dire_json"],
                canonical_side,
                side_time,
                side_ref,
                canonical["captured_at"].isoformat(),
                canonical["source_frame_ref"],
                status,
                conflict_at,
                match_id,
                map_number,
            ),
        )
        self.connection.execute(
            "SET LOCAL dota2.allow_vision_anchor_rebuild = 'off'"
        )

        for row in self.connection.execute(
            """SELECT captured_at, source_frame_ref, radiant_hero_ids,
                      dire_hero_ids, radiant_team_side
                 FROM vision_observations
                WHERE raybet_match_id=? AND map_number=?""",
            (match_id, map_number),
        ).fetchall():
            key = (str(row["captured_at"]), str(row["source_frame_ref"]))
            candidate = candidates.get(key)
            if candidate is None or key in invalidated:
                continue
            trusted = candidate["draft_hash"] == canonical_hash
            if (
                trusted
                and canonical_side is not None
                and candidate["radiant_team_side"] is not None
                and candidate["radiant_team_side"] != canonical_side
            ):
                trusted = False
            if conflict_cutoff is not None and candidate["captured_at"] >= conflict_cutoff:
                trusted = False
            self.connection.execute(
                """UPDATE vision_observations SET confirmed=?
                    WHERE raybet_match_id=? AND captured_at=?
                      AND source_frame_ref=?""",
                (int(trusted), match_id, row["captured_at"], row["source_frame_ref"]),
            )

        current_key = (
            self._iso(observation.captured_at), str(observation.source_frame_ref)
        )
        current = candidates.get(current_key)
        if current is None:
            return False
        trusted = current["draft_hash"] == canonical_hash
        if (
            trusted
            and canonical_side is not None
            and current["radiant_team_side"] is not None
            and current["radiant_team_side"] != canonical_side
        ):
            trusted = False
        if conflict_cutoff is not None and current["captured_at"] >= conflict_cutoff:
            trusted = False
        return trusted

    def _scalar(value: Any) -> Any:
        return value.value if isinstance(value, Enum) else value

    def upsert_provider_match(self, match: ProviderMatch, updated_at: datetime) -> None:
        self.execute(
            """INSERT INTO provider_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, provider_match_id) DO UPDATE SET
              tournament=excluded.tournament, team_one=excluded.team_one,
              team_two=excluded.team_two, scheduled_at=excluded.scheduled_at,
              best_of=excluded.best_of, status=excluded.status,
              raw_json=excluded.raw_json, updated_at=excluded.updated_at""",
            (match.provider, match.provider_match_id, match.tournament, match.team_one,
             match.team_two, match.scheduled_at.isoformat() if match.scheduled_at else None,
             match.best_of, match.status, self.json(sanitize_raybet_payload(match.raw)),
             updated_at.isoformat()),
        )

    def upsert_raybet_match(
        self,
        row: dict[str, Any],
        updated_at: datetime,
        *,
        public_live_url: object = None,
    ) -> None:
        safe_row = sanitize_raybet_payload(row)
        if not isinstance(safe_row, dict):
            raise ValueError("RayBet match payload must be an object")
        safe_row.pop(PUBLIC_STREAM_EVIDENCE_KEY, None)
        evidence = public_stream_evidence(public_live_url)
        if evidence is None:
            safe_row.pop("live_url", None)
        else:
            safe_row["live_url"] = evidence["url"]
            safe_row[PUBLIC_STREAM_EVIDENCE_KEY] = evidence
        team_one_row, team_two_row = self._raybet_teams_for_write(safe_row)
        team_one = str(team_one_row.get("team_name") or "")
        team_two = str(team_two_row.get("team_name") or "")
        round_name = str(safe_row.get("round") or "").lower()
        best_of = int(round_name[2:]) if round_name.startswith("bo") and round_name[2:].isdigit() else None
        self.execute(
            """INSERT INTO raybet_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(raybet_match_id) DO UPDATE SET
              tournament=excluded.tournament, team_one=excluded.team_one,
              team_two=excluded.team_two, scheduled_at=excluded.scheduled_at,
              best_of=excluded.best_of, status=excluded.status,
              live_url=excluded.live_url, raw_json=excluded.raw_json,
              updated_at=excluded.updated_at
            WHERE excluded.updated_at::timestamptz IS NOT NULL
              AND (
                    raybet_matches.updated_at::timestamptz IS NULL
                    OR excluded.updated_at::timestamptz >=
                       raybet_matches.updated_at::timestamptz
              )""",
            (
                str(safe_row.get("id")),
                str(safe_row.get("tournament_name") or ""),
                team_one,
                team_two,
                safe_row.get("start_time"),
                best_of,
                str(safe_row.get("status") or ""),
                safe_row.get("live_url"),
                self.json(safe_row),
                updated_at.isoformat(),
            ),
        )

    def insert_browser_raybet_match(
        self, row: dict[str, Any], updated_at: datetime
    ) -> bool:
        """Insert sanitized browser metadata without replacing direct-owned data."""
        safe_row = sanitize_raybet_payload(row)
        if not isinstance(safe_row, dict):
            raise ValueError("RayBet browser metadata must be an object")
        team_one_row, team_two_row = self._raybet_teams_for_write(safe_row)
        team_one = str(team_one_row.get("team_name") or "")
        team_two = str(team_two_row.get("team_name") or "")
        round_name = str(safe_row.get("round") or "").lower()
        best_of = (
            int(round_name[2:])
            if round_name.startswith("bo") and round_name[2:].isdigit()
            else None
        )
        cursor = self.execute(
            """INSERT INTO raybet_matches
            (raybet_match_id, tournament, team_one, team_two, scheduled_at, best_of,
             status, live_url, raw_json, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
             ON CONFLICT DO NOTHING""",
            (
                str(safe_row.get("id")),
                str(safe_row.get("tournament_name") or ""),
                team_one,
                team_two,
                safe_row.get("start_time"),
                best_of,
                str(safe_row.get("status") or ""),
                self.json(safe_row),
                updated_at.isoformat(),
            ),
        )
        return cursor.rowcount == 1

    def _raybet_teams_for_write(
        self,
        safe_row: dict[str, Any],
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Resolve an explicit pair or reuse one already stored for this match."""
        if (
            classify_raybet_match_format(safe_row) == RAYBET_MATCH_NON_HEAD_TO_HEAD
            and str(safe_row.get("match_short_name") or "").strip().casefold()
            == "outright"
        ):
            raise ValueError("raybet_non_head_to_head_match")
        if "team" in safe_row:
            return strict_raybet_head_to_head_teams(safe_row)

        match_id = str(safe_row.get("id") or "").strip()
        existing = self.connection.execute(
            """SELECT tournament, team_one, team_two, scheduled_at,
                      best_of, status, raw_json
                 FROM raybet_matches WHERE raybet_match_id=?""",
            (match_id,),
        ).fetchone()
        if existing is None:
            raise ValueError("raybet_exact_team_metadata_missing")
        team_one = str(existing["team_one"] or "").strip()
        team_two = str(existing["team_two"] or "").strip()
        if (
            not team_one
            or not team_two
            or team_one.casefold() == team_two.casefold()
        ):
            raise ValueError("raybet_existing_team_identity_invalid")
        try:
            existing_payload = json.loads(str(existing["raw_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("raybet_existing_team_identity_invalid") from error
        if not isinstance(existing_payload, dict):
            raise ValueError("raybet_existing_team_identity_invalid")
        if (
            classify_raybet_match_format(existing_payload)
            == RAYBET_MATCH_NON_HEAD_TO_HEAD
            and str(existing_payload.get("match_short_name") or "")
            .strip()
            .casefold()
            == "outright"
        ):
            raise ValueError("raybet_non_head_to_head_match")
        for key in (
            "game_id",
            "tournament_id",
            "tournament_name",
            "match_name",
            "match_short_name",
            "start_time",
            "round",
            "status",
        ):
            if key not in safe_row and key in existing_payload:
                safe_row[key] = existing_payload[key]
        if "tournament_name" not in safe_row and existing["tournament"]:
            safe_row["tournament_name"] = str(existing["tournament"])
        if "start_time" not in safe_row and existing["scheduled_at"] is not None:
            safe_row["start_time"] = existing["scheduled_at"]
        if "round" not in safe_row and existing["best_of"] is not None:
            safe_row["round"] = f"bo{int(existing['best_of'])}"
        if "status" not in safe_row and existing["status"] is not None:
            safe_row["status"] = existing["status"]
        if "team" in existing_payload:
            existing_one, existing_two = strict_raybet_head_to_head_teams(
                existing_payload
            )
            safe_row["team"] = [dict(existing_one), dict(existing_two)]
        else:
            safe_row["team"] = [
                {"pos": 1, "team_name": team_one},
                {"pos": 2, "team_name": team_two},
            ]
        return strict_raybet_head_to_head_teams(safe_row)

    def insert_browser_event(
        self,
        event: Mapping[str, Any] | Any,
        *,
        received_at: datetime,
        recognized: bool,
        raw_artifact: ArtifactReceipt | None = None,
        processing_status: str = "pending",
        processing_reason: str | None = None,
    ) -> bool:
        captured_at = self._event_value(
            event, "captured_at_utc", self._event_value(event, "captured_at")
        )
        payload = self._event_value(event, "payload", {})
        if raw_artifact is None:
            raw_artifact = self.archive_response_payload(
                payload,
                observed_at=captured_at,
                match_id=self._event_value(event, "raybet_match_id"),
            )
        self._register_raw_artifact(raw_artifact)
        cursor = self.execute(
            """INSERT INTO browser_events
            (event_id, schema_version, capture_session_id, captured_at, received_at,
             transport, event_type, raybet_match_id, game_id, page_origin, page_path,
             source_path, payload_hash, payload_bytes, payload_json,
             payload_artifact_hash, payload_storage, capture_reason,
             extension_version, recognized, processing_status, processing_reason)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
             ON CONFLICT DO NOTHING""",
            (
                str(self._event_value(event, "event_id")),
                int(self._event_value(event, "schema_version")),
                str(self._event_value(event, "capture_session_id")),
                self._iso(captured_at),
                self._iso(received_at),
                str(self._scalar(self._event_value(event, "transport"))),
                str(self._scalar(self._event_value(event, "event_type"))),
                self._event_value(event, "raybet_match_id"),
                self._event_value(event, "game_id"),
                str(self._event_value(event, "page_origin")),
                str(self._event_value(event, "page_path")),
                str(self._event_value(event, "source_path")),
                str(self._event_value(event, "payload_hash")),
                int(self._event_value(event, "payload_bytes")),
                self.json({}),
                raw_artifact.content_sha256,
                "external",
                self._event_value(event, "capture_reason"),
                str(self._event_value(event, "extension_version")),
                int(recognized),
                processing_status,
                processing_reason,
            ),
        )
        return cursor.rowcount == 1

    def browser_event_identity_matches(self, event: Mapping[str, Any] | Any) -> bool:
        """Check immutable retry identity before treating an event ID as duplicate."""
        event_id = str(self._event_value(event, "event_id"))
        row = self.connection.execute(
            """SELECT schema_version, capture_session_id, captured_at, transport,
                      event_type, raybet_match_id, game_id, page_origin, page_path,
                      source_path, payload_hash, payload_bytes, capture_reason,
                      extension_version, payload_storage, payload_artifact_hash,
                      payload_json
                 FROM browser_events WHERE event_id=?""",
            (event_id,),
        ).fetchone()
        if row is None:
            return False
        captured_at = self._event_value(
            event, "captured_at_utc", self._event_value(event, "captured_at")
        )
        expected = (
            int(self._event_value(event, "schema_version")),
            str(self._event_value(event, "capture_session_id")),
            self._iso(captured_at),
            str(self._scalar(self._event_value(event, "transport"))),
            str(self._scalar(self._event_value(event, "event_type"))),
            self._event_value(event, "raybet_match_id"),
            self._event_value(event, "game_id"),
            str(self._event_value(event, "page_origin")),
            str(self._event_value(event, "page_path")),
            str(self._event_value(event, "source_path")),
            str(self._event_value(event, "payload_hash")),
            int(self._event_value(event, "payload_bytes")),
            self._event_value(event, "capture_reason"),
            str(self._event_value(event, "extension_version")),
        )
        if tuple(row[:14]) != expected:
            return False
        if str(row["payload_storage"]) == "external":
            try:
                return self._read_raw_artifact(str(row["payload_artifact_hash"])) == self._event_value(
                    event, "payload", {}
                )
            except (RuntimeError, ValueError, TypeError):
                return False
        return self.json(self._event_value(event, "payload", {})) == str(
            row["payload_json"]
        )

    def update_browser_event_status(
        self, event_id: str, status: str, reason: str | None = None
    ) -> bool:
        cursor = self.execute(
            """UPDATE browser_events
               SET processing_status=?, processing_reason=? WHERE event_id=?""",
            (status, reason, event_id),
        )
        return cursor.rowcount == 1

    def observation_timing_status(
        self, raybet_match_id: str, observed_at: datetime, *, source: str
    ) -> str:
        source_filter = " AND source='direct'" if source == "direct" else ""
        newest = self.connection.execute(
            f"""SELECT observed_at FROM odds_transport_observations
               WHERE raybet_match_id=? AND timing_status!='late'
               {source_filter}
               ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
            (raybet_match_id,),
        ).fetchone()
        if newest and self._iso(observed_at) < str(newest["observed_at"]):
            return "late"
        return "on_time"

    def insert_transport_observation(
        self,
        *,
        observation_key: str,
        source: str,
        source_event_id: str | None,
        raybet_match_id: str,
        observed_at: datetime,
        normalized_state_hash: str,
        response_state_hash: str,
        response_artifact_hash: str,
        timing_status: str,
        processing_status: str,
        normalized_change_count: int,
        normalized_state_hash_version: int = 2,
        original_legacy_normalized_state_hash: str | None = None,
    ) -> bool:
        cursor = self.execute(
            """INSERT INTO odds_transport_observations
            (observation_key, source, source_event_id, raybet_match_id, observed_at,
             normalized_state_hash, normalized_state_hash_version,
             original_legacy_normalized_state_hash, response_state_hash,
             response_artifact_hash, timing_status, processing_status,
             normalized_change_count)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
             ON CONFLICT DO NOTHING""",
            (
                observation_key,
                source,
                source_event_id,
                raybet_match_id,
                self._iso(observed_at),
                normalized_state_hash,
                normalized_state_hash_version,
                original_legacy_normalized_state_hash,
                response_state_hash,
                response_artifact_hash,
                timing_status,
                processing_status,
                normalized_change_count,
            ),
        )
        return cursor.rowcount == 1

    def store_odds_observation(
        self,
        *,
        source: str,
        observation_key: str,
        source_event_id: str | None,
        raybet_match_id: str,
        observed_at: datetime,
        normalized_state_hash: str,
        snapshots: Sequence[OddsSnapshot],
        raw_payload: Mapping[str, Any] | None = None,
        raw_artifact: ArtifactReceipt | None = None,
        audit_only: bool = False,
    ) -> tuple[str, int]:
        """Atomically retain transport, deduplicated state, and exact raw evidence."""
        from .markets import (
            is_closed_odds_member,
            normalized_state_hash as compute_normalized_state_hash,
            snapshots_from_payload,
            snapshot_state_outcome,
        )

        seen_odds_ids: set[str] = set()
        for snapshot in snapshots:
            if snapshot.raybet_match_id != raybet_match_id:
                raise ValueError("response outcome match id mismatch")
            if snapshot.received_at != observed_at:
                raise ValueError("response outcome transport time mismatch")
            if snapshot.odds_id in seen_odds_ids:
                raise ValueError("duplicate odds id in one response")
            if not math.isfinite(snapshot.price):
                raise ValueError("response outcome price must be finite")
            if snapshot.market.line is not None and not math.isfinite(
                float(snapshot.market.line)
            ):
                raise ValueError("response outcome line must be finite")
            seen_odds_ids.add(snapshot.odds_id)

        validated_payload = self._validated_response_payload(
            raw_payload=raw_payload,
            raw_artifact=raw_artifact,
        )
        raw_snapshots = snapshots_from_payload(
            validated_payload,
            received_at=observed_at,
        )
        result = validated_payload.get("result")
        raw_members = result.get("odds") if isinstance(result, dict) else None
        if not isinstance(result, dict) or str(result.get("id") or "") != raybet_match_id:
            raise ValueError("response raw payload match id mismatch")
        closed_member_count = (
            sum(is_closed_odds_member(member) for member in raw_members)
            if isinstance(raw_members, list)
            else 0
        )
        if (
            not isinstance(raw_members, list)
            or len(raw_members) != len(raw_snapshots) + closed_member_count
        ):
            raise ValueError("response raw payload contains unparsed odds members")
        caller_members = canonical_state_outcomes(
            snapshot_state_outcome(snapshot) for snapshot in snapshots
        )
        raw_semantic_members = canonical_state_outcomes(
            snapshot_state_outcome(snapshot) for snapshot in raw_snapshots
        )
        if caller_members != raw_semantic_members:
            raise ValueError("response snapshots do not match raw semantic membership")
        snapshots = raw_snapshots
        raw_payload = validated_payload

        computed_normalized_hash = compute_normalized_state_hash(snapshots)
        if normalized_state_hash != computed_normalized_hash:
            raise ValueError("normalized state hash does not match response membership")
        state_hash, state_values = self._response_state_identity(
            raybet_match_id,
            normalized_state_hash,
            snapshots,
        )
        artifact_hash, _, artifact_receipt = self._response_artifact_identity(
            raybet_match_id,
            snapshots,
            raw_payload=raw_payload,
            raw_artifact=raw_artifact,
        )

        with self.transaction():
            existing = self.connection.execute(
                """SELECT source, source_event_id, raybet_match_id, observed_at,
                          normalized_state_hash, normalized_state_hash_version,
                          original_legacy_normalized_state_hash, response_state_hash,
                          response_artifact_hash, timing_status,
                          normalized_change_count
                   FROM odds_transport_observations WHERE observation_key=?""",
                (observation_key,),
            ).fetchone()
            if existing:
                identity = (
                    str(existing["source"]),
                    existing["source_event_id"],
                    str(existing["raybet_match_id"]),
                    str(existing["observed_at"]),
                    str(existing["normalized_state_hash"]),
                    int(existing["normalized_state_hash_version"]),
                    existing["original_legacy_normalized_state_hash"],
                )
                expected = (
                    source,
                    source_event_id,
                    raybet_match_id,
                    self._iso(observed_at),
                    normalized_state_hash,
                    2,
                    None,
                )
                if identity != expected:
                    raise ValueError("observation key already belongs to another response")
                storage_refs = (
                    existing["response_state_hash"],
                    existing["response_artifact_hash"],
                )
                if storage_refs != (None, None) and storage_refs != (
                    state_hash,
                    artifact_hash,
                ):
                    raise ValueError(
                        "observation key response membership or payload differs"
                    )
                persisted_outcomes = self.connection.execute(
                    """SELECT raybet_match_id, odds_id, odds_group_id, received_at,
                              price, status, market_type, period, side, line,
                              outcome_key, supported, last_update
                         FROM odds_response_outcomes_effective
                        WHERE observation_key=? ORDER BY odds_id""",
                    (observation_key,),
                ).fetchall()
                if not persisted_outcomes:
                    if snapshots:
                        raise ValueError(
                            "observation key response membership or payload differs"
                        )
                actual_outcomes = [tuple(row) for row in persisted_outcomes]
                expected_outcomes = sorted(
                    (
                        self._effective_response_outcome_values(snapshot)
                        for snapshot in snapshots
                    ),
                    key=lambda values: str(values[1]),
                )
                if actual_outcomes != expected_outcomes:
                    raise ValueError(
                        "observation key response membership or payload differs"
                    )
                if storage_refs == (None, None):
                    legacy_raw = self.connection.execute(
                        """SELECT raw_json FROM odds_response_outcomes
                            WHERE observation_key=? ORDER BY odds_id""",
                        (observation_key,),
                    ).fetchall()
                    expected_raw = sorted(
                        self._snapshot_raw_json(snapshot) for snapshot in snapshots
                    )
                    if sorted(str(row[0]) for row in legacy_raw) != expected_raw:
                        raise ValueError(
                            "observation key response membership or payload differs"
                        )
                return str(existing["timing_status"]), 0

            if artifact_receipt is None:
                artifact_receipt = self.archive_response_payload(
                    raw_payload
                    if raw_payload is not None
                    else snapshot_derived_payload(
                        raybet_match_id,
                        (snapshot.raw for snapshot in snapshots),
                    ),
                    observed_at=observed_at,
                    match_id=raybet_match_id,
                )
                if artifact_receipt.content_sha256 != artifact_hash:
                    raise ValueError("response artifact hash mismatch")
            self._register_raw_artifact(artifact_receipt)
            self._persist_response_state(
                state_hash,
                raybet_match_id,
                normalized_state_hash,
                state_values,
            )
            timing_status = self.observation_timing_status(
                raybet_match_id, observed_at, source=source
            )
            processing_status = (
                "audit_only"
                if audit_only or timing_status == "late"
                else "processing"
            )
            inserted = self.insert_transport_observation(
                observation_key=observation_key,
                source=source,
                source_event_id=source_event_id,
                raybet_match_id=raybet_match_id,
                observed_at=observed_at,
                normalized_state_hash=normalized_state_hash,
                response_state_hash=state_hash,
                response_artifact_hash=artifact_hash,
                timing_status=timing_status,
                processing_status=processing_status,
                normalized_change_count=0,
            )
            if not inserted:
                return timing_status, 0

            change_count = 0
            if timing_status != "late" and not audit_only:
                change_count = sum(int(self.insert_odds(snapshot)) for snapshot in snapshots)
                processing_status = "processed"
            self.execute(
                """UPDATE odds_transport_observations
                   SET processing_status=?, normalized_change_count=?
                   WHERE observation_key=?""",
                (processing_status, change_count, observation_key),
            )
            return timing_status, change_count

    def _response_state_identity(
        self,
        raybet_match_id: str,
        normalized_state_hash: str,
        snapshots: Sequence[OddsSnapshot],
    ) -> tuple[str, list[tuple[Any, ...]]]:
        state_hash, values, _ = canonical_response_state_identity(
            raybet_match_id,
            normalized_state_hash,
            (
                self._response_state_outcome_values(snapshot)
                for snapshot in snapshots
            ),
        )
        return state_hash, list(values)

    def _validated_response_payload(
        self,
        *,
        raw_payload: Mapping[str, Any] | None,
        raw_artifact: ArtifactReceipt | None,
    ) -> dict[str, Any]:
        if raw_payload is None and raw_artifact is None:
            raise ValueError("exact raw response evidence is required")

        supplied_payload: dict[str, Any] | None = None
        supplied_hash: str | None = None
        supplied_bytes: bytes | None = None
        if raw_payload is not None:
            supplied_hash, supplied_bytes, sanitized = (
                canonical_response_artifact_identity(raw_payload)
            )
            if not isinstance(sanitized, dict):
                raise ValueError("response raw payload must be an object")
            supplied_payload = sanitized

        if raw_artifact is None:
            assert supplied_payload is not None
            return supplied_payload

        if raw_artifact.source != "raybet":
            raise ValueError("RayBet artifact source is required")
        RawArchive._verify(raw_artifact.path, raw_artifact.content_sha256)
        try:
            compressed = raw_artifact.path.read_bytes()
            canonical = gzip.decompress(compressed)
            artifact_payload = json.loads(canonical.decode("utf-8"))
        except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("response raw artifact is corrupt") from error
        artifact_hash, artifact_bytes, sanitized_artifact = (
            canonical_response_artifact_identity(artifact_payload)
        )
        if not isinstance(sanitized_artifact, dict):
            raise ValueError("response raw artifact must contain an object")
        if (
            artifact_hash != raw_artifact.content_sha256
            or artifact_bytes != canonical
            or len(canonical) != raw_artifact.byte_count
            or len(compressed) != raw_artifact.compressed_byte_count
            or schema_fingerprint(artifact_payload) != raw_artifact.schema_fingerprint
        ):
            raise ValueError("response raw artifact metadata mismatch")
        if supplied_payload is not None and (
            supplied_hash != artifact_hash or supplied_bytes != artifact_bytes
        ):
            raise ValueError("response raw artifact does not match payload")
        return sanitized_artifact

    def _response_artifact_identity(
        self,
        raybet_match_id: str,
        snapshots: Sequence[OddsSnapshot],
        *,
        raw_payload: Mapping[str, Any] | None,
        raw_artifact: ArtifactReceipt | None,
    ) -> tuple[str, bytes, ArtifactReceipt | None]:
        if raw_artifact is not None:
            if raw_payload is not None:
                payload_hash, _, _ = canonical_response_artifact_identity(
                    raw_payload
                )
                if payload_hash != raw_artifact.content_sha256:
                    raise ValueError("response raw artifact does not match payload")
            return raw_artifact.content_sha256, b"", raw_artifact
        if raw_payload is None:
            payload: Any = snapshot_derived_payload(
                raybet_match_id,
                (snapshot.raw for snapshot in snapshots),
            )
        else:
            payload = raw_payload
        artifact_hash, encoded, _ = canonical_response_artifact_identity(payload)
        return artifact_hash, encoded, None

    def archive_response_payload(
        self,
        payload: Any,
        *,
        observed_at: datetime,
        match_id: str | None,
        response_kind: str = "live_odds",
        endpoint: str | None = None,
        request_identity: str | None = None,
        status_code: int | None = None,
    ) -> ArtifactReceipt:
        try:
            default_endpoint = _DIRECT_RESPONSE_ENDPOINTS[response_kind]
        except KeyError as error:
            raise ValueError("direct response kind is invalid") from error
        endpoint = endpoint or default_endpoint
        request_identity = request_identity or (
            f"{endpoint}?match_id={match_id}" if match_id else endpoint
        )
        artifact_hash, encoded, _ = canonical_response_artifact_identity(payload)
        numeric_match = int(match_id) if match_id and str(match_id).isdigit() else None
        receipt = self.raw_archive.archive_json(
            source="raybet",
            endpoint=endpoint,
            request_identity=request_identity,
            payload_bytes=encoded,
            observed_at=observed_at,
            match_id=numeric_match,
            status_code=status_code,
        )
        if receipt.content_sha256 != artifact_hash:
            raise RuntimeError("response artifact hash mismatch")
        return receipt

    def _register_raw_artifact(self, receipt: ArtifactReceipt) -> None:
        if receipt.source != "raybet":
            raise ValueError("RayBet artifact source is required")
        RawArchive._verify(receipt.path, receipt.content_sha256)
        try:
            receipt.path.resolve().relative_to(self.raw_archive_root)
        except ValueError:
            try:
                canonical = gzip.decompress(receipt.path.read_bytes())
            except (OSError, EOFError) as error:
                raise ValueError("response artifact is corrupt") from error
            receipt = self.raw_archive.archive_json(
                source="raybet",
                endpoint=receipt.endpoint,
                request_identity=receipt.request_identity,
                payload_bytes=canonical,
                observed_at=receipt.observed_at,
                match_id=receipt.match_id,
                status_code=receipt.status_code,
                source_timestamp=receipt.source_timestamp,
                first_usable_at=receipt.first_usable_at,
            )
        relative_path = receipt.path.resolve().relative_to(self.raw_archive_root)
        self.execute(
            """INSERT INTO odds_raw_artifacts
               (artifact_hash, source, storage_path, uncompressed_bytes,
                compressed_bytes, schema_fingerprint)
               VALUES (?, 'raybet', ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (
                receipt.content_sha256,
                relative_path.as_posix(),
                receipt.byte_count,
                receipt.compressed_byte_count,
                receipt.schema_fingerprint,
            ),
        )
        row = self.connection.execute(
            """SELECT source, storage_path, uncompressed_bytes,
                      compressed_bytes, schema_fingerprint
                 FROM odds_raw_artifacts WHERE artifact_hash=?""",
            (receipt.content_sha256,),
        ).fetchone()
        if row is None:
            raise RuntimeError("response artifact insert was not durable")
        identity = (
            str(row["source"]),
            int(row["uncompressed_bytes"]),
            int(row["compressed_bytes"]),
            str(row["schema_fingerprint"]),
        )
        expected = (
            "raybet",
            receipt.byte_count,
            receipt.compressed_byte_count,
            receipt.schema_fingerprint,
        )
        if identity != expected:
            raise ValueError("response artifact hash collision or content mismatch")
        existing_relative = Path(str(row["storage_path"]))
        if existing_relative.is_absolute():
            raise ValueError("response artifact path must be relative")
        existing_path = (self.raw_archive_root / existing_relative).resolve()
        try:
            existing_path.relative_to(self.raw_archive_root)
        except ValueError as error:
            raise ValueError("response artifact path escapes archive root") from error
        try:
            existing_content = gzip.decompress(existing_path.read_bytes())
            incoming_content = gzip.decompress(receipt.path.read_bytes())
        except (OSError, EOFError) as error:
            raise ValueError("response artifact is corrupt") from error
        if existing_content != incoming_content:
            raise ValueError("response artifact hash collision or content mismatch")

    def record_direct_response_audit(
        self,
        receipt: ArtifactReceipt,
        *,
        response_kind: str,
        claimed_raybet_match_id: str | None,
        observed_raybet_match_id: str | None,
        disposition: str,
        reason: str,
        provider_code: int | None = None,
        request_metadata: Mapping[str, Any] | None = None,
        payload_kind: str = "provider_response",
        sanitized: bool = True,
    ) -> str:
        if response_kind not in _DIRECT_RESPONSE_ENDPOINTS:
            raise ValueError("direct response kind is invalid")
        if disposition not in {"accepted", "rejected", "audit_only"}:
            raise ValueError("direct response disposition is invalid")
        if payload_kind not in {
            "provider_response",
            "request_failure",
            "aggregate",
        }:
            raise ValueError("direct response payload kind is invalid")
        if type(sanitized) is not bool:
            raise ValueError("direct response sanitized flag must be boolean")
        if provider_code is not None and type(provider_code) is not int:
            raise ValueError("direct response provider code must be an integer")
        metadata = sanitize_raybet_payload(dict(request_metadata or {}))
        if not isinstance(metadata, dict):
            raise ValueError("direct response request metadata must be an object")
        metadata_json = json.dumps(
            metadata,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        normalized_reason = " ".join(str(reason).split())[:200]
        if not normalized_reason:
            raise ValueError("direct response reason is required")
        identity = (
            "direct-response-audit-v2",
            response_kind,
            self._iso(receipt.observed_at),
            claimed_raybet_match_id or "",
            observed_raybet_match_id or "",
            receipt.endpoint,
            receipt.request_identity,
            "" if receipt.status_code is None else str(receipt.status_code),
            "" if provider_code is None else str(provider_code),
            metadata_json,
            payload_kind,
            str(int(sanitized)),
            disposition,
            normalized_reason,
            receipt.content_sha256,
        )
        audit_key = hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()
        values = (
            audit_key,
            "direct",
            response_kind,
            self._iso(receipt.observed_at),
            claimed_raybet_match_id,
            observed_raybet_match_id,
            receipt.endpoint,
            receipt.request_identity,
            receipt.status_code,
            provider_code,
            metadata_json,
            payload_kind,
            int(sanitized),
            disposition,
            normalized_reason,
            receipt.content_sha256,
        )
        with self.transaction():
            self._register_raw_artifact(receipt)
            self.execute(
                """INSERT INTO direct_response_audit
                   (audit_key, source, response_kind, observed_at,
                    claimed_raybet_match_id, observed_raybet_match_id,
                    endpoint, request_identity, http_status, provider_code,
                    request_metadata_json, payload_kind, sanitized,
                    disposition, reason, artifact_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                values,
            )
            existing = self.connection.execute(
                """SELECT audit_key, source, response_kind, observed_at,
                          claimed_raybet_match_id, observed_raybet_match_id,
                          endpoint, request_identity, http_status, provider_code,
                          request_metadata_json, payload_kind, sanitized,
                          disposition, reason, artifact_hash
                     FROM direct_response_audit WHERE audit_key=?""",
                (audit_key,),
            ).fetchone()
            if existing is None or tuple(existing) != values:
                raise ValueError("direct response audit key was reused")
        return audit_key

    def direct_response_payload(self, audit_key: str) -> Any:
        """Replay the sanitized JSON value for one immutable direct audit."""
        row = self.connection.execute(
            "SELECT artifact_hash FROM direct_response_audit WHERE audit_key=?",
            (audit_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("direct response audit is missing")
        return self._read_raw_artifact(str(row["artifact_hash"]))

    def _read_raw_artifact(self, artifact_hash: str) -> Any:
        return _load_odds_raw_artifact(
            self.connection,
            self.raw_archive_root,
            artifact_hash,
        )

    def browser_event_payload(self, event_id: str) -> dict[str, Any]:
        """Load and revalidate an externalized browser payload."""
        return read_browser_event_payload(
            self.connection,
            self.raw_archive_root,
            event_id,
        )

    def response_raw_payload(self, observation_key: str) -> dict[str, Any]:
        """Return the exact raw response envelope for one transport observation."""
        row = self.connection.execute(
            """SELECT response_artifact_hash, source, raybet_match_id
                 FROM odds_transport_observations WHERE observation_key=?""",
            (observation_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("transport observation is missing")
        if row["response_artifact_hash"] is not None:
            payload = self._read_raw_artifact(str(row["response_artifact_hash"]))
            if not isinstance(payload, dict):
                raise RuntimeError("response raw artifact is not an object")
            return payload
        # Legacy rows have no external artifact. Reconstruct only the exact
        # retained outcome members; callers must treat the envelope as partial.
        raw_rows = self.connection.execute(
            """SELECT raw_json FROM odds_response_outcomes
                WHERE observation_key=? ORDER BY odds_id""",
            (observation_key,),
        ).fetchall()
        odds: list[Any] = []
        for raw_row in raw_rows:
            try:
                value = json.loads(str(raw_row["raw_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("legacy response outcome is invalid") from error
            odds.append(value)
        return {
            "result": {
                "id": str(row["raybet_match_id"]),
                "odds": odds,
            }
        }

    def response_outcomes(
        self,
        observation_key: str,
        *,
        raybet_match_id: str | None = None,
        period: str | None = None,
        include_raw: bool = False,
    ) -> list[dict[str, Any]]:
        """Read one exact response through the v2-first compatibility API."""
        clauses = ["observation_key=?"]
        parameters: list[Any] = [observation_key]
        if raybet_match_id is not None:
            clauses.append("raybet_match_id=?")
            parameters.append(raybet_match_id)
        if period is not None:
            clauses.append("period=?")
            parameters.append(period)
        rows = self.connection.execute(
            """SELECT observation_key, raybet_match_id, odds_id, odds_group_id,
                      received_at, price, status, market_type, period, side,
                      line, outcome_key, supported, last_update, raw_json,
                      response_state_hash, response_artifact_hash, storage_version
                 FROM odds_response_outcomes_effective WHERE """
            + " AND ".join(clauses)
            + " ORDER BY odds_id",
            tuple(parameters),
        ).fetchall()
        result = [dict(row) for row in rows]
        if not include_raw or not result:
            return result
        if result[0]["storage_version"] == "legacy":
            return result
        payload = self.response_raw_payload(observation_key)
        envelope = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        raw_items = envelope.get("odds") if isinstance(envelope, dict) else None
        if not isinstance(raw_items, list):
            raise RuntimeError("response raw artifact has no odds array")
        by_id: dict[str, Any] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("odds_id") or item.get("id") or "")
            if item_id:
                if item_id in by_id:
                    raise RuntimeError("response raw artifact has duplicate odds id")
                by_id[item_id] = item
        for row in result:
            item = by_id.get(str(row["odds_id"]))
            if item is None:
                raise RuntimeError("response raw artifact is missing an outcome")
            row["raw_json"] = json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        return result

    def _persist_response_state(
        self,
        state_hash: str,
        raybet_match_id: str,
        normalized_state_hash: str,
        outcomes: Sequence[tuple[Any, ...]],
        *,
        normalized_state_hash_version: int = 2,
        original_legacy_normalized_state_hash: str | None = None,
    ) -> None:
        conflicts = self.connection.execute(
            """SELECT response_state_hash FROM odds_response_states
                WHERE raybet_match_id=? AND normalized_state_hash=?
                  AND normalized_state_hash_version=?
                  AND original_legacy_normalized_state_hash IS NOT DISTINCT FROM ?""",
            (
                raybet_match_id,
                normalized_state_hash,
                normalized_state_hash_version,
                original_legacy_normalized_state_hash,
            ),
        ).fetchall()
        if conflicts and {str(row[0]) for row in conflicts} != {state_hash}:
            raise ValueError(
                "normalized state hash maps to a different response manifest"
            )
        self.execute(
            """INSERT INTO odds_response_states
               (response_state_hash, raybet_match_id, normalized_state_hash,
                normalized_state_hash_version,
                original_legacy_normalized_state_hash, outcome_count)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (
                state_hash,
                raybet_match_id,
                normalized_state_hash,
                normalized_state_hash_version,
                original_legacy_normalized_state_hash,
                len(outcomes),
            ),
        )
        for outcome in outcomes:
            self.execute(
                """INSERT INTO odds_response_state_outcomes
                   (response_state_hash, odds_id, odds_group_id, price, status,
                    market_type, period, side, line, outcome_key, supported,
                    last_update)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING""",
                (state_hash, *outcome),
            )
        state = self.connection.execute(
            """SELECT raybet_match_id, normalized_state_hash,
                      normalized_state_hash_version,
                      original_legacy_normalized_state_hash, outcome_count
                 FROM odds_response_states WHERE response_state_hash=?""",
            (state_hash,),
        ).fetchone()
        persisted = self.connection.execute(
            """SELECT odds_id, odds_group_id, price, status, market_type,
                      period, side, line, outcome_key, supported, last_update
                 FROM odds_response_state_outcomes
                WHERE response_state_hash=? ORDER BY odds_id""",
            (state_hash,),
        ).fetchall()
        if state is None or (
            str(state["raybet_match_id"]),
            str(state["normalized_state_hash"]),
            int(state["normalized_state_hash_version"]),
            state["original_legacy_normalized_state_hash"],
            int(state["outcome_count"]),
        ) != (
            raybet_match_id,
            normalized_state_hash,
            normalized_state_hash_version,
            original_legacy_normalized_state_hash,
            len(outcomes),
        ):
            raise ValueError("response state hash collision or content mismatch")
        if [tuple(row) for row in persisted] != list(outcomes):
            raise ValueError("response state hash collision or content mismatch")

    @staticmethod
    def _snapshot_raw_json(snapshot: OddsSnapshot) -> str:
        return json.dumps(
            sanitize_raybet_payload(snapshot.raw),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )

    def _response_state_outcome_values(
        self, snapshot: OddsSnapshot
    ) -> tuple[Any, ...]:
        from .markets import snapshot_state_outcome

        return snapshot_state_outcome(snapshot)

    def _effective_response_outcome_values(
        self, snapshot: OddsSnapshot
    ) -> tuple[Any, ...]:
        state = self._response_state_outcome_values(snapshot)
        return (
            snapshot.raybet_match_id,
            state[0],
            state[1],
            self._iso(snapshot.received_at),
            *state[2:],
        )

    def insert_odds(self, snapshot: OddsSnapshot) -> bool:
        market = snapshot.market
        previous = self.connection.execute(
            """SELECT odds_group_id, price, status, market_type, period,
                      side, line, outcome_key, supported, last_update
                 FROM odds_snapshots
            WHERE raybet_match_id=? AND odds_id=? AND received_at<=?
            ORDER BY received_at DESC, id DESC LIMIT 1""",
            (snapshot.raybet_match_id, snapshot.odds_id,
             self._iso(snapshot.received_at)),
        ).fetchone()
        current = (
            snapshot.odds_group_id,
            snapshot.price,
            str(snapshot.status),
            market.market_type,
            market.period,
            market.side,
            market.line,
            market.outcome_key,
            int(market.supported),
            snapshot.last_update,
        )
        if previous and tuple(previous) == current:
            return False
        cursor = self.execute(
            """INSERT INTO odds_snapshots
            (raybet_match_id, odds_id, odds_group_id, received_at, price, status,
             market_type, period, side, line, outcome_key, supported, last_update, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""",
            (snapshot.raybet_match_id, snapshot.odds_id, snapshot.odds_group_id,
              self._iso(snapshot.received_at), snapshot.price, str(snapshot.status),
             market.market_type, market.period, market.side, market.line,
             market.outcome_key, int(market.supported), snapshot.last_update,
             self.json(sanitize_raybet_payload(snapshot.raw))),
        )
        return cursor.rowcount == 1

    def record_collector(
        self, collector: str, *, success_at: datetime | None = None,
        error_at: datetime | None = None, error: str | None = None,
        cursor: str | None = None, gap: bool = False,
    ) -> None:
        self.execute(
            """INSERT INTO collector_runs
            (collector, last_success_at, last_error_at, last_error, cursor, gap_detected)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(collector) DO UPDATE SET
              last_success_at=COALESCE(excluded.last_success_at, collector_runs.last_success_at),
              last_error_at=COALESCE(excluded.last_error_at, collector_runs.last_error_at),
              last_error=excluded.last_error, cursor=COALESCE(excluded.cursor, collector_runs.cursor),
              gap_detected=excluded.gap_detected""",
            (collector, success_at.isoformat() if success_at else None,
             error_at.isoformat() if error_at else None, error, cursor, int(gap)),
        )

    def insert_vision_observation(self, observation: Any) -> bool:
        captured_at = self._iso(observation.captured_at)
        radiant_json = self.json(list(observation.radiant_hero_ids))
        dire_json = self.json(list(observation.dire_hero_ids))
        radiant_team_side = observation.radiant_team_side
        if radiant_team_side not in {None, "team_one", "team_two"}:
            raise ValueError(
                "radiant_team_side must be team_one, team_two, or null"
            )
        frame_sha256 = getattr(observation, "source_frame_sha256", None)
        frame_bytes = getattr(observation, "source_frame_bytes", None)
        frame_path = getattr(observation, "source_frame_path", None)
        frame_values = (frame_sha256, frame_bytes, frame_path)
        if any(value is not None for value in frame_values) and any(
            value is None for value in frame_values
        ):
            raise ValueError("vision frame integrity metadata must be complete")
        frame_receipt = None
        if all(value is not None for value in frame_values):
            frame_receipt = VisionFrameReceipt(
                frame_ref=str(observation.source_frame_ref),
                content_sha256=str(frame_sha256),
                byte_length=int(frame_bytes),
                storage_path=Path(str(frame_path)),
            )
        stored_confirmed = _valid_confirmed_vision_payload(
            observation.radiant_hero_ids,
            observation.dire_hero_ids,
            observation.source_frame_ref,
        ) and bool(
            observation.is_confirmed
            or getattr(observation, "is_draft_confirmed", False)
        ) and frame_receipt is not None
        with self.transaction():
            if frame_receipt is not None:
                register_vision_frame_artifact(
                    self.connection,
                    frame_receipt,
                    registered_at=observation.captured_at,
                )
            if stored_confirmed and observation.map_number is not None:
                draft_payload = self.json(
                    {
                        "radiant": list(observation.radiant_hero_ids),
                        "dire": list(observation.dire_hero_ids),
                    }
                )
                draft_hash = hashlib.sha256(draft_payload.encode("utf-8")).hexdigest()
                anchor = self.connection.execute(
                    """SELECT draft_hash, radiant_hero_ids, dire_hero_ids,
                              radiant_team_side, team_side_anchored_at,
                              team_side_source_frame_ref, anchored_at,
                              source_frame_ref, status, conflict_at
                         FROM vision_draft_anchors
                        WHERE raybet_match_id=? AND map_number=?""",
                    (observation.raybet_match_id, observation.map_number),
                ).fetchone()
                if anchor is None:
                    self.connection.execute(
                        """INSERT INTO vision_draft_anchors
                           (raybet_match_id, map_number, draft_hash,
                            radiant_hero_ids, dire_hero_ids,
                            radiant_team_side, team_side_anchored_at,
                            team_side_source_frame_ref, anchored_at,
                            source_frame_ref, status, conflict_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                   'anchored', NULL)""",
                        (
                            observation.raybet_match_id,
                            observation.map_number,
                            draft_hash,
                            radiant_json,
                            dire_json,
                            radiant_team_side,
                            captured_at if radiant_team_side is not None else None,
                            observation.source_frame_ref
                            if radiant_team_side is not None
                            else None,
                            captured_at,
                            observation.source_frame_ref,
                        ),
                    )
                else:
                    stored_confirmed = self._rebuild_vision_draft_anchor(
                        observation, anchor
                    )
            cursor = self.connection.execute(
                """INSERT INTO vision_observations
                (raybet_match_id, map_number, captured_at, game_clock_seconds,
                 is_paused, radiant_hero_ids, dire_hero_ids, radiant_team_side,
                 clock_confidence, draft_confidence, source_frame_ref,
                 source_frame_sha256, source_frame_bytes, screen_state,
                 confirmed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT DO NOTHING""",
                (
                    observation.raybet_match_id,
                    observation.map_number,
                    captured_at,
                    observation.game_clock_seconds,
                    None
                    if observation.is_paused is None
                    else int(observation.is_paused),
                    radiant_json,
                    dire_json,
                    radiant_team_side,
                    observation.clock_confidence,
                    observation.draft_confidence,
                    observation.source_frame_ref,
                    None if frame_receipt is None else frame_receipt.content_sha256,
                    None if frame_receipt is None else frame_receipt.byte_length,
                    observation.screen_state,
                    int(stored_confirmed),
                ),
            )
            inserted = cursor.rowcount == 1
        state = observation.comeback_state
        if (
            inserted
            and observation.map_number is not None
            and observation.game_clock_seconds is not None
            and observation.screen_state == "game"
            and observation.clock_confidence >= 0.9
            and state.is_available
            and state.confidence >= 0.9
            and type(state.radiant_net_worth) is int
            and type(state.dire_net_worth) is int
            and _verified_vision_map_start(
                self.connection,
                raybet_match_id=observation.raybet_match_id,
                map_number=observation.map_number,
                captured_at=observation.captured_at,
            )
        ):
            try:
                append_live_game_snapshot(
                    self.connection,
                    raybet_match_id=observation.raybet_match_id,
                    map_number=observation.map_number,
                    game_time_seconds=observation.game_clock_seconds,
                    radiant_networth=state.radiant_net_worth,
                    dire_networth=state.dire_net_worth,
                    radiant_kills=state.radiant_kills,
                    dire_kills=state.dire_kills,
                    vision_confidence=min(
                        observation.clock_confidence,
                        state.confidence,
                    ),
                    screenshot_path=observation.source_frame_ref,
                    source="vision",
                    captured_at=observation.captured_at,
                )
            except ValueError:
                pass
        return inserted

    def insert_map_result(self, result: Any, *, strict_mapping_id: int) -> bool:
        if type(strict_mapping_id) is not int or strict_mapping_id <= 0:
            raise ValueError("strict_mapping_id must be a positive integer")
        reconciliation = self.connection.execute(
            """SELECT evidence_ref, raybet_evidence_id, opendota_evidence_id,
                      raybet_evidence_ref, opendota_evidence_ref,
                      raybet_observed_at, opendota_observed_at, first_usable_at
                 FROM settlement_reconciliations
                WHERE raybet_match_id=? AND map_number=?
                  AND strict_mapping_id=? AND dota_match_id=?
                  AND status='confirmed'
                  AND raybet_winner_side=? AND opendota_winner_side=?""",
            (
                result.raybet_match_id,
                result.map_number,
                strict_mapping_id,
                result.dota_match_id,
                result.winner_side,
                result.winner_side,
            ),
        ).fetchone()
        if (
            reconciliation is None
            or str(reconciliation["evidence_ref"] or "") != result.evidence_ref
            or str(reconciliation["first_usable_at"] or "")
            != self._iso(result.settled_at)
        ):
            return False
        cursor = self.execute(
            """INSERT INTO map_results
               (raybet_match_id, map_number, strict_mapping_id, dota_match_id,
                winner_side, team_one_kills, team_two_kills, duration_seconds,
                evidence_ref, reconciliation_ref, raybet_evidence_id,
                opendota_evidence_id, raybet_evidence_ref,
                opendota_evidence_ref, raybet_observed_at,
                opendota_observed_at, first_usable_at, settled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (
                result.raybet_match_id,
                result.map_number,
                strict_mapping_id,
                result.dota_match_id,
                result.winner_side,
                result.team_one_kills,
                result.team_two_kills,
                result.duration_seconds,
                result.evidence_ref,
                reconciliation["evidence_ref"],
                reconciliation["raybet_evidence_id"],
                reconciliation["opendota_evidence_id"],
                reconciliation["raybet_evidence_ref"],
                reconciliation["opendota_evidence_ref"],
                reconciliation["raybet_observed_at"],
                reconciliation["opendota_observed_at"],
                reconciliation["first_usable_at"],
                result.settled_at.isoformat(),
            ),
        )
        return cursor.rowcount == 1

    def _settlement_source_authority_valid(
        self,
        *,
        raybet_match_id: str,
        map_number: int,
        strict_mapping_id: int,
        dota_match_id: int,
        raybet_status: str,
        raybet_winner_side: str | None,
        opendota_winner_side: str,
        raybet_evidence_ref: str,
        opendota_evidence_ref: str,
        opendota_facts: Mapping[str, object],
        raybet_observed: str,
        opendota_observed: str,
        opendota_first_usable: str,
        raybet_audit_key: str,
        raybet_transport_key: str | None,
        raybet_response_state_hash: str | None,
        raybet_response_artifact_hash: str,
        opendota_artifact_id: str,
        opendota_observation_id: str,
        opendota_content_hash: str,
    ) -> bool:
        """Re-read both immutable raw payloads before creating result authority."""

        try:
            mapping = self.connection.execute(
                """SELECT team_one_id, team_two_id, canonical_team_one_id,
                          canonical_team_two_id
                     FROM strict_live_map_mappings
                    WHERE mapping_id=? AND raybet_match_id=? AND map_number=?""",
                (strict_mapping_id, raybet_match_id, map_number),
            ).fetchone()
            if mapping is None or any(
                type(mapping[field]) is not int or int(mapping[field]) <= 0
                for field in (
                    "team_one_id", "team_two_id",
                    "canonical_team_one_id", "canonical_team_two_id",
                )
            ):
                return False
            audit = self.connection.execute(
                """SELECT observed_at, claimed_raybet_match_id,
                          observed_raybet_match_id, disposition, artifact_hash
                     FROM direct_response_audit
                    WHERE audit_key=? AND source='direct'
                      AND response_kind='final_odds'""",
                (raybet_audit_key,),
            ).fetchone()
            if (
                audit is None
                or str(audit["observed_at"]) != raybet_observed
                or str(audit["claimed_raybet_match_id"] or "")
                != raybet_match_id
                or str(audit["observed_raybet_match_id"] or "")
                != raybet_match_id
                or str(audit["disposition"])
                not in {"accepted", "audit_only"}
                or str(audit["artifact_hash"])
                != raybet_response_artifact_hash
            ):
                return False
            raw_response = self.direct_response_payload(raybet_audit_key)
            if not isinstance(raw_response, dict):
                return False
            raw_result = raw_response.get("result")
            if not isinstance(raw_result, dict):
                return False
            from .raybet import parse_raybet_map_final

            parsed_final = parse_raybet_map_final(
                raw_result,
                map_number,
                observed_at=datetime.fromisoformat(raybet_observed),
                expected_match_id=raybet_match_id,
                expected_team_ids=(
                    int(mapping["team_one_id"]),
                    int(mapping["team_two_id"]),
                ),
            )
            if (
                parsed_final.status != raybet_status
                or parsed_final.winner_side != raybet_winner_side
                or parsed_final.evidence_ref != raybet_evidence_ref
            ):
                return False
            if raybet_transport_key is not None:
                transport = self.connection.execute(
                    """SELECT response_state_hash, response_artifact_hash,
                              observed_at, raybet_match_id, source,
                              normalized_state_hash_version,
                              original_legacy_normalized_state_hash,
                              processing_status
                         FROM odds_transport_observations
                        WHERE observation_key=?""",
                    (raybet_transport_key,),
                ).fetchone()
                if (
                    transport is None
                    or transport["response_state_hash"]
                    != raybet_response_state_hash
                    or transport["response_artifact_hash"]
                    != raybet_response_artifact_hash
                    or str(transport["observed_at"]) != raybet_observed
                    or str(transport["raybet_match_id"]) != raybet_match_id
                    or str(transport["source"]) != "direct"
                    or int(transport["normalized_state_hash_version"]) != 2
                    or transport["original_legacy_normalized_state_hash"]
                    is not None
                    or str(transport["processing_status"]) != "processed"
                ):
                    return False

            source = self.connection.execute(
                """SELECT observation.source, observation.artifact_use,
                          observation.endpoint,
                          observation.sanitized_request_identity,
                          observation.match_id, observation.content_hash,
                          observation.received_at,
                          observation.first_usable_at,
                          artifact.storage_path, artifact.content_hash
                              AS artifact_content_hash,
                          artifact.source AS artifact_source,
                          artifact.artifact_use AS artifact_use,
                          artifact.match_id AS artifact_match_id,
                          artifact.first_usable_at AS artifact_first_usable_at
                     FROM raw_source_observations AS observation
                     JOIN raw_source_artifacts AS artifact
                       ON artifact.artifact_id=observation.artifact_id
                    WHERE observation.observation_id=?
                      AND observation.artifact_id=?""",
                (opendota_observation_id, opendota_artifact_id),
            ).fetchone()
            if (
                source is None
                or str(source["source"]) != "opendota"
                or str(source["artifact_source"]) != "opendota"
                or str(source["artifact_use"]) != "primary"
                or int(source["match_id"]) != dota_match_id
                or int(source["artifact_match_id"]) != dota_match_id
                or str(source["content_hash"]) != opendota_content_hash
                or str(source["artifact_content_hash"])
                != opendota_content_hash
                or str(source["received_at"]) != opendota_observed
                or str(source["first_usable_at"]) != opendota_first_usable
                or str(source["artifact_first_usable_at"])
                != opendota_first_usable
                or str(source["endpoint"]) != f"/api/matches/{dota_match_id}"
                or str(source["sanitized_request_identity"])
                != f"/api/matches/{dota_match_id}"
            ):
                return False
            source_path = Path(str(source["storage_path"]))
            if not source_path.is_absolute():
                return False
            RawArchive._verify(source_path, opendota_content_hash)
            raw_detail = json.loads(gzip.decompress(source_path.read_bytes()))
            if not isinstance(raw_detail, dict):
                return False
            if (
                type(raw_detail.get("match_id")) is not int
                or int(raw_detail["match_id"]) != dota_match_id
                or opendota_evidence_ref
                != f"opendota:{dota_match_id}:sha256:{opendota_content_hash}"
            ):
                return False
            radiant_team_id = raw_detail.get("radiant_team_id")
            dire_team_id = raw_detail.get("dire_team_id")
            radiant_win = raw_detail.get("radiant_win")
            if (
                type(radiant_team_id) is not int
                or type(dire_team_id) is not int
                or type(radiant_win) not in {bool, int}
                or radiant_win not in {True, False, 0, 1}
                or {radiant_team_id, dire_team_id}
                != {
                    int(mapping["canonical_team_one_id"]),
                    int(mapping["canonical_team_two_id"]),
                }
            ):
                return False
            radiant_side = (
                "team_one"
                if radiant_team_id == int(mapping["canonical_team_one_id"])
                else "team_two"
            )
            raw_winner_side = (
                radiant_side
                if bool(radiant_win)
                else ("team_two" if radiant_side == "team_one" else "team_one")
            )
            score_by_side = {
                radiant_side: raw_detail.get("radiant_score"),
                "team_two" if radiant_side == "team_one" else "team_one": (
                    raw_detail.get("dire_score")
                ),
            }
            if (
                raw_winner_side != opendota_winner_side
                or score_by_side["team_one"]
                != opendota_facts.get("team_one_kills")
                or score_by_side["team_two"]
                != opendota_facts.get("team_two_kills")
                or raw_detail.get("duration")
                != opendota_facts.get("duration_seconds")
            ):
                return False
        except (
            KeyError,
            OSError,
            EOFError,
            RuntimeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            SQLAlchemyError,
        ):
            return False
        return True

    def record_settlement_reconciliation(
        self,
        *,
        raybet_match_id: str,
        map_number: int,
        strict_mapping_id: int,
        dota_match_id: int,
        raybet_status: str,
        raybet_winner_side: str | None,
        opendota_winner_side: str,
        raybet_evidence_ref: str,
        opendota_evidence_ref: str,
        raybet_facts: Mapping[str, object],
        opendota_facts: Mapping[str, object],
        status: str,
        reason: str,
        raybet_observed_at: datetime,
        opendota_observed_at: datetime,
        opendota_first_usable_at: datetime,
        raybet_audit_key: str | None,
        raybet_transport_key: str | None,
        raybet_response_state_hash: str | None,
        raybet_response_artifact_hash: str | None,
        opendota_artifact_id: str | None,
        opendota_observation_id: str | None,
        opendota_content_hash: str | None,
    ) -> DatabaseRow:
        """Persist both source facts and a sticky fail-closed resolution."""
        if type(strict_mapping_id) is not int or strict_mapping_id <= 0:
            raise ValueError("strict_mapping_id must be a positive integer")
        for field, value in (
            ("raybet_observed_at", raybet_observed_at),
            ("opendota_observed_at", opendota_observed_at),
            ("opendota_first_usable_at", opendota_first_usable_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field} must be timezone-aware")
        raybet_observed_at = raybet_observed_at.astimezone(timezone.utc)
        opendota_observed_at = opendota_observed_at.astimezone(timezone.utc)
        opendota_first_usable_at = opendota_first_usable_at.astimezone(
            timezone.utc
        )
        if opendota_first_usable_at < opendota_observed_at:
            raise ValueError(
                "OpenDota first usable time cannot precede its observation"
            )
        first_usable_at = max(raybet_observed_at, opendota_first_usable_at)
        raybet_observed = self._iso(raybet_observed_at)
        opendota_observed = self._iso(opendota_observed_at)
        opendota_first_usable = self._iso(opendota_first_usable_at)
        first_usable = self._iso(first_usable_at)
        reconciliation_ref = (
            f"settlement-reconciliation:{raybet_match_id}:map:{map_number}"
        )
        expected_identity = {
            "raybet_match_id": raybet_match_id,
            "map_number": map_number,
            "strict_mapping_id": strict_mapping_id,
            "dota_match_id": dota_match_id,
        }
        raybet_facts_map = dict(raybet_facts)
        opendota_facts_map = dict(opendota_facts)
        raybet_authority_facts = {
            **expected_identity,
            "winner_side": raybet_winner_side,
            "observed_at": raybet_observed,
            "audit_key": raybet_audit_key,
            "transport_key": raybet_transport_key,
            "response_state_hash": raybet_response_state_hash,
            "response_artifact_hash": raybet_response_artifact_hash,
        }
        opendota_authority_facts = {
            **expected_identity,
            "winner_side": opendota_winner_side,
            "observed_at": opendota_observed,
            "first_usable_at": opendota_first_usable,
            "artifact_id": opendota_artifact_id,
            "observation_id": opendota_observation_id,
            "content_hash": opendota_content_hash,
        }
        raybet_identity_conflict = any(
            key in raybet_facts_map and raybet_facts_map[key] != value
            for key, value in raybet_authority_facts.items()
        ) or (
            "winner_side" in raybet_facts_map
            and raybet_facts_map["winner_side"] != raybet_winner_side
        )
        opendota_identity_conflict = any(
            key in opendota_facts_map and opendota_facts_map[key] != value
            for key, value in opendota_authority_facts.items()
        ) or (
            "winner_side" in opendota_facts_map
            and opendota_facts_map["winner_side"] != opendota_winner_side
        )
        facts_identity_conflict = (
            raybet_identity_conflict or opendota_identity_conflict
        )
        for key, value in raybet_authority_facts.items():
            raybet_facts_map.setdefault(key, value)
        for key, value in opendota_authority_facts.items():
            opendota_facts_map.setdefault(key, value)
        raybet_facts_json = self.json(raybet_facts_map)
        opendota_facts_json = self.json(opendota_facts_map)
        transport_refs = (
            raybet_transport_key,
            raybet_response_state_hash,
        )
        transport_refs_complete = all(transport_refs) or not any(transport_refs)
        source_authority_supplied = bool(
            raybet_audit_key
            and raybet_response_artifact_hash
            and transport_refs_complete
            and opendota_artifact_id
            and opendota_observation_id
            and opendota_content_hash
        )
        source_authority_complete = source_authority_supplied
        if source_authority_complete:
            source_authority_complete = self._settlement_source_authority_valid(
                raybet_match_id=raybet_match_id,
                map_number=map_number,
                strict_mapping_id=strict_mapping_id,
                dota_match_id=dota_match_id,
                raybet_status=raybet_status,
                raybet_winner_side=raybet_winner_side,
                opendota_winner_side=opendota_winner_side,
                raybet_evidence_ref=raybet_evidence_ref,
                opendota_evidence_ref=opendota_evidence_ref,
                opendota_facts=opendota_facts_map,
                raybet_observed=raybet_observed,
                opendota_observed=opendota_observed,
                opendota_first_usable=opendota_first_usable,
                raybet_audit_key=str(raybet_audit_key),
                raybet_transport_key=raybet_transport_key,
                raybet_response_state_hash=raybet_response_state_hash,
                raybet_response_artifact_hash=str(
                    raybet_response_artifact_hash
                ),
                opendota_artifact_id=str(opendota_artifact_id),
                opendota_observation_id=str(opendota_observation_id),
                opendota_content_hash=str(opendota_content_hash),
            )
        if status != "manual_review" and not source_authority_complete:
            status, reason = (
                "manual_review",
                (
                    "source_authority_invalid"
                    if source_authority_supplied
                    else "source_authority_missing"
                ),
            )
        with self.transaction():
            existing = self.connection.execute(
                """SELECT * FROM settlement_reconciliations
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            ).fetchone()
            mapping_authority = self.connection.execute(
                """SELECT 1 FROM strict_live_map_mappings
                    WHERE mapping_id=? AND raybet_match_id=? AND map_number=?""",
                (strict_mapping_id, raybet_match_id, map_number),
            ).fetchone()
            if mapping_authority is None:
                raise ValueError("strict mapping does not match the reconciliation")
            if existing is not None:
                existing_mapping_id = existing["strict_mapping_id"]
                if (
                    type(existing_mapping_id) is not int
                    or int(existing_mapping_id) != strict_mapping_id
                ):
                    self.connection.execute(
                        """UPDATE settlement_reconciliations
                              SET status='manual_review',
                                  reason='mapping_lineage_conflict',
                                  updated_at=?
                            WHERE raybet_match_id=? AND map_number=?""",
                        (first_usable, raybet_match_id, map_number),
                    )
                    row = self.connection.execute(
                        """SELECT * FROM settlement_reconciliations
                            WHERE raybet_match_id=? AND map_number=?""",
                        (raybet_match_id, map_number),
                    ).fetchone()
                    assert row is not None
                    return row
            if status != "manual_review" and facts_identity_conflict:
                status, reason = "manual_review", "source_facts_identity_conflict"
            evidence_ids: dict[str, int] = {}
            if source_authority_complete:
                evidence_rows = (
                    (
                        "raybet", raybet_status, raybet_winner_side,
                        raybet_evidence_ref, raybet_facts_json,
                        raybet_observed, raybet_observed,
                        raybet_audit_key, raybet_transport_key,
                        raybet_response_state_hash,
                        raybet_response_artifact_hash,
                        None, None, None,
                    ),
                    (
                        "opendota", "confirmed", opendota_winner_side,
                        opendota_evidence_ref, opendota_facts_json,
                        opendota_observed, opendota_first_usable,
                        None, None, None, None,
                        opendota_artifact_id, opendota_observation_id,
                        opendota_content_hash,
                    ),
                )
                try:
                    with self.transaction():
                        for values in evidence_rows:
                            self.connection.execute(
                                """INSERT INTO settlement_result_evidence
                                   (raybet_match_id, map_number, dota_match_id,
                                    source, status, winner_side, evidence_ref,
                                    facts_json, observed_at, first_usable_at,
                                    raybet_audit_key, raybet_transport_key,
                                    raybet_response_state_hash,
                                    raybet_response_artifact_hash,
                                    opendota_artifact_id,
                                    opendota_observation_id,
                                    opendota_content_hash)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                           ?, ?, ?, ?)
                                   ON CONFLICT DO NOTHING""",
                                (raybet_match_id, map_number, dota_match_id, *values),
                            )
                            source = str(values[0])
                            evidence_ref = str(values[3])
                            persisted = self.connection.execute(
                                """SELECT evidence_id, status, winner_side,
                                          facts_json, observed_at, first_usable_at,
                                          raybet_audit_key, raybet_transport_key,
                                          raybet_response_state_hash,
                                          raybet_response_artifact_hash,
                                          opendota_artifact_id,
                                          opendota_observation_id,
                                          opendota_content_hash
                                     FROM settlement_result_evidence
                                    WHERE raybet_match_id=? AND map_number=?
                                      AND source=? AND evidence_ref=?""",
                                (
                                    raybet_match_id, map_number, source,
                                    evidence_ref,
                                ),
                            ).fetchone()
                            if persisted is None or tuple(persisted)[1:] != (
                                values[1], values[2], values[4], *values[5:]
                            ):
                                raise ValueError(
                                    "settlement evidence reference was reused"
                                )
                            evidence_ids[source] = int(persisted["evidence_id"])
                except (IntegrityError, ValueError):
                    evidence_ids.clear()
                    source_authority_complete = False
                    status, reason = "manual_review", "source_authority_invalid"

            linked_elsewhere = self.connection.execute(
                """SELECT raybet_match_id, map_number
                     FROM settlement_reconciliations
                    WHERE dota_match_id=?
                      AND (raybet_match_id!=? OR map_number!=?)
                    UNION
                   SELECT raybet_match_id, map_number
                     FROM map_results
                    WHERE dota_match_id=?
                      AND (raybet_match_id!=? OR map_number!=?)""",
                (
                    dota_match_id,
                    raybet_match_id,
                    map_number,
                    dota_match_id,
                    raybet_match_id,
                    map_number,
                ),
            ).fetchall()
            link_conflict = bool(linked_elsewhere)
            if link_conflict:
                self.connection.execute(
                    """UPDATE settlement_reconciliations
                          SET status='manual_review',
                              reason=CASE
                                WHEN status='manual_review' THEN reason
                                ELSE 'opendota_match_link_conflict'
                              END,
                              updated_at=?
                        WHERE dota_match_id=?
                          AND (raybet_match_id!=? OR map_number!=?)""",
                    (first_usable, dota_match_id, raybet_match_id, map_number),
                )

            effective_status = "manual_review" if link_conflict else status
            effective_reason = (
                "opendota_match_link_conflict" if link_conflict else reason
            )
            effective_dota_match_id = dota_match_id
            effective_raybet_winner = raybet_winner_side
            effective_opendota_winner = opendota_winner_side
            effective_raybet_ref = raybet_evidence_ref
            effective_opendota_ref = opendota_evidence_ref
            effective_raybet_evidence_id = evidence_ids.get("raybet")
            effective_opendota_evidence_id = evidence_ids.get("opendota")
            effective_raybet_observed = raybet_observed
            effective_opendota_observed = opendota_observed
            effective_first_usable = first_usable
            effective_first_observed = first_usable
            effective_updated = first_usable
            if existing is not None and existing["status"] == "manual_review":
                effective_status = "manual_review"
                effective_reason = str(existing["reason"])
                effective_dota_match_id = int(existing["dota_match_id"])
                effective_raybet_winner = existing["raybet_winner_side"]
                effective_opendota_winner = str(existing["opendota_winner_side"])
                effective_raybet_ref = str(existing["raybet_evidence_ref"])
                effective_opendota_ref = str(existing["opendota_evidence_ref"])
                effective_raybet_evidence_id = existing["raybet_evidence_id"]
                effective_opendota_evidence_id = existing["opendota_evidence_id"]
                effective_raybet_observed = existing["raybet_observed_at"]
                effective_opendota_observed = existing["opendota_observed_at"]
                effective_first_usable = existing["first_usable_at"]
                effective_first_observed = existing["first_observed_at"]
            elif existing is not None and existing["status"] == "confirmed":
                candidate_identity = (
                    dota_match_id, raybet_winner_side, opendota_winner_side,
                    raybet_evidence_ref, opendota_evidence_ref,
                    effective_raybet_evidence_id,
                    effective_opendota_evidence_id,
                    raybet_observed, opendota_observed, first_usable,
                )
                existing_identity = (
                    int(existing["dota_match_id"]),
                    existing["raybet_winner_side"],
                    str(existing["opendota_winner_side"]),
                    str(existing["raybet_evidence_ref"]),
                    str(existing["opendota_evidence_ref"]),
                    existing["raybet_evidence_id"],
                    existing["opendota_evidence_id"],
                    existing["raybet_observed_at"],
                    existing["opendota_observed_at"],
                    existing["first_usable_at"],
                )
                if effective_status != "confirmed" or candidate_identity != existing_identity:
                    effective_status = "manual_review"
                    effective_reason = (
                        "opendota_match_link_conflict"
                        if link_conflict
                        else (
                            reason
                            if reason in {
                                "stored_map_result_conflict",
                                "map_result_persistence_conflict",
                            }
                            else "source_result_changed"
                        )
                    )
                else:
                    effective_reason = str(existing["reason"])
                effective_dota_match_id = int(existing["dota_match_id"])
                effective_raybet_winner = existing["raybet_winner_side"]
                effective_opendota_winner = str(existing["opendota_winner_side"])
                effective_raybet_ref = str(existing["raybet_evidence_ref"])
                effective_opendota_ref = str(existing["opendota_evidence_ref"])
                effective_raybet_evidence_id = existing["raybet_evidence_id"]
                effective_opendota_evidence_id = existing["opendota_evidence_id"]
                effective_raybet_observed = existing["raybet_observed_at"]
                effective_opendota_observed = existing["opendota_observed_at"]
                effective_first_usable = existing["first_usable_at"]
                effective_first_observed = existing["first_observed_at"]
                effective_updated = (
                    str(existing["updated_at"])
                    if effective_status == "confirmed"
                    else first_usable
                )

            self.connection.execute(
                """INSERT INTO settlement_reconciliations
                   (raybet_match_id, map_number, strict_mapping_id, dota_match_id,
                    raybet_winner_side, opendota_winner_side,
                    raybet_evidence_ref, opendota_evidence_ref, evidence_ref,
                    raybet_evidence_id, opendota_evidence_id,
                    raybet_observed_at, opendota_observed_at, first_usable_at,
                    status, reason, first_observed_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(raybet_match_id, map_number) DO UPDATE SET
                     dota_match_id=excluded.dota_match_id,
                     raybet_winner_side=excluded.raybet_winner_side,
                     opendota_winner_side=excluded.opendota_winner_side,
                     raybet_evidence_ref=excluded.raybet_evidence_ref,
                     opendota_evidence_ref=excluded.opendota_evidence_ref,
                     evidence_ref=excluded.evidence_ref,
                     raybet_evidence_id=excluded.raybet_evidence_id,
                     opendota_evidence_id=excluded.opendota_evidence_id,
                     raybet_observed_at=excluded.raybet_observed_at,
                     opendota_observed_at=excluded.opendota_observed_at,
                     first_usable_at=excluded.first_usable_at,
                     status=excluded.status,
                     reason=excluded.reason,
                     updated_at=excluded.updated_at""",
                (
                    raybet_match_id,
                    map_number,
                    strict_mapping_id,
                    effective_dota_match_id,
                    effective_raybet_winner,
                    effective_opendota_winner,
                    effective_raybet_ref,
                    effective_opendota_ref,
                    reconciliation_ref,
                    effective_raybet_evidence_id,
                    effective_opendota_evidence_id,
                    effective_raybet_observed,
                    effective_opendota_observed,
                    effective_first_usable,
                    effective_status,
                    effective_reason,
                    effective_first_observed,
                    effective_updated,
                ),
            )
            row = self.connection.execute(
                """SELECT * FROM settlement_reconciliations
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            ).fetchone()
            assert row is not None
            return row
