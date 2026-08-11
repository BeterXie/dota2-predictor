"""Fail-closed OCR for player nameplates on completed draft frames."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from contracts.live_observation import (
    DraftPlayerNameplate,
    DraftPlayerNames,
    PLAYER_NAME_MIN_CONFIDENCE,
)

from .layouts import BroadcastLayout


class DraftPlayerNameReader:
    def __init__(self, layout: BroadcastLayout, *, ocr: Any = None) -> None:
        self.layout = layout
        self.ocr = ocr

    @staticmethod
    def _result(result: object) -> tuple[str | None, float]:
        if not isinstance(result, list) or not result:
            return None, 0.0
        first = result[0]
        if not isinstance(first, (list, tuple)) or len(first) < 2:
            return None, 0.0
        text = " ".join(str(first[0] or "").split()) or None
        try:
            confidence = float(first[1])
        except (TypeError, ValueError):
            return text, 0.0
        return text, max(0.0, min(1.0, confidence))

    def _read_slot(
        self,
        image: np.ndarray,
        *,
        region_index: int,
    ) -> DraftPlayerNameplate:
        side = "radiant" if region_index < 5 else "dire"
        visual_slot = region_index + 1 if region_index < 5 else region_index - 4
        region = self.layout.draft_player_nameplates[region_index]
        try:
            crop = region.crop(image)
            enlarged = cv2.resize(
                crop,
                None,
                fx=6,
                fy=6,
                interpolation=cv2.INTER_CUBIC,
            )
            result, _ = self.ocr(
                enlarged,
                use_det=False,
                use_cls=False,
                use_rec=True,
            )
            raw_text, confidence = self._result(result)
        except (cv2.error, RuntimeError, TypeError, ValueError):
            raw_text, confidence = None, 0.0
            reason = "ocr_failed"
        else:
            if raw_text is None:
                reason = "ocr_no_text"
            elif confidence < PLAYER_NAME_MIN_CONFIDENCE:
                reason = "confidence_below_threshold"
            else:
                return DraftPlayerNameplate(
                    side=side,
                    visual_slot=visual_slot,
                    raw_text=raw_text,
                    observed_text=raw_text,
                    verified_player_name=None,
                    identity_source_url=None,
                    confidence=confidence,
                    unavailable_reason=None,
                )
        return DraftPlayerNameplate(
            side=side,
            visual_slot=visual_slot,
            raw_text=raw_text,
            observed_text=None,
            verified_player_name=None,
            identity_source_url=None,
            confidence=confidence,
            unavailable_reason=reason,
        )

    def read(self, image: np.ndarray) -> DraftPlayerNames:
        if len(self.layout.draft_player_nameplates) != 10:
            return DraftPlayerNames.unavailable("layout_nameplates_unavailable")
        if self.ocr is None:
            return DraftPlayerNames.unavailable("ocr_unavailable")
        slots = [self._read_slot(image, region_index=index) for index in range(10)]
        accepted = sum(slot.observed_text is not None for slot in slots)
        if accepted == 10:
            return DraftPlayerNames(
                status="available",
                source="vision_ocr",
                slots=slots,
                unavailable_reason=None,
            )
        if accepted:
            return DraftPlayerNames(
                status="partial",
                source="vision_ocr",
                slots=slots,
                unavailable_reason="one_or_more_nameplates_untrusted",
            )
        return DraftPlayerNames(
            status="unavailable",
            source="vision_ocr",
            slots=slots,
            unavailable_reason="all_nameplates_untrusted",
        )

    @staticmethod
    def bind_heroes(
        reading: DraftPlayerNames,
        radiant_hero_ids: tuple[int, ...],
        dire_hero_ids: tuple[int, ...],
    ) -> DraftPlayerNames:
        heroes = radiant_hero_ids + dire_hero_ids
        if (
            len(radiant_hero_ids) != 5
            or len(dire_hero_ids) != 5
            or len(set(heroes)) != 10
            or any(type(hero_id) is not int or hero_id <= 0 for hero_id in heroes)
            or len(reading.slots) != 10
        ):
            return reading
        slots = [
            DraftPlayerNameplate.model_validate(
                {**slot.model_dump(mode="python"), "hero_id": heroes[index]}
            )
            for index, slot in enumerate(reading.slots)
        ]
        return DraftPlayerNames.model_validate(
            {**reading.model_dump(mode="python"), "slots": slots}
        )


__all__ = ["DraftPlayerNameReader"]
