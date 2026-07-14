"""Emit versioned visual observations from a RayBet Dota HLS stream."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.live_observation import LiveObservation  # noqa: E402
from vision.clock_reader import ClockReader  # noqa: E402
from vision.hero_recognizer import (  # noqa: E402
    DraftReading,
    DraftTracker,
    HeroRecognizer,
)
from vision.map_state import ConfirmedClock, MapStateTracker  # noqa: E402
from vision.observation_writer import ObservationWriter  # noqa: E402
from vision.screen_state import classify_screen_state  # noqa: E402
from vision.stream_capture import HLSStreamCapture  # noqa: E402
from vision.team_side import TeamSideRecognizer, TeamSideTracker  # noqa: E402


MAP_RE = re.compile(r"map_(\d+)")
OPEN_STATUSES = ("1", "5", "open", "active", "running")
COMPLETION_CHECK_INTERVAL = 15
LIVE_BETTING_DATA = ROOT / "data" / "live_betting"
DEFAULT_OBSERVATION_DIR = LIVE_BETTING_DATA / "live_observations"
DEFAULT_EVIDENCE_DIR = LIVE_BETTING_DATA / "live_evidence"
DEFAULT_FEATURES = ROOT / "vision" / "templates" / "hero_features.npz"


def _manual_map_number(payload: object, best_of: int | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    indexes: set[int] = set()
    for team in payload.get("team") or []:
        if not isinstance(team, dict):
            continue
        score = team.get("score")
        manual = score.get("manualControlData") if isinstance(score, dict) else None
        value = manual.get("currentIndex") if isinstance(manual, dict) else None
        if value is None or isinstance(value, bool):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if str(parsed) != str(value).strip() or parsed < 1 or parsed > 10:
            continue
        indexes.add(parsed)
    if not indexes:
        return None
    if len(indexes) != 1:
        raise ValueError("RayBet manualControlData has conflicting map indexes")
    map_number = indexes.pop()
    if best_of is not None and map_number > best_of:
        raise ValueError("RayBet current map exceeds the configured series length")
    return map_number


def _unique_open_map(connection: sqlite3.Connection, match_id: str) -> int | None:
    rows = connection.execute(
        """WITH latest AS (
               SELECT odds_id, period, status,
                      ROW_NUMBER() OVER (
                          PARTITION BY odds_id
                          ORDER BY received_at DESC, id DESC
                      ) AS rank
                 FROM odds_snapshots
                WHERE raybet_match_id=? AND market_type='winner'
           )
           SELECT DISTINCT period FROM latest
            WHERE rank=1 AND status IN (?, ?, ?, ?, ?)""",
        (match_id, *OPEN_STATUSES),
    ).fetchall()
    maps = {
        int(match.group(1))
        for row in rows
        if (match := MAP_RE.fullmatch(str(row[0]))) is not None
    }
    return maps.pop() if len(maps) == 1 else None


def match_source(
    database: Path, match_id: str, map_override: int | None = None
) -> tuple[str, int]:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT live_url, raw_json, best_of FROM raybet_matches "
            "WHERE raybet_match_id=?",
            (match_id,),
        ).fetchone()
        if not row or not row[0]:
            raise ValueError(f"no live_url found for RayBet match {match_id}")
        try:
            payload = json.loads(str(row[1] or "{}"))
        except (TypeError, ValueError):
            payload = {}
        best_of = int(row[2]) if row[2] is not None else None
        if map_override is not None:
            if map_override < 1 or map_override > 10:
                raise ValueError("map override must be between 1 and 10")
            if best_of is not None and map_override > best_of:
                raise ValueError("map override exceeds the configured series length")
            return str(row[0]), map_override
        map_number = _manual_map_number(payload, best_of)
        if map_number is None:
            map_number = _unique_open_map(connection, match_id)
        if map_number is None or (best_of is not None and map_number > best_of):
            raise ValueError(
                f"cannot determine a unique current map for RayBet match {match_id}"
            )
        return str(row[0]), map_number
    finally:
        connection.close()


def resolve_source(
    *,
    url: str | None,
    database: Path | None,
    match_id: str,
    map_number: int | None,
) -> tuple[str, int]:
    if url:
        if map_number is None:
            raise ValueError("--map-number is required with --url")
        if map_number < 1 or map_number > 10:
            raise ValueError("map number must be between 1 and 10")
        return url, map_number
    if database:
        return match_source(database, match_id, map_override=map_number)
    raise ValueError("provide --url or --database")


def match_is_complete(database: Path, match_id: str) -> bool:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT status FROM raybet_matches WHERE raybet_match_id=?", (match_id,)
        ).fetchone()
        return bool(row and str(row[0]) == "2")
    finally:
        connection.close()


def completion_check_due(sample_count: int) -> bool:
    return sample_count > 0 and sample_count % COMPLETION_CHECK_INTERVAL == 0


def _meaningful(previous: LiveObservation | None, current: LiveObservation) -> bool:
    if previous is None or previous.screen_state != current.screen_state:
        return True
    if current.is_confirmed and not previous.is_confirmed:
        return True
    if current.map_number != previous.map_number:
        return True
    if (
        current.game_clock_seconds is not None
        and previous.game_clock_seconds is not None
    ):
        return abs(current.game_clock_seconds - previous.game_clock_seconds) >= 5
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--url")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--map-number", type=int)
    parser.add_argument("--radiant-side", choices=("team_one", "team_two"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--features", type=Path, default=DEFAULT_FEATURES)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--count", type=int)
    parser.add_argument("--evidence-interval", type=float, default=30.0)
    args = parser.parse_args()

    try:
        url, map_number = resolve_source(
            url=args.url,
            database=args.database,
            match_id=args.match_id,
            map_number=args.map_number,
        )
    except ValueError as error:
        parser.error(str(error))
    output = args.output or DEFAULT_OBSERVATION_DIR / f"{args.match_id}.jsonl"
    evidence_dir = args.evidence_dir or DEFAULT_EVIDENCE_DIR / args.match_id

    clock_reader = ClockReader()
    clock_tracker = MapStateTracker()
    clock_tracker.reset_map(map_number)
    hero_reader = HeroRecognizer(args.features)
    draft_tracker = DraftTracker()
    side_reader = None
    if args.database and not args.radiant_side:
        side_reader = TeamSideRecognizer.from_database(args.database, args.match_id)
    side_tracker = TeamSideTracker()
    writer = ObservationWriter(output)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    last_clock: ConfirmedClock | None = None
    last_draft: DraftReading | None = None
    previous: LiveObservation | None = None
    outside_game_frames = 0
    radiant_team_side = args.radiant_side
    last_evidence_at = 0.0
    sample_count = 0

    with HLSStreamCapture(url) as capture:
        for frame in capture.sample(interval=args.interval, count=args.count):
            sample_count += 1
            if (
                args.database
                and completion_check_due(sample_count)
                and match_is_complete(args.database, args.match_id)
            ):
                break
            state, _ = classify_screen_state(frame.image)
            if state == "game":
                raw_clock = clock_reader.read(frame.image)
                if (
                    outside_game_frames >= 5
                    and last_clock is not None
                    and last_clock.seconds > 300
                    and raw_clock.seconds is not None
                    and raw_clock.confidence >= clock_tracker.min_confidence
                    and raw_clock.seconds <= 180
                ):
                    map_number += 1
                    clock_tracker.reset_map(map_number)
                    draft_tracker.reset()
                    last_clock = None
                    last_draft = None
                    radiant_team_side = None
                    side_tracker.reset()
                outside_game_frames = 0
                confirmed_clock = clock_tracker.update(raw_clock)
                confirmed_draft = None
                if last_draft is None:
                    confirmed_draft = draft_tracker.update(
                        hero_reader.read(frame.image)
                    )
                last_clock = confirmed_clock or last_clock
                last_draft = confirmed_draft or last_draft
                if radiant_team_side is None and side_reader is not None:
                    side = side_tracker.update(side_reader.read(frame.image))
                    if side is not None:
                        radiant_team_side = side.radiant_team_side
            else:
                outside_game_frames += 1
            captured = datetime.fromtimestamp(frame.captured_at, timezone.utc)
            frame_name = f"{args.match_id}_{captured.strftime('%Y%m%dT%H%M%S_%fZ')}.jpg"
            frame_path = evidence_dir / frame_name
            observation = LiveObservation(
                raybet_match_id=args.match_id,
                map_number=map_number if state == "game" else None,
                captured_at_utc=captured,
                game_clock_seconds=(
                    last_clock.seconds if state == "game" and last_clock else None
                ),
                is_paused=(
                    last_clock.is_paused if state == "game" and last_clock else None
                ),
                radiant_hero_ids=(
                    list(last_draft.radiant_hero_ids) if last_draft else []
                ),
                dire_hero_ids=(list(last_draft.dire_hero_ids) if last_draft else []),
                radiant_team_side=radiant_team_side,
                clock_confidence=(
                    last_clock.confidence if state == "game" and last_clock else 0.0
                ),
                draft_confidence=last_draft.confidence if last_draft else 0.0,
                source_frame_ref=f"stream:{frame.source_hash}:{frame.sequence}",
                screen_state=state,
            )
            if _meaningful(previous, observation):
                important_change = (
                    previous is None
                    or previous.screen_state != observation.screen_state
                    or (observation.is_confirmed and not previous.is_confirmed)
                    or observation.map_number != previous.map_number
                )
                if (
                    important_change
                    or frame.captured_at - last_evidence_at >= args.evidence_interval
                ):
                    cv2.imwrite(
                        str(frame_path), frame.image, [cv2.IMWRITE_JPEG_QUALITY, 85]
                    )
                    observation.source_frame_ref = str(frame_path.resolve())
                    last_evidence_at = frame.captured_at
                writer.append(observation)
                print(observation.model_dump_json())
                previous = observation
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
