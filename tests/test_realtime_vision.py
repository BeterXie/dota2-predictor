from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest

from contracts.live_observation import ComebackState, LiveObservation
from live_betting.vision import read_jsonl
from scripts.build_hero_features import build_hero_features
from scripts.fetch_hero_portraits import valid_portrait_bytes
from vision.clock_reader import ClockReader, ClockReading
from vision.hero_recognizer import DraftReading, DraftTracker, HeroRecognizer
from vision.hud_reader import HudReader
from vision.image_features import color_histogram, compute_phash
from vision.layout_selector import select_broadcast_layout
from vision.layouts import (
    BroadcastLayout,
    EPL_MASTERS_LIVE,
    NormalizedRegion,
    STANDARD_DOTA_HUD,
)
from vision.map_state import MapStateTracker
from vision.observation_writer import ObservationWriter
from vision.screen_state import classify_screen_state
from vision.scoreboard_reader import (
    NetWorthAdvantageReading,
    NetWorthAdvantageTracker,
    ScoreboardReader,
    ScoreboardReading,
    ScoreboardTracker,
    ReplayGateReading,
)
from vision.stream_capture import HLSStreamCapture, nonblack_ratio
from vision.team_side import TeamSideRecognizer, TeamSideTracker


FULL = NormalizedRegion(0, 0, 1, 1)
CLOCK_LAYOUT = BroadcastLayout("clock-test", FULL, FULL)
HERO_LAYOUT = BroadcastLayout("hero-test", FULL, FULL, (FULL,) * 5, (FULL,) * 5)
LEFT = NormalizedRegion(0, 0, 0.5, 1)
RIGHT = NormalizedRegion(0.5, 0, 1, 1)
LOGO_LAYOUT = BroadcastLayout("logo-test", LEFT, LEFT, (), (), LEFT, RIGHT)
SCORE_LAYOUT = BroadcastLayout(
    "score-test",
    FULL,
    FULL,
    radiant_kills=LEFT,
    dire_kills=RIGHT,
)


def _synthetic_epl_hud() -> np.ndarray:
    image = np.full((1080, 1920, 3), 70, dtype=np.uint8)
    cyan = (255, 255, 0)
    cv2.rectangle(image, (451, 0), (480, 70), cyan, -1)
    cv2.rectangle(image, (1440, 0), (1469, 70), cyan, -1)
    cv2.rectangle(image, (1305, 918), (1651, 1079), (12, 12, 12), -1)
    cv2.rectangle(image, (1536, 928), (1612, 1079), cyan, -1)
    return image


def test_epl_layout_requires_the_complete_hud_geometry() -> None:
    image = _synthetic_epl_hud()
    selection = select_broadcast_layout(image)

    assert selection.layout == EPL_MASTERS_LIVE
    assert selection.confidence >= 0.9

    image[:, 1440:1470] = 70
    fallback = select_broadcast_layout(image)
    assert fallback.layout == STANDARD_DOTA_HUD


def test_epl_scoreboard_strip_uses_positioned_ocr_results() -> None:
    reader = ScoreboardReader(EPL_MASTERS_LIVE, use_ocr=False)

    class PositionedOcr:
        def __call__(self, image, **kwargs):
            del image, kwargs
            return [
                [[[300, 10], [325, 10], [325, 30], [300, 30]], "6", 0.99],
                [[[450, 10], [465, 10], [465, 30], [450, 30]], "3", 0.98],
                [[[295, 42], [328, 42], [328, 55], [295, 55]], "<1k", 0.999],
            ], None

    reader.ocr = PositionedOcr()
    reading = reader.read(np.zeros((1080, 1920, 3), dtype=np.uint8))

    assert reading == ScoreboardReading(6, 3, 0.98)


def test_epl_positioned_ocr_is_reused_for_clock_and_advantage() -> None:
    reader = ScoreboardReader(EPL_MASTERS_LIVE, use_ocr=False)

    class PositionedOcr:
        calls = 0

        def __call__(self, image, **kwargs):
            del image, kwargs
            self.calls += 1
            return [
                [[[300, 10], [325, 10], [325, 30], [300, 30]], "6", 0.99],
                [[[450, 10], [465, 10], [465, 30], [450, 30]], "3", 0.98],
                [[[365, 23], [402, 23], [402, 36], [365, 36]], "6:03", 0.97],
                [[[295, 42], [328, 42], [328, 55], [295, 55]], "<1k", 0.96],
            ], None

    ocr = PositionedOcr()
    reader.ocr = ocr
    image = np.zeros((1080, 1920, 3), dtype=np.uint8)

    assert reader.read_positioned_clock(image) == ClockReading(363, 0.97, "6:03")
    assert reader.read(image) == ScoreboardReading(6, 3, 0.98)
    assert reader.read_net_worth_advantage(image) == NetWorthAdvantageReading(
        "radiant",
        0,
        999,
        0.96,
    )
    assert ocr.calls == 1


def test_layout_aware_hud_reader_keeps_hud_independent_from_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _synthetic_epl_hud()
    reader = HudReader(use_ocr=False)
    profile = reader._profile(EPL_MASTERS_LIVE)

    class PositionedOcr:
        def __call__(self, image, **kwargs):
            del image, kwargs
            return [
                [[[300, 10], [325, 10], [325, 30], [300, 30]], "12", 0.99],
                [[[450, 10], [475, 10], [475, 30], [450, 30]], "3", 0.99],
                [[[365, 23], [402, 23], [402, 36], [365, 36]], "13:28", 0.98],
                [[[295, 42], [328, 42], [328, 55], [295, 55]], "11k", 0.97],
            ], None

    profile.scoreboard.ocr = PositionedOcr()
    monkeypatch.setattr(
        "vision.hud_reader.classify_screen_state",
        lambda image, layout: ("game", 0.98),
    )

    reading = reader.read(image)

    assert reading.selection.layout == EPL_MASTERS_LIVE
    assert reading.replay_gate.status == "live"
    assert reading.clock == ClockReading(808, 0.98, "13:28")
    assert reading.scoreboard == ScoreboardReading(12, 3, 0.99)
    assert reading.net_worth_advantage == NetWorthAdvantageReading(
        "radiant", 11_000, 11_999, 0.97
    )
    assert reading.is_hud_available
    assert reading.draft == DraftReading((), (), 0.0)


def test_epl_live_gate_requires_scoreboard_geometry_and_replay_overrides() -> None:
    image = _synthetic_epl_hud()
    reader = ScoreboardReader(EPL_MASTERS_LIVE, use_ocr=False)

    class PositionedOcr:
        replay = False

        def __call__(self, image, **kwargs):
            del image, kwargs
            rows = [
                [[[300, 10], [325, 10], [325, 30], [300, 30]], "6", 0.99],
                [[[450, 10], [465, 10], [465, 30], [450, 30]], "3", 0.98],
                [[[365, 23], [402, 23], [402, 36], [365, 36]], "6:03", 0.97],
            ]
            if self.replay:
                rows.append(
                    [[[320, 65], [420, 65], [420, 82], [320, 82]], "REPLAY", 0.99]
                )
            return rows, None

    ocr = PositionedOcr()
    reader.ocr = ocr
    assert reader.read_replay_gate(image).status == "live"

    ocr.replay = True
    reader._strip_cache_image = None
    assert reader.read_replay_gate(image).status == "replay"

    reader.ocr = lambda image, **kwargs: (
        [[[[90, 40], [180, 40], [180, 60], [90, 60]], "EPL MASTERS", 0.99]],
        None,
    )
    reader._strip_cache_image = None
    assert reader.read_replay_gate(image).status == "untrusted"


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


def test_scoreboard_reader_extracts_both_kill_scores_from_a_synthetic_hud() -> None:
    image = np.zeros((80, 240, 3), dtype=np.uint8)
    reader = ScoreboardReader(SCORE_LAYOUT, use_ocr=False)
    for digit, left in (("1", 24), ("8", 52), ("2", 144), ("5", 172)):
        glyph = cv2.cvtColor(reader.templates[digit], cv2.COLOR_GRAY2BGR)
        image[22:58, left : left + 24] = glyph

    reading = reader.read(image)

    assert reading.radiant_kills == 18
    assert reading.dire_kills == 25
    assert reading.confidence >= 0.9


def test_standard_score_regions_do_not_overlap_clock_or_hero_portraits() -> None:
    layout = STANDARD_DOTA_HUD
    assert layout.radiant_kills is not None
    assert layout.dire_kills is not None
    assert layout.radiant_heroes[-1].right < layout.radiant_kills.left
    assert layout.radiant_kills.right < layout.clock.left
    assert layout.clock.right < layout.dire_kills.left
    assert layout.dire_kills.right < layout.dire_heroes[0].left
    assert layout.radiant_net_worth_advantage is not None
    assert layout.dire_net_worth_advantage is not None
    assert (
        layout.radiant_net_worth_advantage.right
        < layout.dire_net_worth_advantage.left
    )


def test_scoreboard_tracker_requires_two_monotonic_high_confidence_frames() -> None:
    tracker = ScoreboardTracker(confirmations=2)
    assert tracker.update(ScoreboardReading(18, 25, 0.96)) is None
    assert tracker.update(ScoreboardReading(18, 26, 0.94)) is None
    confirmed = tracker.update(ScoreboardReading(18, 26, 0.93))
    assert confirmed == ScoreboardReading(18, 26, 0.93)

    assert tracker.update(ScoreboardReading(17, 26, 0.99)) is None
    assert tracker.update(ScoreboardReading(18, 26, 0.89)) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<1k", (0, 999, 0.95)),
        ("1k", (1_000, 1_999, 0.95)),
        ("9K", (9_000, 9_999, 0.95)),
        ("10kv", (10_000, 10_999, 0.95)),
        ("0k", None),
        ("<2k", None),
        ("channeling", None),
    ],
)
def test_net_worth_advantage_parser_preserves_bucket_bounds(
    text: str,
    expected: tuple[int, int, float] | None,
) -> None:
    assert ScoreboardReader._parse_advantage_text(text, 0.95) == expected


def test_net_worth_advantage_requires_two_matching_high_confidence_frames() -> None:
    tracker = NetWorthAdvantageTracker(confirmations=2)
    one = NetWorthAdvantageReading("radiant", 1_000, 1_999, 0.96)
    assert tracker.update(one) is None
    assert (
        tracker.update(NetWorthAdvantageReading("dire", 1_000, 1_999, 0.96))
        is None
    )
    confirmed = tracker.update(
        NetWorthAdvantageReading("dire", 1_000, 1_999, 0.94)
    )
    assert confirmed == NetWorthAdvantageReading("dire", 1_000, 1_999, 0.94)
    assert (
        tracker.update(NetWorthAdvantageReading("dire", 2_000, 2_999, 0.89))
        is None
    )


@pytest.mark.parametrize(
    ("readings", "expected"),
    [
        ([('HIGHLIGHTS', 0.99)], ReplayGateReading("replay", 0.99, "HIGHLIGHTS")),
        ([('REPLAY', 0.95)], ReplayGateReading("replay", 0.95, "REPLAY")),
        ([('PLAYOFFS', 0.98)], ReplayGateReading("untrusted", 0.98, None)),
        (
            [('PLAYOFFS', 0.98), ('QUARTERFINAL', 0.99)],
            ReplayGateReading("live", 0.99, "QUARTERFINAL"),
        ),
        (
            [
                ('HIGHLIGHTS', 0.75),
                ('PLAYOFFS', 0.98),
                ('QUARTERFINAL', 0.99),
            ],
            ReplayGateReading("untrusted", 0.75, "HIGHLIGHTS"),
        ),
        ([('HIGHLIGHTS', 0.75)], ReplayGateReading("untrusted", 0.75, "HIGHLIGHTS")),
        ([], ReplayGateReading("untrusted", 0.0, None)),
    ],
)
def test_replay_gate_is_explicit_and_fails_closed(
    readings: list[tuple[str, float]],
    expected: ReplayGateReading,
) -> None:
    assert ScoreboardReader._classify_broadcast_text(
        readings,
        live_marker_sets=STANDARD_DOTA_HUD.live_broadcast_marker_sets,
    ) == expected


def test_replay_gate_matches_fixed_real_broadcast_status_crops() -> None:
    fixture_root = Path(__file__).parent / "fixtures" / "vision" / "replay_gate"
    highlight = fixture_root / "highlights.jpg"
    live = fixture_root / "live_playoffs_quarterfinal.jpg"
    assert highlight.is_file()
    assert live.is_file()
    fixture_layout = replace(STANDARD_DOTA_HUD, broadcast_status=FULL)
    reader = ScoreboardReader(fixture_layout)
    highlight_image = cv2.imread(str(highlight))
    live_image = cv2.imread(str(live))
    assert highlight_image is not None and highlight_image.shape[:2] == (302, 307)
    assert live_image is not None and live_image.shape[:2] == (302, 307)

    assert reader.read_replay_gate(highlight_image).status == "replay"
    assert reader.read_replay_gate(live_image).status == "live"


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
    assert parsed[0].comeback_state.status == "unavailable"
    assert parsed[0].comeback_state.unavailable_reason == "live_state_not_provided"


def test_available_comeback_state_round_trips_from_the_same_evidence_frame(
    tmp_path: Path,
) -> None:
    observation = LiveObservation(
        raybet_match_id="42",
        captured_at_utc=datetime.now(timezone.utc),
        source_frame_ref="frame.jpg",
        comeback_state=ComebackState(
            status="available",
            source="vision_hud",
            confidence=0.95,
            radiant_kills=18,
            dire_kills=25,
            radiant_net_worth=42_000,
            dire_net_worth=49_500,
            unavailable_reason=None,
        ),
    )
    path = tmp_path / "observations" / "42.jsonl"

    ObservationWriter(path).append(observation)
    state = read_jsonl(path)[0].comeback_state

    assert state.is_available
    assert state.source == "vision_hud"
    assert state.confidence == 0.95
    assert state.radiant_kills == 18
    assert state.dire_kills == 25
    assert state.radiant_net_worth == 42_000
    assert state.dire_net_worth == 49_500


def test_hud_confirmation_does_not_require_a_confirmed_draft(tmp_path: Path) -> None:
    observation = LiveObservation(
        raybet_match_id="42",
        captured_at_utc=datetime.now(timezone.utc),
        map_number=2,
        game_clock_seconds=808,
        is_paused=False,
        clock_confidence=0.98,
        draft_confidence=0.0,
        source_frame_ref="frame.jpg",
        screen_state="game",
        comeback_state=ComebackState(
            status="available",
            source="vision_hud",
            confidence=0.97,
            radiant_kills=12,
            dire_kills=3,
            net_worth_advantage_side="radiant",
            net_worth_advantage_min=11_000,
            net_worth_advantage_max=11_999,
            unavailable_reason=None,
        ),
    )

    assert observation.is_hud_confirmed
    assert not observation.is_confirmed

    path = tmp_path / "42.jsonl"
    ObservationWriter(path).append(observation)
    parsed = read_jsonl(path)[0]
    assert parsed.is_hud_confirmed
    assert not parsed.is_confirmed


def test_bucketed_net_worth_advantage_round_trips_without_exact_totals(
    tmp_path: Path,
) -> None:
    observation = LiveObservation(
        raybet_match_id="42",
        captured_at_utc=datetime.now(timezone.utc),
        source_frame_ref="frame.jpg",
        comeback_state=ComebackState(
            status="available",
            source="vision_hud",
            confidence=0.95,
            radiant_kills=18,
            dire_kills=25,
            net_worth_advantage_side="dire",
            net_worth_advantage_min=5_000,
            net_worth_advantage_max=5_999,
            unavailable_reason=None,
        ),
    )
    path = tmp_path / "observations" / "42.jsonl"

    ObservationWriter(path).append(observation)
    state = read_jsonl(path)[0].comeback_state

    assert state.radiant_net_worth is None
    assert state.dire_net_worth is None
    assert state.net_worth_advantage_side == "dire"
    assert state.net_worth_advantage_min == 5_000
    assert state.net_worth_advantage_max == 5_999


def test_comeback_state_fails_closed_instead_of_accepting_partial_values() -> None:
    with pytest.raises(ValueError, match="complete trusted HUD evidence"):
        ComebackState(
            status="available",
            source="vision_hud",
            confidence=0.95,
            radiant_kills=18,
            unavailable_reason=None,
        )
    with pytest.raises(ValueError, match="cannot contain inferred HUD values"):
        ComebackState(
            status="unavailable",
            confidence=0.0,
            radiant_kills=18,
            unavailable_reason="partial_ocr",
        )
    with pytest.raises(ValueError, match="complete trusted HUD evidence"):
        ComebackState(
            status="available",
            source="vision_hud",
            confidence=0.95,
            radiant_kills=18,
            dire_kills=25,
            net_worth_advantage_side="dire",
            net_worth_advantage_min=1_000,
            unavailable_reason=None,
        )


def test_legacy_observation_explicitly_degrades_comeback_state() -> None:
    payload = LiveObservation(
        raybet_match_id="42",
        captured_at_utc=datetime.now(timezone.utc),
        source_frame_ref="frame.jpg",
    ).model_dump(mode="json")
    payload["schema_version"] = 2
    payload.pop("comeback_state")

    from live_betting.vision import parse_observation

    state = parse_observation(payload).comeback_state
    assert state.status == "unavailable"
    assert state.unavailable_reason == "legacy_schema_live_state_unavailable"


@pytest.mark.parametrize("economy_kind", ["bucket", "exact"])
def test_schema_v3_cannot_disguise_v4_economy_evidence(economy_kind: str) -> None:
    economy = (
        {
            "net_worth_advantage_side": "dire",
            "net_worth_advantage_min": 5_000,
            "net_worth_advantage_max": 5_999,
        }
        if economy_kind == "bucket"
        else {"radiant_net_worth": 42_000, "dire_net_worth": 47_000}
    )
    payload = LiveObservation(
        raybet_match_id="42",
        captured_at_utc=datetime.now(timezone.utc),
        source_frame_ref="frame.jpg",
        comeback_state=ComebackState(
            status="available",
            source="vision_hud",
            confidence=0.95,
            radiant_kills=18,
            dire_kills=25,
            **economy,
            unavailable_reason=None,
        ),
    ).model_dump(mode="json")
    payload["schema_version"] = 3

    from live_betting.vision import parse_observation

    state = parse_observation(payload).comeback_state
    assert state.status == "available"
    assert state.radiant_kills == 18
    assert state.dire_kills == 25
    assert state.radiant_net_worth is None
    assert state.dire_net_worth is None
    assert state.net_worth_advantage_side is None
    assert state.net_worth_advantage_min is None
    assert state.net_worth_advantage_max is None


@pytest.mark.parametrize("schema_version", [1, 2])
def test_legacy_observation_cannot_claim_available_comeback_state(
    schema_version: int,
) -> None:
    payload = LiveObservation(
        raybet_match_id="42",
        captured_at_utc=datetime.now(timezone.utc),
        source_frame_ref="frame.jpg",
        comeback_state=ComebackState(
            status="available",
            source="vision_hud",
            confidence=0.95,
            radiant_kills=18,
            dire_kills=25,
            unavailable_reason=None,
        ),
    ).model_dump(mode="json")
    payload["schema_version"] = schema_version

    from live_betting.vision import parse_observation

    state = parse_observation(payload).comeback_state
    assert state.status == "unavailable"
    assert state.unavailable_reason == "legacy_schema_live_state_unavailable"


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
