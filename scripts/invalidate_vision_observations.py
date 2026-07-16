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

from live_betting.database_protocol import online_backup  # noqa: E402
from live_betting.storage import LiveBettingStore  # noqa: E402
from shared.sqlite import connect as connect_sqlite  # noqa: E402


UTC = timezone.utc


def _aware_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _online_backup(database: Path, destination: Path) -> None:
    online_backup(database, destination)


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
            """SELECT captured_at, source_frame_ref,
                      radiant_hero_ids, dire_hero_ids
                 FROM vision_observations
                WHERE raybet_match_id=? AND map_number=?
                ORDER BY captured_at, source_frame_ref""",
            (match_id, map_number),
        ).fetchall()
        valid: list[tuple[sqlite3.Row, str]] = []
        for row in rows:
            try:
                radiant = json.loads(str(row["radiant_hero_ids"]))
                dire = json.loads(str(row["dire_hero_ids"]))
            except (TypeError, ValueError):
                continue
            if len(radiant) != 5 or len(dire) != 5 or len(set(radiant + dire)) != 10:
                continue
            payload = json.dumps(
                {"radiant": radiant, "dire": dire},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            valid.append((row, hashlib.sha256(payload.encode("utf-8")).hexdigest()))
        if not valid:
            raise ValueError("no complete draft observations found for map")

        first, first_hash = valid[0]
        with store.transaction():
            store.connection.execute(
                """INSERT OR IGNORE INTO vision_draft_anchors
                   (raybet_match_id, map_number, draft_hash,
                    radiant_hero_ids, dire_hero_ids, anchored_at,
                    source_frame_ref, status, conflict_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'conflict', ?)""",
                (
                    match_id,
                    map_number,
                    first_hash,
                    str(first["radiant_hero_ids"]),
                    str(first["dire_hero_ids"]),
                    str(first["captured_at"]),
                    str(first["source_frame_ref"]),
                    recorded_at,
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
            for row, draft_hash in valid:
                store.connection.execute(
                    """INSERT OR IGNORE INTO vision_draft_conflicts
                       (raybet_match_id, map_number, captured_at,
                        source_frame_ref, observed_draft_hash,
                        radiant_hero_ids, dire_hero_ids, reason, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        match_id,
                        map_number,
                        str(row["captured_at"]),
                        str(row["source_frame_ref"]),
                        draft_hash,
                        str(row["radiant_hero_ids"]),
                        str(row["dire_hero_ids"]),
                        reason,
                        recorded_at,
                    ),
                )
            for dependent_type, table, key_column in (
                ("odds_alignment", "odds_alignments", "odds_snapshot_id"),
                ("strategy_decision", "strategy_decisions", "decision_key"),
                (
                    "research_prediction",
                    "research_live_predictions",
                    "prediction_key",
                ),
                ("shadow_order", "shadow_orders", "order_key"),
            ):
                rows_for_type = store.connection.execute(
                    f"""SELECT {key_column} FROM {table}
                         WHERE raybet_match_id=?
                           AND {('map_number=?' if table != 'shadow_orders' else 'strict_mapping_id IN (SELECT mapping_id FROM strict_live_map_mappings WHERE raybet_match_id=? AND map_number=?)')}""",
                    (
                        (match_id, map_number)
                        if table != "shadow_orders"
                        else (match_id, match_id, map_number)
                    ),
                ).fetchall()
                store.connection.executemany(
                    """INSERT OR IGNORE INTO vision_derived_invalidations
                       (dependent_type, dependent_key, raybet_match_id,
                        map_number, reason, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        (
                            dependent_type,
                            str(dependent[0]),
                            match_id,
                            map_number,
                            reason,
                            recorded_at,
                        )
                        for dependent in rows_for_type
                    ),
                )
            store.connection.execute(
                "DELETE FROM odds_alignments WHERE raybet_match_id=? AND map_number=?",
                (match_id, map_number),
            )
    return len(valid)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "dota2.db")
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
