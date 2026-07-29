"""Emit versioned visual observations from a RayBet Dota HLS stream."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

os.environ["OPENCV_FFMPEG_DEBUG"] = "0"
os.environ["OPENCV_LOG_LEVEL"] = "SILENT"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"

import cv2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.live_observation import ComebackState, LiveObservation  # noqa: E402
from live_betting.direct_response_audit import (  # noqa: E402
    DirectResponseContext,
    DirectResponseDecision,
    audited_direct_request,
)
from live_betting.raybet_state import (  # noqa: E402
    infer_current_map_number,
    raybet_match_is_live,
)
from live_betting.raybet import BASE_URL, RayBetClient  # noqa: E402
from live_betting.sanitize import stored_public_stream_url  # noqa: E402
from live_betting.service_coordination import (  # noqa: E402
    add_single_database_argument,
    database_writer_authority,
    service_data_paths,
)
from live_betting.storage import LiveBettingStore  # noqa: E402
from live_betting.vision_frame_registry import (  # noqa: E402
    VisionFrameReceipt,
    publish_vision_frame_bytes,
)
from shared.sqlite import connect as connect_sqlite  # noqa: E402
from vision.hero_recognizer import (  # noqa: E402
    DraftReading,
    DraftTracker,
)
from vision.hud_reader import HudReader  # noqa: E402
from vision.map_state import ConfirmedClock, MapStateTracker  # noqa: E402
from vision.observation_writer import ObservationWriter  # noqa: E402
from vision.scoreboard_reader import (  # noqa: E402
    NetWorthAdvantageReading,
    NetWorthAdvantageTracker,
    ScoreboardReading,
    ScoreboardTracker,
    ReplayGateReading,
)
from vision.stream_capture import HLSStreamCapture  # noqa: E402
from vision.team_side import TeamSideRecognizer, TeamSideTracker  # noqa: E402


COMPLETION_CHECK_INTERVAL = 15
DEFAULT_FEATURES = ROOT / "vision" / "templates" / "hero_features.npz"
ALLOWED_STREAM_HOSTS = frozenset(
    {
        "play.ehome.gg",
        "play.xmshlb.com",
        "qplay.ehome.gg",
        "qplay.shyxswl.com",
    }
)


class WatcherFailure(RuntimeError):
    def __init__(self, category: str, stream_location: str | None = None) -> None:
        super().__init__(category)
        self.category = category
        self.stream_location = stream_location


def _sanitized_stream_location(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.hostname is None:
        return None
    return f"{parsed.hostname.casefold()}{parsed.path}"[:500]


def _watcher_error_category(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "stream_timeout"
    if isinstance(error, cv2.error):
        return "video_backend_error"
    if isinstance(error, ValueError):
        return "validation_failed"
    if isinstance(error, OSError):
        return "io_error"
    return "watcher_failed"


@contextmanager
def _suppress_native_video_stderr() -> Iterator[None]:
    """Keep native OpenCV/FFmpeg diagnostics out of watcher logs."""

    try:
        stderr_fd = sys.__stderr__.fileno()
    except (AttributeError, OSError, ValueError):
        yield
        return
    saved_stderr = os.dup(stderr_fd)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        sys.stderr.flush()
        os.dup2(devnull, stderr_fd)
        yield
    finally:
        os.dup2(saved_stderr, stderr_fd)
        os.close(saved_stderr)
        os.close(devnull)


def _native_safe_frames(
    url: str, *, interval: float, count: int | None
) -> Iterator[object]:
    with _suppress_native_video_stderr(), HLSStreamCapture(url) as capture:
        yield from capture.sample(interval=interval, count=count)


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


def _fresh_stream_payload(
    database: Path, match_id: str
) -> tuple[str, dict[str, object]]:
    endpoint = f"{BASE_URL}/odds"
    request_identity = f"{endpoint}?match_id={match_id}"

    def validate(
        context: DirectResponseContext,
    ) -> DirectResponseDecision[tuple[str, dict[str, object]]]:
        result = context.payload.get("result")
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
        _validate_stream_url(
            url, description=f"fresh live URL for RayBet match {match_id}"
        )
        return DirectResponseDecision(
            (url, result),
            disposition="audit_only",
            reason="stream_url_refresh",
            observed_raybet_match_id=match_id,
        )

    with LiveBettingStore(database) as store, RayBetClient() as client:
        return audited_direct_request(
            store,
            fetch=lambda: client.match_odds_response(match_id),
            process=validate,
            response_kind="live_odds",
            claimed_raybet_match_id=match_id,
            endpoint=endpoint,
            request_identity=request_identity,
            request_metadata={"operation": "stream_url_refresh"},
        )


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
    finally:
        connection.close()
    if not row or (not row[0] and not refresh_url):
        raise ValueError(f"no live_url found for RayBet match {match_id}")
    if refresh_url:
        url, payload = _fresh_stream_payload(database, match_id)
    else:
        stored_url = stored_public_stream_url(row[0], row[1])
        if stored_url is None:
            raise ValueError(
                f"invalid stored live URL for RayBet match {match_id}: "
                "unsigned provenance is missing"
            )
        url = _validate_stream_url(
            stored_url, description=f"stored live URL for RayBet match {match_id}"
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
    if current.comeback_state != previous.comeback_state:
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


def current_frame_comeback_state(
    confirmed_clock: ConfirmedClock | None,
    confirmed_scoreboard: ScoreboardReading | None,
    confirmed_advantage: NetWorthAdvantageReading | None,
) -> ComebackState:
    if confirmed_clock is None:
        return ComebackState.unavailable("hud_clock_unconfirmed")
    if confirmed_scoreboard is None:
        return ComebackState.unavailable("hud_kill_score_unconfirmed")
    if confirmed_advantage is None:
        return ComebackState.unavailable("hud_net_worth_advantage_unconfirmed")
    return ComebackState(
        status="available",
        source="vision_hud",
        confidence=min(
            confirmed_clock.confidence,
            confirmed_scoreboard.confidence,
            confirmed_advantage.confidence,
        ),
        radiant_kills=confirmed_scoreboard.radiant_kills,
        dire_kills=confirmed_scoreboard.dire_kills,
        radiant_net_worth=None,
        dire_net_worth=None,
        net_worth_advantage_side=confirmed_advantage.side,
        net_worth_advantage_min=confirmed_advantage.minimum,
        net_worth_advantage_max=confirmed_advantage.maximum,
        unavailable_reason=None,
    )


def allow_live_hud_tracking(
    replay_gate: ReplayGateReading,
    *,
    map_number: int,
    clock_tracker: MapStateTracker,
    scoreboard_tracker: ScoreboardTracker,
    advantage_tracker: NetWorthAdvantageTracker,
) -> bool:
    if replay_gate.status == "live":
        return True
    clock_tracker.reset_map(map_number)
    scoreboard_tracker.reset()
    advantage_tracker.reset()
    return False


def _should_persist_frame(
    previous: LiveObservation | None,
    current: LiveObservation,
    *,
    captured_at: float,
    last_evidence_at: float,
    evidence_interval: float,
) -> bool:
    """Persist every decision-capable observation and periodic barriers."""
    important_change = (
        previous is None
        or previous.screen_state != current.screen_state
        or (current.is_confirmed and not previous.is_confirmed)
        or current.map_number != previous.map_number
        or current.comeback_state != previous.comeback_state
    )
    return (
        current.is_confirmed
        or important_change
        or captured_at - last_evidence_at >= evidence_interval
    )


def _write_evidence_frame(
    evidence_root: Path,
    image: object,
) -> VisionFrameReceipt:
    """Encode once and publish an atomically content-addressed frame."""
    try:
        written, encoded = cv2.imencode(
            ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85]
        )
        if not written or encoded is None or int(encoded.size) <= 0:
            raise OSError("encoder did not produce a complete evidence frame")
        return publish_vision_frame_bytes(evidence_root, encoded.tobytes())
    except (cv2.error, OSError, RuntimeError, ValueError) as error:
        raise OSError("failed to write content-addressed evidence frame") from error


def capture_heartbeat_path(output: Path) -> Path:
    return output.with_suffix(".heartbeat.json")


def _write_capture_heartbeat(
    output: Path,
    *,
    match_id: str,
    captured_at: datetime,
    capture_status: str,
    layout_name: str,
    layout_confidence: float,
    screen_state: str,
    replay_gate_status: str | None,
) -> None:
    if capture_status not in {"producing_trusted", "capturing_unrecognized"}:
        raise ValueError("capture heartbeat status is invalid")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("capture heartbeat time must be timezone-aware")
    heartbeat = capture_heartbeat_path(output)
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "match_id": match_id,
        "captured_at": captured_at.astimezone(timezone.utc).isoformat(),
        "capture_status": capture_status,
        "layout_profile": layout_name,
        "layout_confidence": round(max(0.0, min(1.0, layout_confidence)), 6),
        "screen_state": screen_state,
        "replay_gate_status": replay_gate_status,
    }
    temporary = heartbeat.with_name(f".{heartbeat.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, heartbeat)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_data_paths(args: argparse.Namespace) -> argparse.Namespace:
    if args.database is not None:
        args.database = Path(args.database).resolve()
    if args.output is None or args.evidence_dir is None:
        if args.database is None:
            raise ValueError(
                "--database is required when --output or --evidence-dir is omitted"
            )
        paths = service_data_paths(args.database)
        if args.output is None:
            args.output = paths.vision_observations / f"{args.match_id}.jsonl"
        if args.evidence_dir is None:
            args.evidence_dir = paths.vision_evidence
    args.output = Path(args.output).resolve()
    args.evidence_dir = Path(args.evidence_dir).resolve()
    return args


def _parse_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--url")
    add_single_database_argument(parser)
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
        args = resolve_data_paths(args)
    except ValueError as error:
        parser.error(str(error))
    return parser, args


def _run_cli(args: argparse.Namespace) -> int:
    url: str | None = None
    try:
        url, map_number = resolve_source(
            url=args.url,
            database=args.database,
            match_id=args.match_id,
            map_number=args.map_number,
            refresh_url=args.refresh_url,
        )
    except Exception as error:
        raise WatcherFailure(_watcher_error_category(error)) from None
    args._resolved_stream_location = _sanitized_stream_location(url)
    output = args.output
    evidence_dir = args.evidence_dir

    hud_reader = HudReader(args.features)
    clock_tracker = MapStateTracker()
    clock_tracker.reset_map(map_number)
    scoreboard_tracker = ScoreboardTracker()
    advantage_tracker = NetWorthAdvantageTracker()
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
    active_layout_name: str | None = None

    try:
        for frame in _native_safe_frames(
            url, interval=args.interval, count=args.count
        ):
            sample_count += 1
            if (
                args.database
                and completion_check_due(sample_count)
                and match_is_complete(args.database, args.match_id)
            ):
                break
            hud = hud_reader.read(frame.image)
            selection = hud.selection
            if active_layout_name is not None and active_layout_name != selection.layout.name:
                clock_tracker.reset_map(map_number)
                scoreboard_tracker.reset()
                advantage_tracker.reset()
                draft_tracker.reset()
            active_layout_name = selection.layout.name
            state = hud.screen_state
            replay_gate_status: str | None = None
            confirmed_clock: ConfirmedClock | None = None
            if state == "game":
                replay_gate = hud.replay_gate
                replay_gate_status = replay_gate.status
                if not allow_live_hud_tracking(
                    replay_gate,
                    map_number=map_number,
                    clock_tracker=clock_tracker,
                    scoreboard_tracker=scoreboard_tracker,
                    advantage_tracker=advantage_tracker,
                ):
                    outside_game_frames += 1
                    confirmed_scoreboard = None
                    confirmed_advantage = None
                    confirmed_draft = None
                    captured = datetime.fromtimestamp(frame.captured_at, timezone.utc)
                    observation = LiveObservation(
                        raybet_match_id=args.match_id,
                        map_number=None,
                        captured_at_utc=captured,
                        game_clock_seconds=None,
                        is_paused=None,
                        radiant_hero_ids=(
                            list(last_draft.radiant_hero_ids) if last_draft else []
                        ),
                        dire_hero_ids=(
                            list(last_draft.dire_hero_ids) if last_draft else []
                        ),
                        radiant_team_side=radiant_team_side,
                        clock_confidence=0.0,
                        draft_confidence=last_draft.confidence if last_draft else 0.0,
                        source_frame_ref=f"stream:{frame.source_hash}:{frame.sequence}",
                        screen_state="replay" if replay_gate.status == "replay" else "unknown",
                        comeback_state=ComebackState.unavailable(
                            "hud_replay_frame"
                            if replay_gate.status == "replay"
                            else "hud_replay_gate_untrusted"
                        ),
                    )
                    if _meaningful(previous, observation):
                        if _should_persist_frame(
                            previous,
                            observation,
                            captured_at=frame.captured_at,
                            last_evidence_at=last_evidence_at,
                            evidence_interval=args.evidence_interval,
                        ):
                            receipt = _write_evidence_frame(evidence_dir, frame.image)
                            observation.source_frame_ref = receipt.frame_ref
                            observation.source_frame_sha256 = receipt.content_sha256
                            observation.source_frame_bytes = receipt.byte_length
                            observation.source_frame_path = str(receipt.storage_path)
                            last_evidence_at = frame.captured_at
                        writer.append(observation)
                        print(observation.model_dump_json())
                        previous = observation
                    _write_capture_heartbeat(
                        output,
                        match_id=args.match_id,
                        captured_at=captured,
                        capture_status=(
                            "producing_trusted"
                            if observation.is_hud_confirmed
                            else "capturing_unrecognized"
                        ),
                        layout_name=selection.layout.name,
                        layout_confidence=selection.confidence,
                        screen_state=observation.screen_state,
                        replay_gate_status=replay_gate_status,
                    )
                    continue
                raw_clock = hud.clock
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
                    scoreboard_tracker.reset()
                    advantage_tracker.reset()
                    draft_tracker.reset()
                    last_clock = None
                    last_draft = None
                    radiant_team_side = None
                    side_tracker.reset()
                outside_game_frames = 0
                confirmed_clock = clock_tracker.update(raw_clock)
                confirmed_scoreboard = scoreboard_tracker.update(hud.scoreboard)
                if confirmed_clock is not None and confirmed_scoreboard is not None:
                    confirmed_advantage = advantage_tracker.update(
                        hud.net_worth_advantage
                    )
                else:
                    advantage_tracker.reset()
                    confirmed_advantage = None
                confirmed_draft = draft_tracker.update(hud.draft)
                if confirmed_clock is not None:
                    last_clock = confirmed_clock
                last_draft = confirmed_draft or last_draft
                if radiant_team_side is None and side_reader is not None:
                    side = side_tracker.update(side_reader.read(frame.image))
                    if side is not None:
                        radiant_team_side = side.radiant_team_side
            else:
                outside_game_frames += 1
                scoreboard_tracker.reset()
                advantage_tracker.reset()
                confirmed_scoreboard = None
                confirmed_advantage = None
            captured = datetime.fromtimestamp(frame.captured_at, timezone.utc)
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
                comeback_state=current_frame_comeback_state(
                    confirmed_clock if state == "game" else None,
                    confirmed_scoreboard if state == "game" else None,
                    confirmed_advantage if state == "game" else None,
                ),
            )
            if _meaningful(previous, observation):
                if _should_persist_frame(
                    previous,
                    observation,
                    captured_at=frame.captured_at,
                    last_evidence_at=last_evidence_at,
                    evidence_interval=args.evidence_interval,
                ):
                    receipt = _write_evidence_frame(evidence_dir, frame.image)
                    observation.source_frame_ref = receipt.frame_ref
                    observation.source_frame_sha256 = receipt.content_sha256
                    observation.source_frame_bytes = receipt.byte_length
                    observation.source_frame_path = str(receipt.storage_path)
                    last_evidence_at = frame.captured_at
                writer.append(observation)
                print(observation.model_dump_json())
                previous = observation
            _write_capture_heartbeat(
                output,
                match_id=args.match_id,
                captured_at=captured,
                capture_status=(
                    "producing_trusted"
                    if observation.is_hud_confirmed
                    else "capturing_unrecognized"
                ),
                layout_name=selection.layout.name,
                layout_confidence=selection.confidence,
                screen_state=observation.screen_state,
                replay_gate_status=replay_gate_status,
            )
    except Exception as error:
        raise WatcherFailure(
            _watcher_error_category(error),
            _sanitized_stream_location(url),
        ) from None
    return 0


def main() -> int:
    parser, args = _parse_args()
    try:
        if args.database is None:
            return _run_cli(args)
        with database_writer_authority(args.database):
            return _run_cli(args)
    except Exception as error:
        failure = (
            error
            if isinstance(error, WatcherFailure)
            else WatcherFailure(
                _watcher_error_category(error),
                getattr(args, "_resolved_stream_location", None)
                or _sanitized_stream_location(getattr(args, "url", None)),
            )
        )
        diagnostic = {"status": "error", "category": failure.category}
        if failure.stream_location is not None:
            diagnostic["stream"] = failure.stream_location
        print(json.dumps(diagnostic, sort_keys=True), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
