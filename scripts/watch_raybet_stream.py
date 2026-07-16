"""Emit versioned visual observations from a RayBet Dota HLS stream."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import cv2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.live_observation import LiveObservation  # noqa: E402
from live_betting.raybet_state import (  # noqa: E402
    infer_current_map_number,
    raybet_match_is_live,
)
from live_betting.raybet import RayBetClient  # noqa: E402
from shared.sqlite import connect as connect_sqlite  # noqa: E402
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


COMPLETION_CHECK_INTERVAL = 15
LIVE_BETTING_DATA = ROOT / "data" / "live_betting"
DEFAULT_OBSERVATION_DIR = LIVE_BETTING_DATA / "live_observations"
DEFAULT_EVIDENCE_DIR = LIVE_BETTING_DATA / "live_evidence"
DEFAULT_FEATURES = ROOT / "vision" / "templates" / "hero_features.npz"
ALLOWED_STREAM_HOSTS = frozenset(
    {
        "play.ehome.gg",
        "qplay.ehome.gg",
        "qplay.shyxswl.com",
    }
)


def _validate_stream_url(url: object, *, description: str = "stream URL") -> str:
    """Accept only public RayBet HLS hosts and their signed query strings."""
    if not isinstance(url, str) or not url:
        raise ValueError(f"invalid {description}")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"invalid {description}") from error
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname.casefold() if parsed.hostname is not None else None
    default_port = {"http": 80, "https": 443}.get(scheme)
    if (
        scheme not in {"http", "https"}
        or hostname not in ALLOWED_STREAM_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, default_port}
        or parsed.fragment
    ):
        raise ValueError(f"invalid {description}")
    return url


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


def _fresh_stream_payload(match_id: str) -> tuple[str, dict[str, object]]:
    with RayBetClient() as client:
        response = client.match_odds(match_id)
    result = response.get("result")
    if (
        not isinstance(result, dict)
        or str(result.get("id") or "") != match_id
        or type(result.get("game_id")) is not int
        or int(result["game_id"]) != 151
    ):
        raise ValueError(f"RayBet stream identity mismatch for {match_id}")
    url = result.get("live_url")
    if not isinstance(url, str):
        raise ValueError(f"no fresh live URL found for RayBet match {match_id}")
    _validate_stream_url(url, description=f"fresh live URL for RayBet match {match_id}")
    return url, result


def match_source(
    database: Path,
    match_id: str,
    map_override: int | None = None,
    *,
    refresh_url: bool = False,
) -> tuple[str, int]:
    connection = connect_sqlite(database, read_only=True)
    try:
        row = connection.execute(
            "SELECT live_url, raw_json, best_of, status FROM raybet_matches "
            "WHERE raybet_match_id=?",
            (match_id,),
        ).fetchone()
        if not row or (not row[0] and not refresh_url):
            raise ValueError(f"no live_url found for RayBet match {match_id}")
        if refresh_url:
            url, payload = _fresh_stream_payload(match_id)
        else:
            url = _validate_stream_url(
                row[0], description=f"stored live URL for RayBet match {match_id}"
            )
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
            return url, map_override
        map_number = _manual_map_number(payload, best_of)
        if map_number is None and str(row[3]) == "2":
            try:
                map_number = infer_current_map_number(payload, best_of)
            except ValueError as error:
                raise ValueError(
                    f"cannot determine a unique current map for RayBet match "
                    f"{match_id}: {error}"
                ) from error
        if map_number is None or (best_of is not None and map_number > best_of):
            raise ValueError(
                f"cannot determine a unique current map for RayBet match {match_id}"
            )
        return url, map_number
    finally:
        connection.close()


def resolve_source(
    *,
    url: str | None,
    database: Path | None,
    match_id: str,
    map_number: int | None,
    refresh_url: bool = False,
) -> tuple[str, int]:
    if url is not None:
        if map_number is None:
            raise ValueError("--map-number is required with --url")
        if map_number < 1 or map_number > 10:
            raise ValueError("map number must be between 1 and 10")
        return _validate_stream_url(url, description="explicit stream URL"), map_number
    if database:
        resolved_url, resolved_map = match_source(
            database,
            match_id,
            map_override=map_number,
            refresh_url=refresh_url,
        )
        return _validate_stream_url(resolved_url), resolved_map
    raise ValueError("provide --url or --database")


def match_is_complete(
    database: Path, match_id: str, *, now: datetime | None = None
) -> bool:
    connection = connect_sqlite(database, read_only=True)
    try:
        row = connection.execute(
            "SELECT status, updated_at FROM raybet_matches WHERE raybet_match_id=?",
            (match_id,),
        ).fetchone()
        return not row or not raybet_match_is_live(row[0], row[1], now=now)
    finally:
        connection.close()


def completion_check_due(sample_count: int) -> bool:
    return sample_count > 0 and sample_count % COMPLETION_CHECK_INTERVAL == 0


def _meaningful(previous: LiveObservation | None, current: LiveObservation) -> bool:
    if previous is None or previous.screen_state != current.screen_state:
        return True
    if current.is_confirmed != previous.is_confirmed:
        return True
    if current.is_paused != previous.is_paused:
        return True
    if current.map_number != previous.map_number:
        return True
    if (
        current.game_clock_seconds is not None
        and previous.game_clock_seconds is not None
    ):
        return abs(current.game_clock_seconds - previous.game_clock_seconds) >= 5
    return False


def current_frame_clock_fields(
    confirmed_clock: ConfirmedClock | None,
) -> tuple[int | None, bool | None, float]:
    """Expose only a confirmation produced from the current video frame."""
    if confirmed_clock is None:
        return None, None, 0.0
    return (
        confirmed_clock.seconds,
        confirmed_clock.is_paused,
        confirmed_clock.confidence,
    )


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
    parser.add_argument(
        "--refresh-url",
        action="store_true",
        help="fetch an ephemeral signed stream URL instead of reading it from SQLite",
    )
    args = parser.parse_args()

    try:
        url, map_number = resolve_source(
            url=args.url,
            database=args.database,
            match_id=args.match_id,
            map_number=args.map_number,
            refresh_url=args.refresh_url,
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
            confirmed_clock: ConfirmedClock | None = None
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
                confirmed_draft = draft_tracker.update(
                    hero_reader.read(frame.image)
                )
                if confirmed_clock is not None:
                    last_clock = confirmed_clock
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
            clock_seconds, is_paused, clock_confidence = current_frame_clock_fields(
                confirmed_clock if state == "game" else None
            )
            observation = LiveObservation(
                raybet_match_id=args.match_id,
                map_number=map_number if state == "game" else None,
                captured_at_utc=captured,
                game_clock_seconds=clock_seconds,
                is_paused=is_paused,
                radiant_hero_ids=(
                    list(last_draft.radiant_hero_ids) if last_draft else []
                ),
                dire_hero_ids=(list(last_draft.dire_hero_ids) if last_draft else []),
                radiant_team_side=radiant_team_side,
                clock_confidence=clock_confidence,
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
