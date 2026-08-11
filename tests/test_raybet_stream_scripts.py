from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from contracts.live_observation import LiveObservation
from live_betting.vision_frame_registry import VisionFrameReceipt
from scripts import supervise_raybet_streams, watch_raybet_stream
from scripts.supervise_raybet_streams import watcher_command
from scripts.watch_raybet_stream import (
    _current_frame_starts_map,
    current_frame_comeback_state,
    _observation_map_number,
    _resume_map_clock,
    _sample_manifest_path,
    _sanitized_stream_location,
    _should_persist_frame,
    _validate_stream_url,
    _write_sample_manifest,
    match_is_complete,
)
from web.routers import monitor as monitor_router
from vision.map_state import ConfirmedClock


@pytest.mark.parametrize(
    ("screen_state", "map_started", "expected"),
    [
        ("draft", False, 3),
        ("game", True, 3),
        ("game", False, None),
        ("replay", True, None),
        ("unknown", True, None),
        ("transition", True, None),
    ],
)
def test_observation_map_number_is_retained_only_for_target_live_states(
    screen_state: str,
    map_started: bool,
    expected: int | None,
) -> None:
    assert (
        _observation_map_number(screen_state, 3, map_started=map_started) == expected
    )


def test_current_frame_starts_map_accepts_provider_start_after_clock_confirmation() -> None:
    provider_started_at = datetime(2026, 8, 11, 14, 3, 24, tzinfo=timezone.utc)
    captured_at = datetime(2026, 8, 11, 14, 5, 34, tzinfo=timezone.utc)
    confirmed_clock = ConfirmedClock(
        map_number=3,
        seconds=2429,
        is_paused=False,
        confidence=0.98,
    )

    assert _current_frame_starts_map(
        confirmed_clock,
        provider_map_started_at=provider_started_at,
        captured_at=captured_at,
    )


@pytest.mark.parametrize(
    ("persisted_map", "expected_clock"),
    [
        (1, None),
        (2, 900),
        (3, None),
    ],
)
def test_watcher_resume_never_overrides_provider_map_identity(
    persisted_map: int,
    expected_clock: int | None,
) -> None:
    persisted = ConfirmedClock(
        map_number=persisted_map,
        seconds=900,
        is_paused=False,
        confidence=0.96,
    )

    map_number, clock = _resume_map_clock(2, persisted, 3)

    assert map_number == 2
    assert (None if clock is None else clock.seconds) == expected_clock


@pytest.mark.parametrize(
    ("screen_state", "expected_reason"),
    [
        ("draft", "draft_in_progress"),
        ("unknown", "screen_state_unknown"),
        ("game", "hud_clock_unconfirmed"),
    ],
)
def test_current_frame_comeback_state_reports_the_actual_unavailable_context(
    screen_state: str,
    expected_reason: str,
) -> None:
    state = current_frame_comeback_state(
        None,
        None,
        None,
        screen_state=screen_state,
    )

    assert state.status == "unavailable"
    assert state.unavailable_reason == expected_reason


def test_signed_hls_url_is_validated_without_leaking_query() -> None:
    url = "https://play.ehome.gg/live/match.m3u8?token=secret&expires=123"

    assert _validate_stream_url(url) == url
    assert _sanitized_stream_location(url) == "play.ehome.gg/live/match.m3u8"


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/live.m3u8",
        "https://user:password@play.ehome.gg/live.m3u8",
        "https://example.com/live.m3u8",
        "https://play.ehome.gg/live.m3u8#fragment",
    ],
)
def test_stream_url_rejects_non_public_or_credentialed_locations(url: str) -> None:
    with pytest.raises(ValueError, match="invalid stream URL"):
        _validate_stream_url(url)


def test_supervisor_starts_the_retained_hls_watcher(tmp_path: Path) -> None:
    command = watcher_command(
        "postgresql+psycopg://user@localhost/dota2",
        "raybet-1",
        tmp_path / "output",
        tmp_path / "evidence",
    )

    assert command[1].endswith("watch_raybet_stream.py")
    assert command[command.index("--match-id") + 1] == "raybet-1"
    assert "--refresh-url" in command
    assert "--evidence-dir" in command


def test_prematch_exact_dota_payload_is_eligible_only_inside_watch_window() -> None:
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    payload = json.dumps({"id": "42", "game_id": 151, "status": "1"})

    assert supervise_raybet_streams._exact_dota_live_payload(payload, "42")
    assert supervise_raybet_streams._prematch_window_open(
        (now + timedelta(minutes=20)).isoformat(), now=now
    )
    assert not supervise_raybet_streams._prematch_window_open(
        (now + timedelta(minutes=31)).isoformat(), now=now
    )
    assert supervise_raybet_streams._prematch_window_open(
        "2026-08-09 20:20:00",
        now=now,
    )


def test_supervisor_starts_near_term_prematch_with_verified_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    url = "https://play.ehome.gg/live/42.m3u8"
    raw = json.dumps(
        {
            "id": "42",
            "game_id": 151,
            "status": "1",
            "_dota2_predictor_public_stream_v1": {
                "source": "direct_unsigned_v1",
                "url": url,
            },
        }
    )

    class Result:
        @staticmethod
        def fetchall() -> list[tuple[object, ...]]:
            return [
                (
                    "42",
                    "1",
                    now.isoformat(),
                    url,
                    raw,
                    (now + timedelta(minutes=20)).isoformat(),
                    None,
                )
            ]

    class Connection:
        @staticmethod
        def execute(_query: str, _params: tuple[object, ...]) -> Result:
            return Result()

    class Store:
        connection = Connection()

        def __enter__(self) -> "Store":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        supervise_raybet_streams,
        "LiveBettingStore",
        lambda _database_url: Store(),
    )

    assert supervise_raybet_streams.active_match_evidence(
        "postgresql://example", now=now
    ) == {"42": "prematch_verified_stream"}


@pytest.mark.parametrize(
    ("database_status", "scheduled_delta"),
    [
        ("1", timedelta(minutes=20)),
        ("unlisted", timedelta(minutes=-5)),
    ],
)
def test_supervisor_probes_ephemeral_stream_inside_prematch_window(
    monkeypatch: pytest.MonkeyPatch,
    database_status: str,
    scheduled_delta: timedelta,
) -> None:
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    raw = json.dumps({"id": "42", "game_id": 151, "status": "1"})

    class Result:
        @staticmethod
        def fetchall() -> list[tuple[object, ...]]:
            return [
                (
                    "42",
                    database_status,
                    now.isoformat(),
                    None,
                    raw,
                    (now + scheduled_delta).isoformat(),
                    None,
                )
            ]

    class Connection:
        @staticmethod
        def execute(_query: str, _params: tuple[object, ...]) -> Result:
            return Result()

    class Store:
        connection = Connection()

        def __enter__(self) -> "Store":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        supervise_raybet_streams,
        "LiveBettingStore",
        lambda _database_url: Store(),
    )

    assert supervise_raybet_streams.active_match_evidence(
        "postgresql://example", now=now
    ) == {"42": "prematch_ephemeral_stream_probe"}


def test_prematch_ephemeral_probe_does_not_exhaust_before_stream_appears() -> None:
    failed_at = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    first = supervise_raybet_streams.watcher_retry_after_failure(
        None,
        exit_code=2,
        produced_output=False,
        failed_at=failed_at,
        persistent_probe=True,
    )
    repeated = supervise_raybet_streams.watcher_retry_after_failure(
        first,
        exit_code=2,
        produced_output=False,
        failed_at=failed_at + timedelta(minutes=1),
        persistent_probe=True,
    )

    assert first.attempts == repeated.attempts == 1
    assert not first.exhausted and not repeated.exhausted
    assert repeated.retry_at == failed_at + timedelta(minutes=2)

    live_retry = supervise_raybet_streams.watcher_retry_after_failure(
        repeated,
        exit_code=2,
        produced_output=False,
        failed_at=failed_at + timedelta(minutes=2),
    )
    assert live_retry.attempts == 2
    assert not live_retry.exhausted


def test_evidence_storage_is_observed_without_automatic_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        supervise_raybet_streams.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(
            total=100 * 1024**3,
            used=94 * 1024**3,
            free=6 * 1024**3,
        ),
    )

    status = supervise_raybet_streams.evidence_storage_health(tmp_path)

    assert status["status"] == "healthy"
    assert status["free_bytes"] == 6 * 1024**3
    assert status["automatic_deletion_enabled"] is False


def test_evidence_write_failure_has_a_distinct_supervisor_reason() -> None:
    failed_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

    state = supervise_raybet_streams.watcher_retry_after_failure(
        None,
        exit_code=4,
        produced_output=True,
        failed_at=failed_at,
    )

    assert state.failure_reason == "evidence_write_failed"
    assert watch_raybet_stream._watcher_error_category(
        OSError("failed to write evidence manifest")
    ) == "evidence_write_failed"
    assert watch_raybet_stream._watcher_exit_code("evidence_write_failed") == 4


def test_every_sample_with_an_explicit_map_identity_is_persisted() -> None:
    mapped = LiveObservation(
        raybet_match_id="42",
        map_number=2,
        captured_at_utc=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
        source_frame_ref="stream:42:1",
        screen_state="draft",
    )
    unassigned = mapped.model_copy(
        update={"map_number": None, "source_frame_ref": "stream:42:2"}
    )

    assert _should_persist_frame(
        None,
        mapped,
        captured_at=60.0,
        last_evidence_at=0.0,
        evidence_interval=30.0,
    )
    assert not _should_persist_frame(
        None,
        unassigned,
        captured_at=60.0,
        last_evidence_at=0.0,
        evidence_interval=30.0,
    )
    assert _should_persist_frame(
        None,
        unassigned,
        captured_at=60.0,
        last_evidence_at=0.0,
        evidence_interval=30.0,
        evidence_due=True,
    )


def test_sample_manifest_is_partitioned_by_series_map_and_utc_day(
    tmp_path: Path,
) -> None:
    captured_at = datetime(2026, 8, 11, 23, 59, tzinfo=timezone.utc)
    digest = "a" * 64
    receipt = VisionFrameReceipt(
        frame_ref=f"vision-frame:sha256:{digest}",
        content_sha256=digest,
        byte_length=1234,
        storage_path=tmp_path / "sha256" / "aa" / f"{digest}.jpg",
    )
    observation = LiveObservation(
        raybet_match_id="38422865",
        map_number=2,
        captured_at_utc=captured_at,
        game_clock_seconds=600,
        radiant_team_side="team_one",
        clock_confidence=0.96,
        source_frame_ref=receipt.frame_ref,
        source_frame_sha256=receipt.content_sha256,
        source_frame_bytes=receipt.byte_length,
        source_frame_path=str(receipt.storage_path),
        screen_state="game",
    )

    path = _write_sample_manifest(
        tmp_path,
        observation=observation,
        receipt=receipt,
        lifecycle_events=(),
    )
    _write_sample_manifest(
        tmp_path,
        observation=observation.model_copy(
            update={"captured_at_utc": captured_at + timedelta(seconds=1)}
        ),
        receipt=receipt,
        lifecycle_events=(),
    )

    assert path == _sample_manifest_path(
        tmp_path,
        match_id="38422865",
        map_number=2,
        captured_at=captured_at,
    )
    assert path.relative_to(tmp_path).parts == (
        "series",
        "38422865",
        "map_2",
        "2026-08-11",
        "frames.jsonl",
    )
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["observation_identity"] == {
        "raybet_match_id": "38422865",
        "map_number": 2,
        "captured_at": captured_at.isoformat(),
        "source_frame_ref": receipt.frame_ref,
    }
    assert rows[0]["frame"]["content_sha256"] == digest
    assert rows[0]["draft_player_names"]["unavailable_reason"] == (
        "draft_player_names_not_observed"
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [("1", False), ("2", False), ("3", True), ("finished", True)],
)
def test_watcher_closes_only_on_terminal_match_evidence(
    status: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Result:
        @staticmethod
        def fetchone() -> tuple[str, str, str, int]:
            return status, "2026-08-09T10:00:00+00:00", "{}", 3

    class Connection:
        @staticmethod
        def execute(_query: str, _params: tuple[str]) -> Result:
            return Result()

    class Store:
        connection = Connection()

        def __enter__(self) -> "Store":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        watch_raybet_stream,
        "LiveBettingStore",
        lambda _database_url: Store(),
    )

    assert match_is_complete("postgresql://example", "42") is expected


def test_provider_series_state_reports_live_map_from_fresh_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "id": "42",
            "game_id": 151,
            "status": "2",
            "team": [
                {
                    "pos": 1,
                    "team_id": 1,
                    "score": {
                        "r1": 1,
                        "manualControlData": {"currentIndex": 3},
                    },
                },
                {
                    "pos": 2,
                    "team_id": 2,
                    "score": {
                        "r1": 0,
                        "manualControlData": {"currentIndex": 3},
                    },
                },
            ],
            "odds": [],
        }
    )

    class Result:
        @staticmethod
        def fetchone() -> tuple[str, str, str, int]:
            return "2", "2026-08-10T10:00:00+00:00", payload, 3

    class Connection:
        @staticmethod
        def execute(_query: str, _params: tuple[str]) -> Result:
            return Result()

    class Store:
        connection = Connection()

        def __enter__(self) -> "Store":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        watch_raybet_stream,
        "LiveBettingStore",
        lambda _database_url: Store(),
    )

    assert watch_raybet_stream._provider_series_state(
        "postgresql://example", "42"
    ) == (3, False)


def test_observation_directory_is_shared_by_supervisor_and_direct_watcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation_root = tmp_path / "shared-observations"
    monkeypatch.setenv("VISION_OBSERVATION_DIR", str(observation_root))

    supervisor_args = supervise_raybet_streams.resolve_data_paths(
        argparse.Namespace(output_dir=None, evidence_dir=None, log_dir=None)
    )
    watcher_args = watch_raybet_stream.resolve_data_paths(
        argparse.Namespace(match_id="38417147", output=None, evidence_dir=None)
    )

    assert supervisor_args.output_dir == observation_root.resolve()
    assert watcher_args.output == observation_root.resolve() / "38417147.jsonl"


class _LiveMatchConnection:
    def __init__(self) -> None:
        self.closed = False

    def execute(self, query: str, params: tuple[str]) -> object:
        assert "FROM raybet_matches" in query
        assert params == ("42",)

        class _Result:
            @staticmethod
            def fetchone() -> dict[str, object]:
                return {
                    "raybet_match_id": "42",
                    "tournament": "Event",
                    "team_one": "Team A",
                    "team_two": "Team B",
                    "scheduled_at": "2026-08-09T12:00:00+00:00",
                    "best_of": 3,
                    "status": "2",
                    "live_url": None,
                    "raw_json": json.dumps(
                        {
                            "id": "42",
                            "game_id": 151,
                            "status": "2",
                            "team": [
                                {"pos": 1, "team_id": 1},
                                {"pos": 2, "team_id": 2},
                            ],
                        }
                    ),
                    "updated_at": "2026-08-09T12:00:00+00:00",
                }

        return _Result()

    def close(self) -> None:
        self.closed = True


def test_live_stream_route_redirects_to_fresh_hls(monkeypatch) -> None:
    connection = _LiveMatchConnection()
    stream_url = "https://play.ehome.gg/live/42.m3u8?expires=1&sig=test"
    monkeypatch.setattr(monitor_router.queries, "get_db", lambda: connection)
    monkeypatch.setattr(
        monitor_router,
        "_fresh_live_stream_url",
        lambda match_id: stream_url if match_id == "42" else "",
    )

    response = monitor_router.live_stream("42")

    assert connection.closed is True
    assert response.status_code == 307
    assert response.headers["location"] == stream_url
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_live_stream_route_reports_resolver_outage(monkeypatch) -> None:
    connection = _LiveMatchConnection()
    monkeypatch.setattr(monitor_router.queries, "get_db", lambda: connection)

    def unavailable(_: str) -> str:
        raise TimeoutError("upstream unavailable")

    monkeypatch.setattr(monitor_router, "_fresh_live_stream_url", unavailable)

    with pytest.raises(HTTPException) as captured:
        monitor_router.live_stream("42")

    assert connection.closed is True
    assert captured.value.status_code == 503
