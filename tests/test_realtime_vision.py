from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest

from contracts.live_observation import LiveObservation
from live_betting.vision import read_jsonl
from scripts.build_hero_features import build_hero_features
from scripts.fetch_hero_portraits import valid_portrait_bytes
from vision.clock_reader import ClockReader, ClockReading
from vision.hero_recognizer import DraftReading, DraftTracker, HeroRecognizer
from vision.image_features import color_histogram, compute_phash
from vision.layouts import BroadcastLayout, NormalizedRegion
from vision.map_state import MapStateTracker
from vision.observation_writer import ObservationWriter
from vision.screen_state import classify_screen_state
from vision.stream_capture import HLSStreamCapture, nonblack_ratio
from vision.team_side import TeamSideRecognizer, TeamSideTracker


FULL = NormalizedRegion(0, 0, 1, 1)
CLOCK_LAYOUT = BroadcastLayout("clock-test", FULL, FULL)
HERO_LAYOUT = BroadcastLayout("hero-test", FULL, FULL, (FULL,) * 5, (FULL,) * 5)
LEFT = NormalizedRegion(0, 0, 0.5, 1)
RIGHT = NormalizedRegion(0.5, 0, 1, 1)
LOGO_LAYOUT = BroadcastLayout("logo-test", LEFT, LEFT, (), (), LEFT, RIGHT)


def _portrait(color: tuple[int, int, int], marker: str) -> np.ndarray:
    image = np.full((72, 128, 3), color, dtype=np.uint8)
    cv2.putText(
        image,
        marker,
        (25, 55),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.7,
        (255, 255, 255),
        3,
        cv2.LINE_AA,
    )
    return image


def _write_features(path: Path, images: list[np.ndarray]) -> None:
    np.savez_compressed(
        path,
        ids=np.arange(1, len(images) + 1, dtype=np.int32),
        hashes=np.asarray([compute_phash(image) for image in images], dtype=np.uint8),
        histograms=np.asarray(
            [color_histogram(image) for image in images], dtype=np.float32
        ),
        thumbnails=np.asarray(
            [
                cv2.resize(
                    cv2.cvtColor(image, cv2.COLOR_BGR2GRAY),
                    (48, 32),
                    interpolation=cv2.INTER_AREA,
                )
                for image in images
            ],
            dtype=np.uint8,
        ),
    )


def _logo(shape: str) -> np.ndarray:
    image = np.zeros((80, 80, 3), dtype=np.uint8)
    if shape == "circle":
        cv2.circle(image, (40, 40), 25, (255, 255, 255), -1)
    else:
        cv2.rectangle(image, (18, 18), (62, 62), (255, 255, 255), -1)
    return image


def _png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


class FakeCapture:
    def __init__(self, frames: list[tuple[bool, np.ndarray]]) -> None:
        self.frames = iter(frames)
        self.released = False

    def read(self) -> tuple[bool, np.ndarray | None]:
        try:
            return next(self.frames)
        except StopIteration:
            return False, None

    def set(self, *_: object) -> bool:
        return True

    def release(self) -> None:
        self.released = True


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class FakeHttpClient:
    responses: dict[str, bytes | Exception] = {}
    requested: list[str] = []

    def __init__(self, **_: object) -> None:
        pass

    def __enter__(self) -> "FakeHttpClient":
        type(self).requested = []
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get(self, url: str) -> FakeResponse:
        type(self).requested.append(url)
        response = type(self).responses[url]
        if isinstance(response, Exception):
            raise response
        return FakeResponse(response)


def _logo_database(path: Path, *, with_team_logos: bool) -> None:
    payload = {
        "team": [
            {"pos": 2, "team_logo": "/file/fallback-two.png"},
            {"pos": 1, "team_logo": "file/fallback-one.png"},
        ]
    }
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE raybet_matches (
                raybet_match_id TEXT PRIMARY KEY,
                team_one TEXT,
                team_two TEXT,
                raw_json TEXT
            );
            CREATE TABLE teams (name TEXT, tag TEXT, logo_url TEXT);
            """
        )
        connection.execute(
            "INSERT INTO raybet_matches VALUES (?, ?, ?, ?)",
            ("42", "VP", "Xtreme Gaming", json.dumps(payload)),
        )
        if with_team_logos:
            connection.executemany(
                "INSERT INTO teams VALUES (?, ?, ?)",
                [
                    ("Virtus.pro", "VP", "https://cdn.test/vp.png"),
                    ("Xtreme Gaming", "XG", "https://cdn.test/xg.png"),
                ],
            )
        connection.commit()
    finally:
        connection.close()


def test_clock_tracker_confirms_pause_and_rejects_invalid_clock() -> None:
    assert ClockReader._parse_digits("7:16.", 0.95).seconds == 436
    assert ClockReader._parse_digits("-1:20", 0.90).seconds == -80
    tracker = MapStateTracker(confirmations=2, pause_frames=2)
    assert tracker.update(ClockReading(100, 0.95, "1:40")) is None
    assert tracker.update(ClockReading(101, 0.95, "1:41")) is not None
    tracker.update(ClockReading(101, 0.95, "1:41"))
    paused = tracker.update(ClockReading(101, 0.95, "1:41"))
    assert paused is not None and paused.is_paused


def test_clock_tracker_recovers_only_after_confirming_a_forward_jump() -> None:
    tracker = MapStateTracker(confirmations=2, pause_frames=2)
    assert tracker.update(ClockReading(100, 0.95, "1:40")) is None
    assert tracker.update(ClockReading(101, 0.95, "1:41")) is not None
    assert tracker.update(ClockReading(130, 0.95, "2:10")) is None
    recovered = tracker.update(ClockReading(131, 0.95, "2:11"))
    assert recovered is not None
    assert recovered.seconds == 131
    assert not recovered.is_paused


def test_black_frame_is_transition() -> None:
    state, confidence = classify_screen_state(
        np.zeros((100, 200, 3), dtype=np.uint8), CLOCK_LAYOUT
    )
    assert state == "transition"
    assert confidence >= 0.5


def test_exact_portrait_is_recognized(tmp_path: Path) -> None:
    one = _portrait((20, 90, 180), "1")
    two = _portrait((170, 60, 20), "2")
    features = tmp_path / "features.npz"
    _write_features(features, [one, two])
    reading = HeroRecognizer(features, HERO_LAYOUT).recognize_crop(one)
    assert reading.hero_id == 1
    assert reading.confidence >= 0.62


def test_feature_asset_can_be_rebuilt_inside_predictor(tmp_path: Path) -> None:
    source = tmp_path / "heroes"
    source.mkdir()
    cv2.imwrite(str(source / "1.png"), _portrait((20, 90, 180), "1"))
    cv2.imwrite(str(source / "2.png"), _portrait((170, 60, 20), "2"))
    (source / "heroes.json").write_text(
        json.dumps({"1": {"id": 1}, "2": {"id": 2}}), encoding="utf-8"
    )
    output = tmp_path / "hero_features.npz"

    assert build_hero_features(source, output) == 2
    with np.load(output) as features:
        assert features["ids"].tolist() == [1, 2]
        assert features["hashes"].shape == (2, 64)


def test_feature_build_rejects_corrupt_or_missing_portraits(tmp_path: Path) -> None:
    source = tmp_path / "heroes"
    source.mkdir()
    cv2.imwrite(str(source / "1.png"), _portrait((20, 90, 180), "1"))
    (source / "2.png").write_bytes(b"not-a-png" * 128)
    (source / "heroes.json").write_text(
        json.dumps({"1": {"id": 1}, "2": {"id": 2}}), encoding="utf-8"
    )
    output = tmp_path / "hero_features.npz"

    with pytest.raises(ValueError, match="invalid hero portrait"):
        build_hero_features(source, output)
    assert not output.exists()

    (source / "2.png").unlink()
    with pytest.raises(ValueError, match="does not match metadata"):
        build_hero_features(source, output)


def test_portrait_download_validation_decodes_image_content() -> None:
    assert valid_portrait_bytes(_png(_portrait((20, 90, 180), "1")))
    assert not valid_portrait_bytes(b"x" * 1024)


def test_draft_needs_temporal_agreement() -> None:
    tracker = DraftTracker(confirmations=2)
    reading = DraftReading((1, 2, 3, 4, 5), (6, 7, 8, 9, 10), 0.7)
    assert tracker.update(reading) is None
    confirmed = tracker.update(reading)
    assert confirmed is not None and confirmed.confidence == 0.7
    changed = DraftReading((1, 2, 3, 4, 6), (5, 7, 8, 9, 10), 0.95)
    assert tracker.update(changed) is None
    switched = tracker.update(changed)
    assert switched is not None
    assert switched.radiant_hero_ids == changed.radiant_hero_ids
    assert switched.confidence == 0.95


def test_hls_capture_reads_frame() -> None:
    image = np.full((20, 30, 3), 200, dtype=np.uint8)
    capture = HLSStreamCapture(
        "https://example.test/live.m3u8",
        capture_factory=lambda _: FakeCapture([(True, image)]),
    )
    frame = capture.read(timeout=0.1)
    assert frame.image.shape == (20, 30, 3)
    assert frame.sequence == 1
    assert nonblack_ratio(frame.image) == 1.0


def test_observation_contract_and_writer_are_consumer_compatible(
    tmp_path: Path,
) -> None:
    observation = LiveObservation(
        raybet_match_id="42",
        captured_at_utc=datetime.now(timezone.utc),
        map_number=2,
        game_clock_seconds=601,
        is_paused=False,
        radiant_hero_ids=[1, 2, 3, 4, 5],
        dire_hero_ids=[6, 7, 8, 9, 10],
        radiant_team_side="team_one",
        clock_confidence=0.95,
        draft_confidence=0.96,
        source_frame_ref="frame.jpg",
        screen_state="game",
    )
    path = tmp_path / "observations" / "42.jsonl"
    ObservationWriter(path).append(observation)
    parsed = read_jsonl(path)
    assert len(parsed) == 1
    assert parsed[0].is_confirmed
    assert parsed[0].radiant_team_side == "team_one"


def test_observation_rejects_duplicate_heroes() -> None:
    with pytest.raises(ValueError):
        LiveObservation(
            raybet_match_id="42",
            captured_at_utc=datetime.now(timezone.utc),
            radiant_hero_ids=[1, 2, 3, 4, 5],
            dire_hero_ids=[5, 6, 7, 8, 9],
            source_frame_ref="frame.jpg",
        )


def test_observation_rejects_non_positive_heroes() -> None:
    with pytest.raises(ValueError, match="positive"):
        LiveObservation(
            raybet_match_id="42",
            captured_at_utc=datetime.now(timezone.utc),
            radiant_hero_ids=[0, 2, 3, 4, 5],
            dire_hero_ids=[6, 7, 8, 9, 10],
            source_frame_ref="frame.jpg",
        )


def test_observation_rejects_blank_source_frame_ref() -> None:
    with pytest.raises(ValueError, match="source_frame_ref"):
        LiveObservation(
            raybet_match_id="42",
            captured_at_utc=datetime.now(timezone.utc),
            source_frame_ref="  ",
        )


def test_team_side_recognizes_swapped_logos() -> None:
    circle, square = _logo("circle"), _logo("square")
    reading = TeamSideRecognizer(circle, square, LOGO_LAYOUT).read(
        np.hstack((square, circle))
    )
    assert reading.radiant_team_side == "team_two"
    tracker = TeamSideTracker(confirmations=2)
    assert tracker.update(reading) is None
    assert tracker.update(reading).radiant_team_side == "team_two"


def test_team_side_rejects_different_unrelated_stable_images() -> None:
    circle, square = _logo("circle"), _logo("square")
    rng = np.random.default_rng(123)
    left_unrelated = right_unrelated = None
    for _ in range(186):
        left_unrelated = rng.integers(0, 256, (80, 80, 3), dtype=np.uint8)
        right_unrelated = rng.integers(0, 256, (80, 80, 3), dtype=np.uint8)
    assert left_unrelated is not None and right_unrelated is not None
    reading = TeamSideRecognizer(circle, square, LOGO_LAYOUT).read(
        np.hstack((left_unrelated, right_unrelated))
    )
    assert reading.radiant_team_side is None


def test_team_side_database_prefers_team_tag_logo(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "logos.db"
    _logo_database(database, with_team_logos=True)
    FakeHttpClient.responses = {
        "https://cdn.test/vp.png": _png(_logo("circle")),
        "https://cdn.test/xg.png": _png(_logo("square")),
    }
    monkeypatch.setattr("vision.team_side.httpx.Client", FakeHttpClient)
    recognizer = TeamSideRecognizer.from_database(database, "42")
    assert recognizer is not None
    assert FakeHttpClient.requested == [
        "https://cdn.test/vp.png",
        "https://cdn.test/xg.png",
    ]


def test_team_side_database_resolves_raybet_relative_logo_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "logos.db"
    _logo_database(database, with_team_logos=False)
    one = "https://www.ray086.com/file/fallback-one.png"
    two = "https://www.ray086.com/file/fallback-two.png"
    FakeHttpClient.responses = {
        one: _png(_logo("circle")),
        two: _png(_logo("square")),
    }
    monkeypatch.setattr("vision.team_side.httpx.Client", FakeHttpClient)
    recognizer = TeamSideRecognizer.from_database(database, "42")
    assert recognizer is not None
    assert FakeHttpClient.requested == [one, two]


def test_team_side_database_falls_back_after_cdn_failure(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "logos.db"
    _logo_database(database, with_team_logos=True)
    one = "https://www.ray086.com/file/fallback-one.png"
    two = "https://www.ray086.com/file/fallback-two.png"
    FakeHttpClient.responses = {
        "https://cdn.test/vp.png": httpx.ConnectError("offline"),
        "https://cdn.test/xg.png": httpx.ConnectError("offline"),
        one: _png(_logo("circle")),
        two: _png(_logo("square")),
    }
    monkeypatch.setattr("vision.team_side.httpx.Client", FakeHttpClient)

    recognizer = TeamSideRecognizer.from_database(database, "42")

    assert recognizer is not None
    assert FakeHttpClient.requested == [
        "https://cdn.test/vp.png",
        one,
        "https://cdn.test/xg.png",
        two,
    ]


def test_team_side_database_degrades_when_all_logo_sources_fail(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "logos.db"
    _logo_database(database, with_team_logos=True)
    error = httpx.ConnectError("offline")
    FakeHttpClient.responses = {
        "https://cdn.test/vp.png": error,
        "https://cdn.test/xg.png": error,
        "https://www.ray086.com/file/fallback-one.png": error,
        "https://www.ray086.com/file/fallback-two.png": error,
    }
    monkeypatch.setattr("vision.team_side.httpx.Client", FakeHttpClient)

    assert TeamSideRecognizer.from_database(database, "42") is None
