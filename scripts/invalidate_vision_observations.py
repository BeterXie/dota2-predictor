"""Audit and invalidate a proven-bad range of confirmed vision observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_betting.service_coordination import (  # noqa: E402
    add_single_database_argument,
    database_writer_authority,
)
from live_betting.database_protocol import online_backup  # noqa: E402
from live_betting.storage import LiveBettingStore  # noqa: E402
from live_betting.vision_frame_registry import (  # noqa: E402
    verify_registered_vision_frame,
)
from shared.sqlite import connect as connect_sqlite  # noqa: E402


UTC = timezone.utc


def _aware_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _online_backup(database: Path, destination: Path) -> None:
    online_backup(database, destination)


def _earliest_captured_at(rows: list[sqlite3.Row]) -> str | None:
    """Return the earliest valid event-time cutoff for selected frames.

    SQLite compares timestamp text lexically in the invalidation queries.  The
    watcher normally stores canonical UTC ISO values, but an operator may be
    repairing an older database with mixed offsets or malformed data.  A bad
    timestamp must fail closed, so ``None`` deliberately asks the dependency
    invalidator to treat every same-map dependent as affected.
    """
    parsed: list[datetime] = []
    for row in rows:
        value = str(row["captured_at"])
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return None
        parsed.append(timestamp.astimezone(UTC))
    if not parsed:
        return None
    return min(parsed).isoformat()


def invalidate(
    database: Path,
    *,
    match_id: str,
    map_number: int,
    clock_seconds: int | None,
    after: str,
    reason: str,
    backup: Path,
    dry_run: bool = False,
    reuse_backup: bool = False,
) -> int:
    database = database.resolve()
    rows: list[sqlite3.Row]
    with LiveBettingStore(database) as store:
        clock_filter = (
            "AND game_clock_seconds=?" if clock_seconds is not None else ""
        )
        parameters: tuple[object, ...] = (match_id, map_number)
        if clock_seconds is not None:
            parameters += (clock_seconds,)
        parameters += (after,)
        rows = store.connection.execute(
            f"""SELECT raybet_match_id, captured_at, source_frame_ref
                  FROM vision_observations
                 WHERE raybet_match_id=? AND map_number=?
                   {clock_filter} AND captured_at>? AND confirmed=1
                 ORDER BY captured_at, source_frame_ref""",
            parameters,
        ).fetchall()
    if dry_run or not rows:
        return len(rows)

    backup = backup.resolve()
    if reuse_backup:
        if not backup.is_file():
            raise ValueError("reused backup does not exist")
        verification = connect_sqlite(backup, read_only=True)
        try:
            if verification.execute(
                "SELECT COUNT(*) FROM sqlite_master"
            ).fetchone()[0] <= 0:
                raise RuntimeError("reused backup has no schema")
        finally:
            verification.close()
    else:
        _online_backup(database, backup)
    invalidated_at = datetime.now(UTC).isoformat()
    with LiveBettingStore(database) as store:
        store.init_schema()
        with store.transaction():
            for row in rows:
                key = (
                    str(row["raybet_match_id"]),
                    str(row["captured_at"]),
                    str(row["source_frame_ref"]),
                )
                store.connection.execute(
                    """INSERT INTO vision_observation_invalidations
                       (raybet_match_id, captured_at, source_frame_ref,
                        invalidated_at, reason)
                       VALUES (?, ?, ?, ?, ?)""",
                    (*key, invalidated_at, reason),
                )
                cursor = store.connection.execute(
                    """UPDATE vision_observations SET confirmed=0
                        WHERE raybet_match_id=? AND captured_at=?
                          AND source_frame_ref=? AND confirmed=1""",
                    key,
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("vision observation changed during invalidation")
            # A confirmed frame is a signal-creation input, not just display
            # data.  Propagate this audit invalidation through every dependent
            # lineage using the same event-time, fail-closed path as a draft
            # conflict.  The cutoff is the earliest selected frame, so outputs
            # before the bad evidence remain reproducible and valid.
            store._invalidate_vision_dependents(
                match_id,
                map_number,
                reason,
                _earliest_captured_at(rows),
                block_reason="vision_observation_invalidated",
                block_actor="vision_invalidation",
            )
    return len(rows)


def freeze_draft_map(
    database: Path,
    *,
    match_id: str,
    map_number: int,
    reason: str,
) -> int:
    recorded_at = datetime.now(UTC).isoformat()
    with LiveBettingStore(database.resolve()) as store:
        store.init_schema()
        rows = store.connection.execute(
            """SELECT observation.captured_at, observation.source_frame_ref,
                      observation.radiant_hero_ids, observation.dire_hero_ids,
                      observation.radiant_team_side,
                      observation.source_frame_sha256,
                      observation.source_frame_bytes
                 FROM trusted_vision_observation_authority AS observation
                WHERE observation.raybet_match_id=?
                  AND observation.map_number=?
                  AND observation.confirmed=1
                  AND NOT EXISTS (
                      SELECT 1
                        FROM vision_observation_invalidations AS invalidation
                       WHERE invalidation.raybet_match_id=observation.raybet_match_id
                         AND invalidation.captured_at=observation.captured_at
                         AND invalidation.source_frame_ref=observation.source_frame_ref
                  )""",
            (match_id, map_number),
        ).fetchall()
        valid: list[tuple[sqlite3.Row, str, datetime]] = []
        for row in rows:
            try:
                verify_registered_vision_frame(
                    store.connection,
                    str(row["source_frame_ref"]),
                    expected_sha256=str(row["source_frame_sha256"]),
                    expected_bytes=int(row["source_frame_bytes"]),
                )
                radiant = json.loads(str(row["radiant_hero_ids"]))
                dire = json.loads(str(row["dire_hero_ids"]))
                captured_at = datetime.fromisoformat(
                    str(row["captured_at"]).replace("Z", "+00:00")
                )
            except (RuntimeError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(radiant, list) or not isinstance(dire, list):
                continue
            heroes = radiant + dire
            if (
                captured_at.tzinfo is None
                or captured_at.utcoffset() is None
                or not str(row["source_frame_ref"]).strip()
                or row["radiant_team_side"] not in {None, "team_one", "team_two"}
                or len(radiant) != 5
                or len(dire) != 5
                or any(type(hero_id) is not int or hero_id <= 0 for hero_id in heroes)
                or len(set(heroes)) != 10
            ):
                continue
            payload = json.dumps(
                {"radiant": radiant, "dire": dire},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            valid.append(
                (
                    row,
                    hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                    captured_at.astimezone(UTC),
                )
            )
        if not valid:
            raise ValueError("no trusted complete draft observations found for map")

        valid.sort(key=lambda item: (item[2], str(item[0]["source_frame_ref"])))
        first, first_hash, _first_captured_at = valid[0]
        with store.transaction():
            store.connection.execute(
                """INSERT OR IGNORE INTO vision_draft_anchors
                   (raybet_match_id, map_number, draft_hash,
                    radiant_hero_ids, dire_hero_ids, radiant_team_side,
                    team_side_anchored_at, team_side_source_frame_ref,
                    anchored_at, source_frame_ref, status, conflict_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'anchored', NULL)""",
                (
                    match_id,
                    map_number,
                    first_hash,
                    str(first["radiant_hero_ids"]),
                    str(first["dire_hero_ids"]),
                    first["radiant_team_side"],
                    str(first["captured_at"])
                    if first["radiant_team_side"] is not None
                    else None,
                    str(first["source_frame_ref"])
                    if first["radiant_team_side"] is not None
                    else None,
                    str(first["captured_at"]),
                    str(first["source_frame_ref"]),
                ),
            )
            anchor = store.connection.execute(
                """SELECT status FROM vision_draft_anchors
                    WHERE raybet_match_id=? AND map_number=?""",
                (match_id, map_number),
            ).fetchone()
            if anchor["status"] == "anchored":
                store.connection.execute(
                    """UPDATE vision_draft_anchors
                          SET status='conflict', conflict_at=?
                        WHERE raybet_match_id=? AND map_number=?""",
                    (recorded_at, match_id, map_number),
                )
            for row, draft_hash, _captured_at in valid:
                store.connection.execute(
                    """INSERT OR IGNORE INTO vision_draft_conflicts
                       (raybet_match_id, map_number, captured_at,
                        source_frame_ref, observed_draft_hash,
                        radiant_hero_ids, dire_hero_ids,
                        observed_radiant_team_side, reason, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        match_id,
                        map_number,
                        str(row["captured_at"]),
                        str(row["source_frame_ref"]),
                        draft_hash,
                        str(row["radiant_hero_ids"]),
                        str(row["dire_hero_ids"]),
                        row["radiant_team_side"],
                        reason,
                        recorded_at,
                    ),
                )
            # Reuse the same causal invalidation path as live ingestion.  The
            # helper derives the earliest captured conflict cutoff from the
            # append-only conflict rows and also upgrades settlements and
            # blocks unsent notifications for invalid order lineage.
            _has_conflict, conflict_cutoff = store._draft_conflict_state(
                match_id, map_number
            )
            store._invalidate_draft_dependents(
                match_id, map_number, reason, conflict_cutoff
            )
            store.connection.execute(
                "DELETE FROM odds_alignments WHERE raybet_match_id=? AND map_number=?",
                (match_id, map_number),
            )
    return len(valid)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(parser, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--map-number", type=int, required=True)
    parser.add_argument("--clock-seconds", type=int)
    parser.add_argument("--after", type=_aware_timestamp, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--reuse-backup", action="store_true")
    parser.add_argument("--freeze-draft", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with database_writer_authority(args.database):
        count = invalidate(
            args.database,
            match_id=args.match_id,
            map_number=args.map_number,
            clock_seconds=args.clock_seconds,
            after=args.after,
            reason=args.reason,
            backup=args.backup,
            dry_run=args.dry_run,
            reuse_backup=args.reuse_backup,
        )
        draft_rows = (
            freeze_draft_map(
                args.database,
                match_id=args.match_id,
                map_number=args.map_number,
                reason=args.reason,
            )
            if args.freeze_draft and not args.dry_run
            else 0
        )
    print(
        json.dumps(
            {
                "matched": count,
                "draft_rows_frozen": draft_rows,
                "dry_run": args.dry_run,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
