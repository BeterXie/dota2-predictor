from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import cv2
import httpx
import numpy as np
import pytest

from contracts.live_observation import ComebackState, DraftPlayerNames, LiveObservation
from live_betting.vision import read_jsonl
from scripts.build_hero_features import build_hero_features
from scripts.fetch_hero_portraits import valid_portrait_bytes
from scripts.supervise_raybet_streams import _capture_heartbeat, _heartbeat_diagnostics
from scripts.watch_raybet_stream import (
    _write_capture_heartbeat,
    allow_live_hud_tracking,
    allow_target_draft_tracking,
)
from vision.clock_reader import ClockReader, ClockReading
from vision.hero_recognizer import (
    DraftReading,
    DraftTracker,
    HeroRecognizer,
    HeroSlotDiagnostic,
)
from vision.hud_reader import HudFrameReading, HudReader
from vision.image_features import (
    MAX_VARIANTS_PER_HERO,
    color_histogram,
    compute_phash,
)
from vision.layout_selector import (
    LayoutSelection,
    draft_lineup_complete,
    select_broadcast_layout,
)
from vision.layouts import (
    BroadcastLayout,
    EPL_MASTERS_DRAFT,
    EPL_MASTERS_LIVE,
    EPL_S39_LIVE,
    NormalizedRegion,
    STANDARD_DOTA_HUD,
    WXC_GOTF_2026_LIVE,
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
from vision.team_side import (
    MAX_TEAM_LOGO_BYTES,
    TeamSideRecognizer,
    TeamSideTracker,
    _validate_team_logo_url,
)


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


def _synthetic_epl_draft(*, complete: bool = True) -> np.ndarray:
    image = np.full((1080, 1920, 3), 70, dtype=np.uint8)
    cyan = (255, 255, 0)
    for region in (
        EPL_MASTERS_DRAFT.draft_banner,
        NormalizedRegion(1756 / 1920, 44 / 1080, 1781 / 1920, 101 / 1080),
        NormalizedRegion(110 / 1920, 190 / 1080, 120 / 1920, 670 / 1080),
        NormalizedRegion(1810 / 1920, 190 / 1080, 1820 / 1920, 670 / 1080),
    ):
        region.crop(image)[:] = cyan
    for index, region in enumerate(
        EPL_MASTERS_DRAFT.radiant_heroes + EPL_MASTERS_DRAFT.dire_heroes
    ):
        crop = region.crop(image)
        crop[:] = np.random.default_rng(1000 + index).integers(
            0, 255, crop.shape, dtype=np.uint8
        )
    if complete:
        for region in EPL_MASTERS_DRAFT.draft_completion_cyan_regions:
            region.crop(image)[:] = cyan
    return image


def _synthetic_standard_hud() -> np.ndarray:
    image = np.full((1080, 1920, 3), 70, dtype=np.uint8)
    cv2.rectangle(image, (422, 0), (1497, 64), (20, 20, 20), -1)
    for left in (856, 904, 1020, 1068):
        cv2.rectangle(image, (left, 14), (left + 10, 42), (255, 255, 255), -1)
    for index, region in enumerate(
        STANDARD_DOTA_HUD.radiant_heroes + STANDARD_DOTA_HUD.dire_heroes
    ):
        crop = region.crop(image)
        noise = np.random.default_rng(index).integers(0, 255, crop.shape, dtype=np.uint8)
        crop[:] = noise
    for region in (
        STANDARD_DOTA_HUD.radiant_team_logo,
        STANDARD_DOTA_HUD.dire_team_logo,
    ):
        assert region is not None
        crop = region.crop(image)
        crop[:] = np.random.default_rng(crop.shape[1]).integers(
            0, 255, crop.shape, dtype=np.uint8
        )
    return image


def _synthetic_epl_s39_hud() -> np.ndarray:
    image = _synthetic_standard_hud()
    plate = NormalizedRegion(0.735, 0.940, 0.860, 1.000).crop(image)
    plate[:] = 12
    plate[:, ::8] = (255, 255, 0)
    return image


def _synthetic_wxc_gotf_hud() -> np.ndarray:
    image = np.full((1080, 1920, 3), 70, dtype=np.uint8)
    NormalizedRegion(0.241, 0.002, 0.2455, 0.052).crop(image)[:] = (0, 255, 255)
    NormalizedRegion(0.755, 0.002, 0.759, 0.052).crop(image)[:] = (68, 0, 1)
    for region in (
        NormalizedRegion(1633 / 1920, 653 / 1080, 1639 / 1920, 812 / 1080),
        NormalizedRegion(1633 / 1920, 653 / 1080, 1.0, 658 / 1080),
        NormalizedRegion(1633 / 1920, 808 / 1080, 1.0, 812 / 1080),
    ):
        region.crop(image)[:] = (255, 0, 255)
    for region in (
        WXC_GOTF_2026_LIVE.clock,
        WXC_GOTF_2026_LIVE.radiant_kills,
        WXC_GOTF_2026_LIVE.dire_kills,
    ):
        region.crop(image)[:] = 255
    for index, region in enumerate(
        WXC_GOTF_2026_LIVE.radiant_heroes + WXC_GOTF_2026_LIVE.dire_heroes
    ):
        crop = region.crop(image)
        crop[:] = np.random.default_rng(index).integers(0, 255, crop.shape, dtype=np.uint8)
    return image


def test_epl_layout_requires_the_complete_hud_geometry() -> None:
    image = _synthetic_epl_hud()
    selection = select_broadcast_layout(image)

    assert selection.layout == EPL_MASTERS_LIVE
    assert selection.confidence >= 0.9

    image[:, 1440:1470] = 70
    unsupported = select_broadcast_layout(image)
    assert unsupported.layout is None
    assert not unsupported.supported


def test_epl_draft_layout_requires_its_complete_geometry() -> None:
    image = _synthetic_epl_draft()

    selection = select_broadcast_layout(image)

    assert selection.layout == EPL_MASTERS_DRAFT
    assert selection.confidence >= 0.9
    assert classify_screen_state(image, EPL_MASTERS_DRAFT)[0] == "draft"

    NormalizedRegion(
        1810 / 1920, 190 / 1080, 1820 / 1920, 670 / 1080
    ).crop(image)[:] = 70
    assert select_broadcast_layout(image).layout != EPL_MASTERS_DRAFT


def test_epl_live_and_plain_frames_do_not_select_the_draft_layout() -> None:
    assert select_broadcast_layout(_synthetic_epl_hud()).layout == EPL_MASTERS_LIVE
    plain = np.full((1080, 1920, 3), 70, dtype=np.uint8)
    assert select_broadcast_layout(plain).layout != EPL_MASTERS_DRAFT


def test_epl_draft_layout_uses_vertical_card_crops() -> None:
    radiant = EPL_MASTERS_DRAFT.radiant_heroes
    dire = EPL_MASTERS_DRAFT.dire_heroes

    assert len(radiant) == len(dire) == 5
    assert len(EPL_MASTERS_DRAFT.draft_completion_cyan_regions) == 10
    assert (
        radiant[0].left,
        radiant[0].top,
        radiant[0].right,
        radiant[0].bottom,
    ) == pytest.approx((39 / 1920, 778 / 1080, 173 / 1920, 951 / 1080))
    assert (
        dire[-1].left,
        dire[-1].top,
        dire[-1].right,
        dire[-1].bottom,
    ) == pytest.approx((1747 / 1920, 778 / 1080, 1881 / 1920, 951 / 1080))


def test_epl_draft_requires_all_final_player_nameplates_before_recognition() -> None:
    complete = _synthetic_epl_draft()
    incomplete = _synthetic_epl_draft(complete=False)

    assert draft_lineup_complete(complete, EPL_MASTERS_DRAFT)
    assert not draft_lineup_complete(incomplete, EPL_MASTERS_DRAFT)


def test_hud_reader_reads_heroes_from_a_draft_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = DraftReading(tuple(range(1, 6)), tuple(range(6, 11)), 0.95)

    class HeroReader:
        calls = 0

        def read(self, _image: np.ndarray) -> DraftReading:
            self.calls += 1
            return expected

    heroes = HeroReader()
    reader = HudReader(use_ocr=False)
    monkeypatch.setattr(
        "vision.hud_reader.select_broadcast_layout",
        lambda _image: LayoutSelection(EPL_MASTERS_DRAFT, 1.0, True),
    )
    monkeypatch.setattr(
        reader,
        "_profile",
        lambda _layout: SimpleNamespace(
            heroes=heroes,
            player_names=SimpleNamespace(
                read=lambda _image: DraftPlayerNames.unavailable("test_double"),
                bind_heroes=lambda reading, *_hero_ids: reading,
            ),
            hero_features_ready=True,
        ),
    )

    reading = reader.read(_synthetic_epl_draft())

    assert reading.screen_state == "draft"
    assert reading.draft == expected
    assert heroes.calls == 1


def test_hud_reader_skips_unfinished_draft_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HeroReader:
        calls = 0

        def read(self, _image: np.ndarray) -> DraftReading:
            self.calls += 1
            return DraftReading(tuple(range(1, 6)), tuple(range(6, 11)), 0.95)

    heroes = HeroReader()
    reader = HudReader(use_ocr=False)
    monkeypatch.setattr(
        "vision.hud_reader.select_broadcast_layout",
        lambda _image: LayoutSelection(EPL_MASTERS_DRAFT, 1.0, True),
    )
    monkeypatch.setattr(
        reader,
        "_profile",
        lambda _layout: SimpleNamespace(
            heroes=heroes,
            player_names=SimpleNamespace(
                read=lambda _image: DraftPlayerNames.unavailable("test_double"),
                bind_heroes=lambda reading, *_hero_ids: reading,
            ),
            hero_features_ready=True,
        ),
    )

    reading = reader.read(_synthetic_epl_draft(complete=False))

    assert reading.screen_state == "draft"
    assert reading.draft.radiant_hero_ids == ()
    assert reading.draft.dire_hero_ids == ()
    assert heroes.calls == 0


def test_hud_reader_requires_promoted_features_for_a_dedicated_draft_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "vision.hud_reader.promoted_profile_feature_path",
        lambda _profile_id: None,
    )

    profile = HudReader(use_ocr=False)._profile(EPL_MASTERS_DRAFT)

    assert not profile.hero_features_ready


def test_standard_layout_requires_its_own_complete_hud_geometry() -> None:
    image = _synthetic_standard_hud()

    selection = select_broadcast_layout(image)

    assert selection.layout == STANDARD_DOTA_HUD
    assert selection.supported
    image[16:45, 904:915] = 70
    assert not select_broadcast_layout(image).supported


def test_epl_s39_layout_uses_its_live_hud_and_brand_plate() -> None:
    image = _synthetic_epl_s39_hud()
    selection = select_broadcast_layout(image)

    assert selection.layout == EPL_S39_LIVE
    assert selection.confidence >= 0.9
    assert EPL_S39_LIVE.draft_recognition_max_clock_seconds is None

    NormalizedRegion(0.735, 0.940, 0.860, 1.000).crop(image)[:] = 70
    assert select_broadcast_layout(image).layout != EPL_S39_LIVE


def test_wxc_gotf_layout_requires_its_tournament_caster_geometry() -> None:
    image = _synthetic_wxc_gotf_hud()

    selection = select_broadcast_layout(image)

    assert selection.layout == WXC_GOTF_2026_LIVE
    assert selection.confidence >= 0.9
    NormalizedRegion(1633 / 1920, 653 / 1080, 1.0, 658 / 1080).crop(image)[:] = 70
    assert select_broadcast_layout(image).layout != WXC_GOTF_2026_LIVE


def test_wxc_gotf_layout_ignores_the_rotating_sponsor_plate() -> None:
    image = _synthetic_wxc_gotf_hud()

    NormalizedRegion(0.000, 0.692, 0.147, 0.745).crop(image)[:] = (0, 255, 255)

    assert select_broadcast_layout(image).layout == WXC_GOTF_2026_LIVE


def test_wxc_gotf_layout_uses_calibrated_portrait_insets() -> None:
    radiant = WXC_GOTF_2026_LIVE.radiant_heroes
    dire = WXC_GOTF_2026_LIVE.dire_heroes

    assert (radiant[0].left, radiant[0].top, radiant[0].right, radiant[0].bottom) == pytest.approx(
        (0.288020833, 0.002777778, 0.316145833, 0.038888889)
    )
    assert (dire[0].left, dire[0].top, dire[0].right, dire[0].bottom) == pytest.approx(
        (0.5546875, 0.002777778, 0.5828125, 0.038888889)
    )


def test_epl_layout_uses_portrait_insets_for_hero_recognition() -> None:
    radiant = EPL_MASTERS_LIVE.radiant_heroes
    dire = EPL_MASTERS_LIVE.dire_heroes
    assert (
        radiant[0].left,
        radiant[0].top,
        radiant[0].right,
        radiant[0].bottom,
    ) == pytest.approx((
        0.288,
        0.002,
        0.3155,
        0.043,
    ))
    assert (
        radiant[-1].left,
        radiant[-1].top,
        radiant[-1].right,
        radiant[-1].bottom,
    ) == pytest.approx((
        0.414,
        0.002,
        0.4415,
        0.043,
    ))
    assert (
        dire[0].left,
        dire[0].top,
        dire[0].right,
        dire[0].bottom,
    ) == pytest.approx((
        0.558,
        0.002,
        0.5855,
        0.043,
    ))
    assert (
        dire[-1].left,
        dire[-1].top,
        dire[-1].right,
        dire[-1].bottom,
    ) == pytest.approx((
        0.684,
        0.002,
        0.7115,
        0.043,
    ))
    assert EPL_MASTERS_LIVE.draft_recognition_max_clock_seconds is None


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
    expected_draft = DraftReading(
        (54, 36, 96, 121, 100),
        (106, 19, 85, 123, 119),
        0.91,
    )
    monkeypatch.setattr(profile.heroes, "read", lambda image: expected_draft)
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
    assert reading.draft == expected_draft


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


def _write_features(
    path: Path,
    images: list[np.ndarray],
    *,
    ids: list[int] | None = None,
    variant_names: list[str] | None = None,
) -> None:
    payload: dict[str, np.ndarray] = {
        "ids": np.asarray(ids or range(1, len(images) + 1), dtype=np.int32),
        "hashes": np.asarray(
            [compute_phash(image) for image in images], dtype=np.uint8
        ),
        "histograms": np.asarray(
            [color_histogram(image) for image in images], dtype=np.float32
        ),
        "thumbnails": np.asarray(
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
    }
    if variant_names is not None:
        payload["variant_names"] = np.asarray(variant_names)
    np.savez_compressed(path, **payload)


def test_hud_reader_uses_promoted_portrait_variants_for_detected_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one = _portrait((20, 90, 180), "1")
    one_arcana = _portrait((30, 170, 70), "A")
    two = _portrait((170, 60, 20), "2")
    promoted = tmp_path / "epl-promoted.npz"
    _write_features(
        promoted,
        [one, one_arcana, two],
        ids=[1, 1, 2],
        variant_names=["1", "1__arcana", "2"],
    )
    monkeypatch.setattr(
        "vision.hud_reader.promoted_profile_feature_path",
        lambda profile_id: promoted if profile_id == EPL_MASTERS_LIVE.name else None,
    )

    profile = HudReader(use_ocr=False)._profile(EPL_MASTERS_LIVE)

    assert profile.heroes.ids.tolist() == [1, 2]
    assert profile.heroes.variant_names.tolist() == ["1", "1__arcana", "2"]
    assert profile.heroes.recognize_crop(one_arcana).hero_id == 1


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
    def __init__(
        self,
        content: bytes,
        content_type: str = "image/png",
        *,
        status_code: int = 200,
        location: str | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if location is not None:
            self.headers["location"] = location

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self):
        yield self.content


class FakeHttpClient:
    responses: dict[str, bytes | Exception | FakeResponse] = {}
    requested: list[str] = []

    def __init__(self, **_: object) -> None:
        pass

    def __enter__(self) -> "FakeHttpClient":
        type(self).requested = []
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def stream(self, method: str, url: str) -> FakeResponse:
        assert method == "GET"
        type(self).requested.append(url)
        response = type(self).responses[url]
        if isinstance(response, Exception):
            raise response
        return (
            response
            if isinstance(response, FakeResponse)
            else FakeResponse(response)
        )


class _LogoQueryResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class _LogoConnection:
    def __init__(self, *, with_team_logos: bool) -> None:
        self.with_team_logos = with_team_logos

    def execute(
        self,
        query: str,
        parameters: tuple[object, ...] = (),
    ) -> _LogoQueryResult:
        del parameters
        if "FROM raybet_matches" in query:
            return _LogoQueryResult([
                ("VP", "Xtreme Gaming", json.dumps(_raw_logo_payload()))
            ])
        if "FROM teams" in query and self.with_team_logos:
            return _LogoQueryResult([
                (
                    "Virtus.pro",
                    "VP",
                    "https://cdn.cloudflare.steamstatic.com/vp.png",
                ),
                (
                    "Xtreme Gaming",
                    "XG",
                    "https://cdn.cloudflare.steamstatic.com/xg.png",
                ),
            ])
        return _LogoQueryResult([])


class _LogoStore:
    def __init__(self, *, with_team_logos: bool) -> None:
        self.connection = _LogoConnection(with_team_logos=with_team_logos)

    def __enter__(self) -> "_LogoStore":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _raw_logo_payload() -> dict[str, object]:
    return {
        "team": [
            {"pos": 2, "team_logo": "/file/fallback-two.png"},
            {"pos": 1, "team_logo": "file/fallback-one.png"},
        ]
    }


def _patch_logo_store(monkeypatch: pytest.MonkeyPatch, *, with_team_logos: bool) -> None:
    store = _LogoStore(with_team_logos=with_team_logos)
    monkeypatch.setattr(
        "vision.team_side.LiveBettingStore",
        lambda database_url: store,
    )


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


def test_clock_tracker_normalizes_unsigned_pregame_countdown() -> None:
    tracker = MapStateTracker(confirmations=2, pause_frames=2)

    assert tracker.update(ClockReading(3, 0.95, "0:03")) is None
    pregame = tracker.update(ClockReading(2, 0.95, "0:02"))
    assert pregame is not None and pregame.seconds == -2
    pregame = tracker.update(ClockReading(1, 0.95, "0:01"))
    assert pregame is not None and pregame.seconds == -1
    horn = tracker.update(ClockReading(0, 0.95, "0:00"))
    assert horn is not None and horn.seconds == 0

    assert tracker.update(ClockReading(1, 0.95, "0:01")) is None
    live = tracker.update(ClockReading(2, 0.95, "0:02"))
    assert live is not None and live.seconds == 2
    assert not tracker.frozen


def test_clock_tracker_still_freezes_a_late_regression() -> None:
    tracker = MapStateTracker(confirmations=2, pause_frames=2)
    assert tracker.update(ClockReading(100, 0.95, "1:40")) is None
    assert tracker.update(ClockReading(101, 0.95, "1:41")) is not None

    assert tracker.update(ClockReading(90, 0.95, "1:30")) is None
    assert tracker.update(ClockReading(89, 0.95, "1:29")) is None
    assert tracker.frozen
    assert tracker.update(ClockReading(102, 0.95, "1:42")) is None


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


def test_replay_gate_pauses_draft_but_resets_live_hud_trackers() -> None:
    clock = MapStateTracker(confirmations=2)
    clock.reset_map(1)
    scoreboard = ScoreboardTracker(confirmations=2)
    advantage = NetWorthAdvantageTracker(confirmations=2)
    draft = DraftTracker(confirmations=2)
    clock.update(ClockReading(100, 0.95, "1:40"))
    assert clock.update(ClockReading(101, 0.95, "1:41")) is not None
    scoreboard.update(ScoreboardReading(8, 5, 0.95))
    assert scoreboard.update(ScoreboardReading(8, 5, 0.95)) is not None
    advantage.update(NetWorthAdvantageReading("radiant", 1_000, 1_999, 0.95))
    assert (
        advantage.update(
            NetWorthAdvantageReading("radiant", 1_000, 1_999, 0.95)
        )
        is not None
    )
    complete_draft = DraftReading((1, 2, 3, 4, 5), (6, 7, 8, 9, 10), 0.95)
    assert draft.update(complete_draft, observed_at=0.0) is None
    assert draft.update(complete_draft, observed_at=3.0) is None
    assert draft.update(complete_draft, observed_at=6.0) is not None

    assert not allow_live_hud_tracking(
        ReplayGateReading("replay", 0.99),
        map_number=1,
        clock_tracker=clock,
        scoreboard_tracker=scoreboard,
        advantage_tracker=advantage,
        draft_tracker=draft,
    )
    assert clock.update(ClockReading(102, 0.95, "1:42")) is None
    assert scoreboard.update(ScoreboardReading(8, 5, 0.95)) is None
    assert (
        advantage.update(
            NetWorthAdvantageReading("radiant", 1_000, 1_999, 0.95)
        )
        is None
    )
    assert draft.update(complete_draft, observed_at=9.0) is not None


def test_legacy_draft_tracking_does_not_require_target_team_confirmation() -> None:
    assert allow_target_draft_tracking(radiant_team_side=None)


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
            [("PLAYOFFS", 0.98), ("QUARTERFINAL", 0.99)],
            ReplayGateReading("untrusted", 0.99, None),
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
    fixture_layout = replace(
        STANDARD_DOTA_HUD,
        broadcast_status=FULL,
        requires_geometry_confirmation=False,
        live_broadcast_marker_sets=(("playoffs", "quarterfinal"),),
    )
    reader = ScoreboardReader(fixture_layout)
    highlight_image = cv2.imread(str(highlight))
    live_image = cv2.imread(str(live))
    assert highlight_image is not None and highlight_image.shape[:2] == (302, 307)
    assert live_image is not None and live_image.shape[:2] == (302, 307)

    assert reader.read_replay_gate(highlight_image).status == "replay"
    assert reader.read_replay_gate(live_image).status == "live"


def test_standard_live_gate_requires_hud_geometry_scoreboard_and_clock() -> None:
    image = _synthetic_standard_hud()
    reader = ScoreboardReader(STANDARD_DOTA_HUD, use_ocr=False)

    class PositionedOcr:
        replay = False

        def __call__(self, image, **kwargs):
            del image, kwargs
            rows = [
                [[[280, 10], [305, 10], [305, 30], [280, 30]], "6", 0.99],
                [[[445, 10], [465, 10], [465, 30], [445, 30]], "3", 0.98],
                [[[326, 23], [365, 23], [365, 36], [326, 36]], "6:03", 0.97],
            ]
            if self.replay:
                rows.append(
                    [[[100, 65], [220, 65], [220, 82], [100, 82]], "REPLAY", 0.99]
                )
            return rows, None

    ocr = PositionedOcr()
    reader.ocr = ocr
    assert reader.read_replay_gate(image).status == "live"

    ocr.replay = True
    reader._strip_cache_image = None
    assert reader.read_replay_gate(image).status == "replay"

    reader.ocr = lambda image, **kwargs: (
        [[[[326, 23], [365, 23], [365, 36], [326, 36]], "6:03", 0.97]],
        None,
    )
    reader._strip_cache_image = None
    assert reader.read_replay_gate(image).status == "untrusted"


def test_geometry_live_gate_accepts_clock_text_with_nonstandard_separator() -> None:
    image = _synthetic_standard_hud()
    reader = ScoreboardReader(STANDARD_DOTA_HUD, use_ocr=False)

    class PositionedOcr:
        def __call__(self, image, **kwargs):
            del image, kwargs
            return (
                [
                    [[[280, 10], [305, 10], [305, 30], [280, 30]], "6", 0.99],
                    [[[445, 10], [465, 10], [465, 30], [445, 30]], "3", 0.98],
                    [[[326, 23], [365, 23], [365, 36], [326, 36]], "13.27", 0.97],
                ],
                None,
            )

    reader.ocr = PositionedOcr()

    assert reader.read_replay_gate(image).status == "live"


def test_hud_diagnostics_reports_the_first_blocking_gate() -> None:
    unavailable = HudFrameReading(
        LayoutSelection(None, 0.4, False, "unsupported_layout"),
        "unknown",
        0.0,
        ReplayGateReading("untrusted", 0.0),
        ClockReading(None, 0.0, None),
        ScoreboardReading(None, None, 0.0),
        NetWorthAdvantageReading(None, None, None, 0.0),
        DraftReading((), (), 0.0),
    )
    assert unavailable.diagnostics.blocker_code == "unsupported_layout"

    partial = HudFrameReading(
        LayoutSelection(EPL_MASTERS_LIVE, 0.95, True),
        "game",
        0.98,
        ReplayGateReading("live", 0.97),
        ClockReading(400, 0.97, "6:40"),
        ScoreboardReading(8, 5, 0.96),
        NetWorthAdvantageReading(None, None, None, 0.0),
        DraftReading((), (), 0.8, _draft_slot_diagnostics(set(range(8)))),
    )
    diagnostics = partial.diagnostics
    assert partial.core_hud_ready
    assert not partial.comeback_state_ready
    assert diagnostics.blocker_code == "net_worth_advantage_unconfirmed"
    assert diagnostics.core_hud_ready
    assert not diagnostics.draft_confirmed

    ready_except_draft = diagnostics.with_confirmations(net_worth_confirmed=True)
    assert ready_except_draft.blocker_code == "draft_unconfirmed"


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"screen_state": "unknown"}, "screen_not_game"),
        ({"replay_gate_status": "replay"}, "replay_detected"),
        ({"replay_gate_status": "untrusted"}, "replay_gate_untrusted"),
        ({"clock_confirmed": False}, "clock_unconfirmed"),
        ({"scoreboard_confirmed": False}, "kill_score_unconfirmed"),
        ({"net_worth_confirmed": False}, "net_worth_advantage_unconfirmed"),
        ({"draft_confirmed": False}, "draft_unconfirmed"),
        ({"team_side_confirmed": False}, "team_side_unconfirmed"),
        ({}, "ready"),
    ],
)
def test_hud_diagnostics_blocker_priority(
    updates: dict[str, object],
    expected: str,
) -> None:
    reading = HudFrameReading(
        LayoutSelection(EPL_MASTERS_LIVE, 0.95, True),
        "game",
        0.98,
        ReplayGateReading("live", 0.97),
        ClockReading(400, 0.97, "6:40"),
        ScoreboardReading(8, 5, 0.96),
        NetWorthAdvantageReading("radiant", 1_000, 1_999, 0.95),
        DraftReading((1, 2, 3, 4, 5), (6, 7, 8, 9, 10), 0.95),
    )
    ready = reading.diagnostics.with_confirmations(team_side_confirmed=True)
    diagnostics = replace(ready, **updates).with_confirmations()
    assert diagnostics.blocker_code == expected


def test_capture_heartbeat_v2_and_v1_are_read_by_supervisor(tmp_path: Path) -> None:
    reading = HudFrameReading(
        LayoutSelection(EPL_MASTERS_LIVE, 0.95, True),
        "game",
        0.98,
        ReplayGateReading("live", 0.97),
        ClockReading(400, 0.97, "6:40"),
        ScoreboardReading(8, 5, 0.96),
        NetWorthAdvantageReading(None, None, None, 0.0),
        DraftReading((), (), 0.8, _draft_slot_diagnostics(set(range(8)))),
    )
    output = tmp_path / "42.jsonl"
    _write_capture_heartbeat(
        output,
        match_id="42",
        map_number=2,
        captured_at=datetime.now(timezone.utc),
        capture_status="capturing_partial",
        diagnostics=reading.diagnostics,
        draft_slot_statuses=DraftTracker().slot_statuses,
        retention={
            "map_sample_count": 4,
            "retained_sample_count": 4,
            "retained_sample_encoded_bytes": 4096,
            "last_retained_at": datetime.now(timezone.utc).isoformat(),
            "last_frame_ref": "vision-frame:sha256:" + "a" * 64,
            "last_manifest_path": str(tmp_path / "frames.jsonl"),
        },
    )

    parsed_v2 = _capture_heartbeat(output.with_suffix(".heartbeat.json"), "42")
    assert parsed_v2 is not None
    diagnostics = _heartbeat_diagnostics(parsed_v2)
    assert diagnostics["blocker_code"] == "net_worth_advantage_unconfirmed"
    assert diagnostics["map_number"] == 2
    assert diagnostics["layout_profile"] == "epl_masters_live_1080p"
    assert diagnostics["layout_supported"] is True
    assert diagnostics["clock_seconds"] == 400
    assert diagnostics["radiant_kills"] == 8
    assert diagnostics["dire_kills"] == 5
    assert diagnostics["core_hud_ready"] is True
    assert diagnostics["net_worth_confirmed"] is False
    assert diagnostics["strategy_ready"] is False
    assert diagnostics["retention"]["status"] == "complete"
    assert diagnostics["retention"]["map_sample_count"] == 4
    assert diagnostics["retention"]["retained_sample_count"] == 4
    assert diagnostics["retention"]["automatic_deletion_enabled"] is False
    assert diagnostics["radiant_hero_count"] == 5
    assert diagnostics["dire_hero_count"] == 3
    assert len(parsed_v2["draft"]["slots"]) == 10
    assert parsed_v2["draft"]["slots"][0]["best"] == {
        "hero_id": 1,
        "variant": None,
        "hero_variant_count": 0,
        "combined": 0.8,
        "channels": None,
    }
    assert len(parsed_v2["draft"]["tracker_slots"]) == 10
    assert parsed_v2["draft"]["tracker_slots"][0]["state"] == "observing"
    assert diagnostics["draft_failed_slots"] == [
        {
            "side": "dire",
            "slot": 4,
            "best_hero_id": 9,
            "best_variant": None,
            "hero_variant_count": 0,
            "best_score": 0.8,
            "second_score": 0.7,
            "margin": 0.1,
            "reason": "low_score",
        },
        {
            "side": "dire",
            "slot": 5,
            "best_hero_id": 10,
            "best_variant": None,
            "hero_variant_count": 0,
            "best_score": 0.8,
            "second_score": 0.7,
            "margin": 0.1,
            "reason": "low_score",
        },
    ]

    heartbeat_path = output.with_suffix(".heartbeat.json")
    suggested_v2 = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    suggested_v2.pop("team_side")
    suggested_v2.pop("strategy_ready")
    heartbeat_path.write_text(json.dumps(suggested_v2), encoding="utf-8")
    assert _capture_heartbeat(heartbeat_path, "42") is not None

    heartbeat_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "match_id": "42",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "capture_status": "capturing_unrecognized",
                "layout_profile": "standard_dota_hud_1080p",
                "layout_confidence": 0.5,
                "screen_state": "unknown",
                "replay_gate_status": None,
            }
        ),
        encoding="utf-8",
    )
    parsed_v1 = _capture_heartbeat(heartbeat_path, "42")
    assert parsed_v1 is not None
    assert _heartbeat_diagnostics(parsed_v1)["layout_profile"] == "standard_dota_hud_1080p"


def test_exact_portrait_is_recognized(tmp_path: Path) -> None:
    one = _portrait((20, 90, 180), "1")
    two = _portrait((170, 60, 20), "2")
    features = tmp_path / "features.npz"
    _write_features(features, [one, two])
    reading = HeroRecognizer(features, HERO_LAYOUT).recognize_crop(one)
    assert reading.hero_id == 1
    assert reading.confidence >= 0.62


def test_portrait_variants_compete_between_heroes(tmp_path: Path) -> None:
    one = _portrait((20, 90, 180), "1")
    one_arcana = _portrait((30, 170, 70), "A")
    two = _portrait((170, 60, 20), "2")
    features = tmp_path / "features.npz"
    _write_features(
        features,
        [one_arcana, one, two],
        ids=[1, 1, 2],
        variant_names=["1__arcana", "1", "2"],
    )

    recognizer = HeroRecognizer(features, HERO_LAYOUT)
    scored = recognizer._score_crop(one_arcana)
    reading = recognizer.recognize_crop(one_arcana)

    assert recognizer.ids.tolist() == [1, 2]
    assert scored is not None and scored.combined.shape == (2,)
    assert scored.variants.tolist() == ["1__arcana", "2"]
    assert scored.variant_counts.tolist() == [2, 1]
    assert reading.hero_id == 1
    assert reading.margin >= 0.025


def test_recognizer_rejects_untraceable_or_excessive_variants(
    tmp_path: Path,
) -> None:
    one = _portrait((20, 90, 180), "1")
    two = _portrait((170, 60, 20), "2")
    features = tmp_path / "features.npz"

    _write_features(features, [one, one.copy(), two], ids=[1, 1, 2])
    with pytest.raises(ValueError, match="require variant_names"):
        HeroRecognizer(features, HERO_LAYOUT)

    _write_features(
        features,
        [one, two],
        ids=[1, 2],
        variant_names=["1__death", "2"],
    )
    with pytest.raises(ValueError, match="missing its base portrait"):
        HeroRecognizer(features, HERO_LAYOUT)

    names = ["1"] + [
        f"1__calibration_{index:012x}"
        for index in range(MAX_VARIANTS_PER_HERO)
    ] + ["2"]
    _write_features(
        features,
        [one.copy() for _ in range(MAX_VARIANTS_PER_HERO + 1)] + [two],
        ids=[1] * (MAX_VARIANTS_PER_HERO + 1) + [2],
        variant_names=names,
    )
    with pytest.raises(ValueError, match="template limit"):
        HeroRecognizer(features, HERO_LAYOUT)


def test_feature_asset_can_be_rebuilt_inside_predictor(tmp_path: Path) -> None:
    source = tmp_path / "heroes"
    source.mkdir()
    cv2.imwrite(str(source / "1.png"), _portrait((20, 90, 180), "1"))
    cv2.imwrite(str(source / "1__death.png"), _portrait((20, 90, 180), "D"))
    cv2.imwrite(str(source / "2.png"), _portrait((170, 60, 20), "2"))
    (source / "heroes.json").write_text(
        json.dumps({"1": {"id": 1}, "2": {"id": 2}}), encoding="utf-8"
    )
    output = tmp_path / "hero_features.npz"

    assert build_hero_features(source, output) == 2
    with np.load(output) as features:
        assert features["ids"].tolist() == [1, 1, 2]
        assert features["variant_names"].tolist() == ["1", "1__death", "2"]
        assert features["hashes"].shape == (3, 64)


@pytest.mark.parametrize("variant_name", ["random1", "better-final-new"])
def test_feature_build_rejects_uncontrolled_variant_names(
    tmp_path: Path, variant_name: str
) -> None:
    source = tmp_path / "heroes"
    source.mkdir()
    cv2.imwrite(str(source / "1.png"), _portrait((20, 90, 180), "1"))
    cv2.imwrite(
        str(source / f"1__{variant_name}.png"),
        _portrait((20, 90, 180), "V"),
    )
    (source / "heroes.json").write_text(
        json.dumps({"1": {"id": 1}}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="invalid hero portrait filename"):
        build_hero_features(source, tmp_path / "features.npz")


def test_feature_build_limits_templates_per_hero(tmp_path: Path) -> None:
    source = tmp_path / "heroes"
    source.mkdir()
    names = ["1"] + [
        f"1__calibration_{index:012x}"
        for index in range(MAX_VARIANTS_PER_HERO)
    ]
    for name in names:
        cv2.imwrite(str(source / f"{name}.png"), _portrait((20, 90, 180), name))
    (source / "heroes.json").write_text(
        json.dumps({"1": {"id": 1}}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="variant limit exceeded"):
        build_hero_features(source, tmp_path / "features.npz")


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

    cv2.imwrite(str(source / "2__death.png"), _portrait((170, 60, 20), "D"))
    with pytest.raises(ValueError, match="missing base portraits"):
        build_hero_features(source, output)


def test_portrait_download_validation_decodes_image_content() -> None:
    assert valid_portrait_bytes(_png(_portrait((20, 90, 180), "1")))
    assert not valid_portrait_bytes(b"x" * 1024)


def test_draft_needs_temporal_agreement() -> None:
    tracker = DraftTracker(confirmations=2)
    reading = DraftReading((1, 2, 3, 4, 5), (6, 7, 8, 9, 10), 0.7)
    assert tracker.update(reading, observed_at=0.0) is None
    assert tracker.update(reading, observed_at=3.0) is None
    confirmed = tracker.update(reading, observed_at=6.0)
    assert confirmed is not None and confirmed.confidence == 0.7
    changed = DraftReading((1, 2, 3, 4, 6), (5, 7, 8, 9, 10), 0.95)
    assert tracker.update(changed, observed_at=9.0) == confirmed
    tracker.reset()
    assert tracker.update(changed, observed_at=12.0) is None
    assert tracker.update(changed, observed_at=15.0) is None
    switched = tracker.update(changed, observed_at=18.0)
    assert switched is not None
    assert switched.radiant_hero_ids == changed.radiant_hero_ids
    assert switched.confidence == 0.95


def test_hero_recognizer_reports_each_failed_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recognizer = HeroRecognizer.__new__(HeroRecognizer)
    recognizer.ids = np.arange(1, 12, dtype=np.int32)
    recognizer.layout = HERO_LAYOUT
    rows: list[np.ndarray] = []
    for slot in range(10):
        scores = np.full(11, 0.1, dtype=np.float64)
        scores[slot] = 0.8
        scores[10] = 0.7
        rows.append(scores)
    rows[2][10] = 0.79
    rows[6] = np.full(11, 0.1, dtype=np.float64)
    rows[6][6] = 0.55
    rows[6][10] = 0.5
    score_rows = iter(
        SimpleNamespace(
            combined=row,
            phash=row,
            histogram=row,
            pixel=row,
            variants=np.asarray([str(hero_id) for hero_id in range(1, 12)]),
            variant_counts=np.ones(11, dtype=np.int32),
            crop_hash=f"{index:016x}",
        )
        for index, row in enumerate(rows)
    )
    monkeypatch.setattr(recognizer, "_score_crop", lambda image: next(score_rows))

    reading = recognizer.read(np.zeros((100, 100, 3), dtype=np.uint8))

    assert reading.radiant_hero_ids == ()
    assert reading.dire_hero_ids == ()
    assert reading.recognized_slot_count == 8
    failed = [item for item in reading.slot_diagnostics if not item.accepted]
    assert [(item.side, item.slot, item.reason) for item in failed] == [
        ("radiant", 3, "ambiguous_match"),
        ("dire", 2, "low_score"),
    ]
    assert failed[0].best_hero_id == 3
    assert failed[0].margin == pytest.approx(0.01)
    assert failed[0].second_hero_id == 11
    assert failed[0].best_variant == "3"
    assert failed[0].hero_variant_count == 1
    assert failed[0].best_channels is not None
    assert failed[0].best_channels.phash == pytest.approx(0.8)
    assert failed[0].crop_hash == "0000000000000002"


def _draft_slot_diagnostics(
    accepted_slots: set[int],
    *,
    hero_ids: tuple[int, ...] = tuple(range(1, 11)),
    score: float = 0.8,
    margin: float = 0.1,
    crop_hash: str | None = None,
) -> tuple[HeroSlotDiagnostic, ...]:
    return tuple(
        HeroSlotDiagnostic(
            "radiant" if index < 5 else "dire",
            index % 5 + 1,
            hero_ids[index],
            score,
            score - margin,
            margin,
            index in accepted_slots,
            "accepted" if index in accepted_slots else "low_score",
            crop_hash=crop_hash,
        )
        for index in range(10)
    )


def test_draft_tracker_confirms_slots_across_different_frames() -> None:
    tracker = DraftTracker(confirmations=2)
    first_half = DraftReading((), (), 0.8, _draft_slot_diagnostics(set(range(5))))
    second_half = DraftReading((), (), 0.8, _draft_slot_diagnostics(set(range(5, 10))))

    assert tracker.update(first_half, observed_at=0.0) is None
    assert tracker.update(first_half, observed_at=3.0) is None
    assert tracker.update(first_half, observed_at=6.0) is None
    assert tracker.update(second_half, observed_at=9.0) is None
    assert tracker.update(second_half, observed_at=12.0) is None
    confirmed = tracker.update(second_half, observed_at=15.0)

    assert confirmed is not None
    assert confirmed.radiant_hero_ids == (1, 2, 3, 4, 5)
    assert confirmed.dire_hero_ids == (6, 7, 8, 9, 10)


def test_draft_tracker_rejects_correlated_duplicate_evidence() -> None:
    tracker = DraftTracker(confirmations=2)
    first = DraftReading(
        (),
        (),
        0.8,
        _draft_slot_diagnostics(set(range(10)), crop_hash="0000000000000000"),
    )
    changed_crop = DraftReading(
        (),
        (),
        0.8,
        _draft_slot_diagnostics(set(range(10)), crop_hash="ffffffffffffffff"),
    )

    assert tracker.update(first, observed_at=0.0) is None
    assert tracker.update(first, observed_at=1.0) is None
    assert tracker.update(first, observed_at=4.0) is None
    assert tracker.update(changed_crop, observed_at=4.0) is None
    assert tracker.update(first, observed_at=7.0) is not None
    assert all(item.duplicate_evidence_count == 2 for item in tracker.slot_statuses)


def test_draft_tracker_caps_static_crop_cluster_votes() -> None:
    tracker = DraftTracker()
    reading = DraftReading(
        (),
        (),
        0.8,
        _draft_slot_diagnostics(
            set(range(10)),
            crop_hash="0000000000000000",
        ),
    )

    for index in range(10):
        assert tracker.update(
            reading,
            observed_at=float(index * 3),
            source_frame_hash=f"frame-{index}",
            game_clock_seconds=100 + index * 3,
        ) is None

    assert all(item.state == "observing" for item in tracker.slot_statuses)


def test_draft_tracker_requires_crop_diversity_and_long_lock_window() -> None:
    tracker = DraftTracker()
    hashes = ("0000000000000000", "ffffffffffffffff")
    confirmed = None

    for index in range(12):
        reading = DraftReading(
            (),
            (),
            0.8,
            _draft_slot_diagnostics(
                set(range(10)),
                crop_hash=hashes[index % 2],
            ),
        )
        confirmed = tracker.update(
            reading,
            observed_at=float(index * 3),
            source_frame_hash=f"frame-{index}",
            game_clock_seconds=100 + index * 3,
        )
        if index < 11:
            assert confirmed is None

    assert confirmed is not None
    assert all(item.state == "locked" for item in tracker.slot_statuses)
    assert all(
        item.unique_crop_cluster_count == 2 for item in tracker.slot_statuses
    )


def test_draft_tracker_rejects_same_source_or_unadvanced_clock() -> None:
    tracker = DraftTracker(confirmations=2)
    reading = DraftReading(
        (),
        (),
        0.8,
        _draft_slot_diagnostics(
            set(range(10)),
            crop_hash="0000000000000000",
        ),
    )

    tracker.update(
        reading,
        observed_at=0.0,
        source_frame_hash="frame-a",
        game_clock_seconds=100,
    )
    tracker.update(
        reading,
        observed_at=3.0,
        source_frame_hash="frame-a",
        game_clock_seconds=103,
    )
    tracker.update(
        reading,
        observed_at=6.0,
        source_frame_hash="frame-b",
        game_clock_seconds=100,
    )

    assert all(
        status.independent_evidence_count == 0
        for status in tracker.slot_statuses
    )
    assert all(status.duplicate_evidence_count == 2 for status in tracker.slot_statuses)


def test_draft_tracker_requires_high_quality_support() -> None:
    tracker = DraftTracker()
    weak = DraftReading(
        (),
        (),
        0.655,
        _draft_slot_diagnostics(
            set(range(10)),
            score=0.655,
            margin=0.03,
        ),
    )

    for index in range(8):
        assert tracker.update(weak, observed_at=float(index * 3)) is None

    assert all(item.state == "observing" for item in tracker.slot_statuses)


def test_draft_tracker_recovers_from_strong_locked_conflicts() -> None:
    tracker = DraftTracker(confirmations=2)
    original = DraftReading(
        (),
        (),
        0.8,
        _draft_slot_diagnostics(set(range(10)), crop_hash="0000000000000000"),
    )
    original_changed_crop = DraftReading(
        (),
        (),
        0.8,
        _draft_slot_diagnostics(set(range(10)), crop_hash="ffffffffffffffff"),
    )
    conflicting_ids = tuple(range(11, 21))
    conflict = DraftReading(
        (),
        (),
        0.82,
        _draft_slot_diagnostics(
            set(range(10)),
            hero_ids=conflicting_ids,
            score=0.82,
            crop_hash="0f0f0f0f0f0f0f0f",
        ),
    )
    conflict_changed_crop = DraftReading(
        (),
        (),
        0.82,
        _draft_slot_diagnostics(
            set(range(10)),
            hero_ids=conflicting_ids,
            score=0.82,
            crop_hash="f0f0f0f0f0f0f0f0",
        ),
    )

    tracker.update(original, observed_at=0.0)
    tracker.update(original_changed_crop, observed_at=3.0)
    assert tracker.update(original, observed_at=6.0) is not None
    assert tracker.update(conflict, observed_at=9.0) is not None
    assert tracker.update(conflict_changed_crop, observed_at=12.0) is None
    assert all(item.state == "provisional" for item in tracker.slot_statuses)
    assert tracker.update(conflict, observed_at=15.0) is None
    assert all(item.state == "observing" for item in tracker.slot_statuses)


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


def test_observation_writer_replays_jsonl_after_database_sink_failure(
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

    def failing_sink(item: object) -> None:
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        ObservationWriter(path, sink=failing_sink).append(observation)

    persisted: list[object] = []
    writer = ObservationWriter(path, sink=persisted.append)
    assert writer.replay_to_sink() == 1
    assert len(persisted) == 1
    assert read_jsonl(path)[0].is_confirmed


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


def test_complete_draft_has_authority_without_live_hud_authority() -> None:
    observation = LiveObservation(
        raybet_match_id="draft-series",
        captured_at_utc=datetime.now(timezone.utc),
        map_number=2,
        radiant_hero_ids=[1, 2, 3, 4, 5],
        dire_hero_ids=[6, 7, 8, 9, 10],
        radiant_team_side="team_two",
        draft_confidence=0.96,
        source_frame_ref="draft-frame",
        screen_state="draft",
    )

    assert observation.is_draft_confirmed
    assert not observation.is_confirmed
    assert not observation.is_hud_confirmed

    assert not observation.model_copy(
        update={"screen_state": "replay"}
    ).is_draft_confirmed
    assert not observation.model_copy(
        update={"radiant_hero_ids": [1, 2, 3, 4]}
    ).is_draft_confirmed
    assert not observation.model_copy(
        update={"radiant_team_side": None}
    ).is_draft_confirmed


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


def test_team_side_uses_exact_two_name_ocr_when_broadcast_logo_changed() -> None:
    class FakeTeamNameOcr:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _image):
            self.calls += 1
            text, score = (("Level UP", 0.94), ("MOUZ", 0.83))[self.calls - 1]
            return [[[0, 0], text, score]], None

    circle, square = _logo("circle"), _logo("square")
    layout = replace(
        LOGO_LAYOUT,
        radiant_team_name=LEFT,
        dire_team_name=RIGHT,
    )
    recognizer = TeamSideRecognizer(
        circle,
        square,
        layout,
        team_names=("Level Up", "MOUZ"),
        use_ocr=False,
    )
    recognizer.ocr = FakeTeamNameOcr()
    unrelated = np.random.default_rng(99).integers(
        0, 256, (80, 160, 3), dtype=np.uint8
    )

    reading = recognizer.read(unrelated)

    assert reading.radiant_team_side == "team_one"
    assert reading.confidence == pytest.approx(0.83)


def test_team_side_name_ocr_requires_both_exact_confident_names() -> None:
    class FakeTeamNameOcr:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _image):
            self.calls += 1
            text, score = (("Level UP", 0.94), ("UNKNOWN", 0.99))[self.calls - 1]
            return [[[0, 0], text, score]], None

    circle, square = _logo("circle"), _logo("square")
    layout = replace(
        LOGO_LAYOUT,
        radiant_team_name=LEFT,
        dire_team_name=RIGHT,
    )
    recognizer = TeamSideRecognizer(
        circle,
        square,
        layout,
        team_names=("Level Up", "MOUZ"),
        use_ocr=False,
    )
    recognizer.ocr = FakeTeamNameOcr()
    unrelated = np.random.default_rng(100).integers(
        0, 256, (80, 160, 3), dtype=np.uint8
    )

    assert recognizer.read(unrelated).radiant_team_side is None


def test_team_side_name_ocr_works_without_logo_templates() -> None:
    class FakeTeamNameOcr:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _image):
            self.calls += 1
            text, score = (("Level UP", 0.94), ("NAVI", 0.91))[self.calls - 1]
            return [[[0, 0], text, score]], None

    layout = replace(
        LOGO_LAYOUT,
        radiant_team_name=LEFT,
        dire_team_name=RIGHT,
    )
    recognizer = TeamSideRecognizer(
        None,
        None,
        layout,
        team_names=("Level Up", "Na`Vi"),
        use_ocr=False,
    )
    recognizer.ocr = FakeTeamNameOcr()

    reading = recognizer.read(
        np.random.default_rng(102).integers(0, 256, (80, 160, 3), dtype=np.uint8)
    )

    assert reading.radiant_team_side == "team_one"
    assert reading.confidence == pytest.approx(0.91)


def test_team_side_name_ocr_sorts_split_words_horizontally() -> None:
    class FakeTeamNameOcr:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _image):
            self.calls += 1
            if self.calls == 1:
                return [
                    [[[70, 0], [100, 0], [100, 20], [70, 20]], "UP", 0.92],
                    [[[5, 0], [60, 0], [60, 20], [5, 20]], "Level", 0.94],
                ], None
            return [
                [[[4, 0], [50, 0], [50, 20], [4, 20]], "MOUZ", 0.93]
            ], None

    circle, square = _logo("circle"), _logo("square")
    layout = replace(
        LOGO_LAYOUT,
        radiant_team_name=LEFT,
        dire_team_name=RIGHT,
    )
    recognizer = TeamSideRecognizer(
        circle,
        square,
        layout,
        team_names=("Level Up", "MOUZ"),
        use_ocr=False,
    )
    recognizer.ocr = FakeTeamNameOcr()
    unrelated = np.random.default_rng(101).integers(
        0, 256, (80, 160, 3), dtype=np.uint8
    )

    reading = recognizer.read(unrelated)

    assert reading.radiant_team_side == "team_one"
    assert reading.confidence == pytest.approx(0.92)


def test_team_side_database_prefers_team_tag_logo(monkeypatch) -> None:
    _patch_logo_store(monkeypatch, with_team_logos=True)
    FakeHttpClient.responses = {
        "https://cdn.cloudflare.steamstatic.com/vp.png": _png(_logo("circle")),
        "https://cdn.cloudflare.steamstatic.com/xg.png": _png(_logo("square")),
    }
    monkeypatch.setattr("vision.team_side.httpx.Client", FakeHttpClient)
    recognizer = TeamSideRecognizer.from_database("postgresql+psycopg://test", "42")
    assert recognizer is not None
    assert FakeHttpClient.requested == [
        "https://cdn.cloudflare.steamstatic.com/vp.png",
        "https://cdn.cloudflare.steamstatic.com/xg.png",
    ]


def test_team_side_database_resolves_raybet_relative_logo_fallback(
    monkeypatch,
) -> None:
    _patch_logo_store(monkeypatch, with_team_logos=False)
    one = "https://www.ray086.com/file/fallback-one.png"
    two = "https://www.ray086.com/file/fallback-two.png"
    FakeHttpClient.responses = {
        one: _png(_logo("circle")),
        two: _png(_logo("square")),
    }
    monkeypatch.setattr("vision.team_side.httpx.Client", FakeHttpClient)
    recognizer = TeamSideRecognizer.from_database("postgresql+psycopg://test", "42")
    assert recognizer is not None
    assert FakeHttpClient.requested == [one, two]


def test_team_side_database_falls_back_after_cdn_failure(
    monkeypatch,
) -> None:
    _patch_logo_store(monkeypatch, with_team_logos=True)
    one = "https://www.ray086.com/file/fallback-one.png"
    two = "https://www.ray086.com/file/fallback-two.png"
    FakeHttpClient.responses = {
        "https://cdn.cloudflare.steamstatic.com/vp.png": httpx.ConnectError(
            "offline"
        ),
        "https://cdn.cloudflare.steamstatic.com/xg.png": httpx.ConnectError(
            "offline"
        ),
        one: _png(_logo("circle")),
        two: _png(_logo("square")),
    }
    monkeypatch.setattr("vision.team_side.httpx.Client", FakeHttpClient)

    recognizer = TeamSideRecognizer.from_database("postgresql+psycopg://test", "42")

    assert recognizer is not None
    assert FakeHttpClient.requested == [
        "https://cdn.cloudflare.steamstatic.com/vp.png",
        one,
        "https://cdn.cloudflare.steamstatic.com/xg.png",
        two,
    ]


def test_team_side_database_degrades_when_all_logo_sources_fail(
    monkeypatch,
) -> None:
    _patch_logo_store(monkeypatch, with_team_logos=True)
    error = httpx.ConnectError("offline")
    FakeHttpClient.responses = {
        "https://cdn.cloudflare.steamstatic.com/vp.png": error,
        "https://cdn.cloudflare.steamstatic.com/xg.png": error,
        "https://www.ray086.com/file/fallback-one.png": error,
        "https://www.ray086.com/file/fallback-two.png": error,
    }
    monkeypatch.setattr("vision.team_side.httpx.Client", FakeHttpClient)
    monkeypatch.setattr(
        TeamSideRecognizer,
        "_ensure_ocr",
        lambda self: setattr(self, "ocr", object()),
    )

    loaded = TeamSideRecognizer.load_from_database(
        "postgresql+psycopg://test",
        "42",
    )

    assert loaded.recognizer is not None
    assert loaded.error == (
        "team_one_logo_invalid,team_two_logo_invalid:team_name_ocr_fallback"
    )


def test_team_side_database_rejects_html_logo_response(monkeypatch) -> None:
    _patch_logo_store(monkeypatch, with_team_logos=False)
    monkeypatch.setattr(
        FakeHttpClient,
        "stream",
        lambda self, method, url: FakeResponse(
            b"<html>login</html>", "text/html"
        ),
    )
    monkeypatch.setattr("vision.team_side.httpx.Client", FakeHttpClient)
    monkeypatch.setattr(
        TeamSideRecognizer,
        "_ensure_ocr",
        lambda self: setattr(self, "ocr", object()),
    )

    loaded = TeamSideRecognizer.load_from_database(
        "postgresql+psycopg://test",
        "42",
    )

    assert loaded.recognizer is not None
    assert loaded.error == (
        "team_one_logo_invalid,team_two_logo_invalid:team_name_ocr_fallback"
    )


@pytest.mark.parametrize(
    "url",
    (
        "http://www.ray086.com/logo.png",
        "https://127.0.0.1/logo.png",
        "https://localhost/logo.png",
        "https://cdn.cloudflare.steamstatic.com:8443/logo.png",
        "https://user:pass@www.ray086.com/logo.png",
        "https://example.com/logo.png",
    ),
)
def test_team_logo_url_rejects_untrusted_sources(url: str) -> None:
    with pytest.raises(ValueError, match="invalid team logo URL"):
        _validate_team_logo_url(url)


def test_team_logo_url_allows_current_steam_cdn() -> None:
    url = "https://cdn.steamusercontent.com/team-logo.png"

    assert _validate_team_logo_url(url) == url


def test_team_side_revalidates_redirect_target(monkeypatch) -> None:
    _patch_logo_store(monkeypatch, with_team_logos=True)
    cdn_one = "https://cdn.cloudflare.steamstatic.com/vp.png"
    cdn_two = "https://cdn.cloudflare.steamstatic.com/xg.png"
    fallback_one = "https://www.ray086.com/file/fallback-one.png"
    FakeHttpClient.responses = {
        cdn_one: FakeResponse(
            b"",
            status_code=302,
            location="http://127.0.0.1/private.png",
        ),
        fallback_one: _png(_logo("circle")),
        cdn_two: _png(_logo("square")),
    }
    monkeypatch.setattr("vision.team_side.httpx.Client", FakeHttpClient)

    loaded = TeamSideRecognizer.load_from_database(
        "postgresql+psycopg://test",
        "42",
    )

    assert loaded.recognizer is not None
    assert FakeHttpClient.requested == [cdn_one, fallback_one, cdn_two]


def test_team_side_rejects_oversized_logo_response(monkeypatch) -> None:
    _patch_logo_store(monkeypatch, with_team_logos=False)
    fallback_one = "https://www.ray086.com/file/fallback-one.png"
    fallback_two = "https://www.ray086.com/file/fallback-two.png"
    FakeHttpClient.responses = {
        fallback_one: b"x" * (MAX_TEAM_LOGO_BYTES + 1),
        fallback_two: _png(_logo("square")),
    }
    monkeypatch.setattr("vision.team_side.httpx.Client", FakeHttpClient)
    monkeypatch.setattr(
        TeamSideRecognizer,
        "_ensure_ocr",
        lambda self: setattr(self, "ocr", object()),
    )

    loaded = TeamSideRecognizer.load_from_database(
        "postgresql+psycopg://test",
        "42",
    )

    assert loaded.recognizer is not None
    assert loaded.error == "team_one_logo_invalid:team_name_ocr_fallback"


def test_team_side_logo_failure_stays_unavailable_without_name_ocr(
    monkeypatch,
) -> None:
    _patch_logo_store(monkeypatch, with_team_logos=True)
    error = httpx.ConnectError("offline")
    FakeHttpClient.responses = {
        "https://cdn.cloudflare.steamstatic.com/vp.png": error,
        "https://cdn.cloudflare.steamstatic.com/xg.png": error,
        "https://www.ray086.com/file/fallback-one.png": error,
        "https://www.ray086.com/file/fallback-two.png": error,
    }
    monkeypatch.setattr("vision.team_side.httpx.Client", FakeHttpClient)
    monkeypatch.setattr(TeamSideRecognizer, "_ensure_ocr", lambda self: None)

    loaded = TeamSideRecognizer.load_from_database(
        "postgresql+psycopg://test",
        "42",
    )

    assert loaded.recognizer is None
    assert loaded.error == "team_one_logo_invalid,team_two_logo_invalid"
