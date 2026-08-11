from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from contracts.live_observation import (
    DraftPlayerNameplate,
    LiveObservation,
    PLAYER_NAME_MIN_CONFIDENCE,
)
from live_betting.vision import parse_observation, read_jsonl
from vision.layouts import EPL_MASTERS_DRAFT, STANDARD_DOTA_HUD
from vision.observation_writer import ObservationWriter
from vision.player_name_reader import DraftPlayerNameReader


class _SequentialOcr:
    def __init__(self, values: list[tuple[str, float]]) -> None:
        self.values = iter(values)

    def __call__(self, _image: np.ndarray, **kwargs: object) -> tuple[list[list[object]], None]:
        assert kwargs == {"use_det": False, "use_cls": False, "use_rec": True}
        text, confidence = next(self.values)
        return [[text, confidence]], None


def _frame() -> np.ndarray:
    return np.zeros((1080, 1920, 3), dtype=np.uint8)


def test_epl_nameplate_reader_keeps_visual_slots_separate_from_positions() -> None:
    names = [
        "Riddys",
        "daze",
        "pma",
        "gotthejuice",
        "Niku",
        "aik",
        "Copy",
        "Malik",
        "Ekki",
        "rincyq",
    ]
    reader = DraftPlayerNameReader(
        EPL_MASTERS_DRAFT,
        ocr=_SequentialOcr([(name, 0.95) for name in names]),
    )

    reading = reader.read(_frame())

    assert reading.status == "available"
    assert [slot.observed_text for slot in reading.slots] == names
    assert all(slot.verified_player_name is None for slot in reading.slots)
    assert [(slot.side, slot.visual_slot) for slot in reading.slots] == [
        *(('radiant', slot) for slot in range(1, 6)),
        *(('dire', slot) for slot in range(1, 6)),
    ]
    assert all(not hasattr(slot, "position") for slot in reading.slots)


def test_low_confidence_nameplate_preserves_raw_ocr_without_accepting_identity() -> None:
    values = [(f"player-{index}", 0.95) for index in range(1, 11)]
    values[5] = ("aik", PLAYER_NAME_MIN_CONFIDENCE - 0.01)
    reading = DraftPlayerNameReader(
        EPL_MASTERS_DRAFT,
        ocr=_SequentialOcr(values),
    ).read(_frame())

    assert reading.status == "partial"
    slot = reading.slots[5]
    assert slot.side == "dire"
    assert slot.visual_slot == 1
    assert slot.raw_text == "aik"
    assert slot.observed_text is None
    assert slot.verified_player_name is None
    assert slot.unavailable_reason == "confidence_below_threshold"


def test_nameplate_reader_records_why_ocr_cannot_run() -> None:
    assert (
        DraftPlayerNameReader(EPL_MASTERS_DRAFT).read(_frame()).unavailable_reason
        == "ocr_unavailable"
    )
    assert (
        DraftPlayerNameReader(STANDARD_DOTA_HUD, ocr=_SequentialOcr([]))
        .read(_frame())
        .unavailable_reason
        == "layout_nameplates_unavailable"
    )


def test_contract_rejects_low_confidence_accepted_name() -> None:
    with pytest.raises(ValueError, match="confident OCR evidence"):
        DraftPlayerNameplate(
            side="radiant",
            visual_slot=1,
            raw_text="player",
            observed_text="player",
            confidence=PLAYER_NAME_MIN_CONFIDENCE - 0.01,
            unavailable_reason=None,
        )


def test_verified_player_name_requires_exact_text_and_source_url() -> None:
    with pytest.raises(ValueError, match="exact observed text"):
        DraftPlayerNameplate(
            side="radiant",
            visual_slot=1,
            raw_text="Eki",
            observed_text="Eki",
            verified_player_name="Ekki",
            identity_source_url="https://example.test/roster",
            confidence=0.95,
            unavailable_reason=None,
        )


def test_nameplate_hero_binding_requires_complete_unique_draft() -> None:
    reader = DraftPlayerNameReader(
        EPL_MASTERS_DRAFT,
        ocr=_SequentialOcr([(f"player-{index}", 0.95) for index in range(10)]),
    )
    reading = reader.read(_frame())

    bound = reader.bind_heroes(reading, tuple(range(1, 6)), tuple(range(6, 11)))
    unbound = reader.bind_heroes(reading, tuple(range(1, 5)), tuple(range(6, 11)))

    assert [slot.hero_id for slot in bound.slots] == list(range(1, 11))
    assert all(slot.hero_id is None for slot in unbound.slots)


def test_schema_five_nameplate_evidence_round_trips_through_jsonl(tmp_path) -> None:
    reader = DraftPlayerNameReader(
        EPL_MASTERS_DRAFT,
        ocr=_SequentialOcr([(f"player-{index}", 0.95) for index in range(10)]),
    )
    names = reader.bind_heroes(
        reader.read(_frame()),
        tuple(range(1, 6)),
        tuple(range(6, 11)),
    )
    observation = LiveObservation(
        raybet_match_id="series-vision-names",
        map_number=2,
        captured_at_utc=datetime(2026, 8, 11, tzinfo=timezone.utc),
        radiant_hero_ids=list(range(1, 6)),
        dire_hero_ids=list(range(6, 11)),
        draft_confidence=0.95,
        source_frame_ref="stream:frame:1",
        screen_state="draft",
        draft_player_names=names,
    )
    path = tmp_path / "series-vision-names.jsonl"

    ObservationWriter(path).append(observation)
    parsed = read_jsonl(path)

    assert len(parsed) == 1
    assert parsed[0].draft_player_names.status == "available"
    assert [slot.hero_id for slot in parsed[0].draft_player_names.slots] == list(
        range(1, 11)
    )
    assert all(
        slot.verified_player_name is None
        for slot in parsed[0].draft_player_names.slots
    )


def test_schema_four_replay_marks_player_names_unobserved() -> None:
    payload = LiveObservation(
        raybet_match_id="legacy-series",
        map_number=1,
        captured_at_utc=datetime(2026, 8, 10, tzinfo=timezone.utc),
        source_frame_ref="stream:legacy:1",
    ).model_dump(mode="json")
    payload["schema_version"] = 4
    payload.pop("draft_player_names")

    parsed = parse_observation(payload)

    assert parsed.draft_player_names.status == "unavailable"
    assert (
        parsed.draft_player_names.unavailable_reason
        == "draft_player_names_not_observed"
    )
