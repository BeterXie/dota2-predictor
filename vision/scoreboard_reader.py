"""High-confidence kill-score reading for the standard Dota spectator HUD."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

from contracts.live_observation import (
    COMEBACK_STATE_MIN_CONFIDENCE,
    is_canonical_net_worth_bucket,
)

from .clock_reader import ClockReader, ClockReading, _render_templates
from .layout_selector import layout_match_confidence
from .layouts import BroadcastLayout, NormalizedRegion, STANDARD_DOTA_HUD


def _normalized_glyph(image: np.ndarray) -> np.ndarray:
    points = cv2.findNonZero(image)
    if points is None:
        return np.zeros((36, 24), dtype=np.uint8)
    x, y, width, height = cv2.boundingRect(points)
    glyph = image[y : y + height, x : x + width]
    scale = min(22.0 / max(1, width), 34.0 / max(1, height))
    resized = cv2.resize(
        glyph,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )
    canvas = np.zeros((36, 24), dtype=np.uint8)
    top = (canvas.shape[0] - resized.shape[0]) // 2
    left = (canvas.shape[1] - resized.shape[1]) // 2
    canvas[top : top + resized.shape[0], left : left + resized.shape[1]] = resized
    return canvas


@dataclass(frozen=True)
class ScoreboardReading:
    radiant_kills: int | None
    dire_kills: int | None
    confidence: float


@dataclass(frozen=True)
class NetWorthAdvantageReading:
    side: Literal["radiant", "dire"] | None
    minimum: int | None
    maximum: int | None
    confidence: float


@dataclass(frozen=True)
class ReplayGateReading:
    status: Literal["live", "replay", "untrusted"]
    confidence: float
    text: str | None = None


class ScoreboardReader:
    def __init__(
        self,
        layout: BroadcastLayout = STANDARD_DOTA_HUD,
        *,
        use_ocr: bool = True,
    ) -> None:
        self.layout = layout
        self.templates = {
            digit: _normalized_glyph(
                cv2.threshold(template, 175, 255, cv2.THRESH_BINARY)[1]
            )
            for digit, template in _render_templates().items()
        }
        self.ocr = None
        self._strip_cache_image: np.ndarray | None = None
        self._strip_cache_result: tuple[np.ndarray, list[object]] | None = None
        if use_ocr:
            try:
                from rapidocr_onnxruntime import RapidOCR

                self.ocr = RapidOCR()
            except ImportError:
                pass

    @staticmethod
    def _parse_text(text: str, confidence: float) -> tuple[int, float] | None:
        digits = re.sub(r"[^0-9]", "", text)
        if not 1 <= len(digits) <= 3:
            return None
        value = int(digits)
        return (value, confidence) if value <= 500 else None

    def _read_region(
        self,
        image: np.ndarray,
        region: NormalizedRegion | None,
    ) -> tuple[int, float] | None:
        if region is None:
            return None
        crop = region.crop(image)
        if crop.size == 0:
            return None
        if self.ocr is not None:
            enlarged = cv2.resize(
                crop, None, fx=8, fy=8, interpolation=cv2.INTER_CUBIC
            )
            gray_ocr = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
            result, _ = self.ocr(
                gray_ocr, use_det=False, use_cls=False, use_rec=True
            )
            if result:
                parsed = self._parse_text(str(result[0][0]), float(result[0][1]))
                if parsed is not None:
                    return parsed

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 175, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(
            binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        boxes = []
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if height >= binary.shape[0] * 0.25 and width >= 2:
                boxes.append((x, y, width, height))
        boxes.sort()
        if not 1 <= len(boxes) <= 3:
            return None

        digits: list[str] = []
        scores: list[float] = []
        for x, y, width, height in boxes:
            glyph = binary[y : y + height, x : x + width]
            glyph = _normalized_glyph(glyph)
            best_digit, best_score = max(
                (
                    (
                        digit,
                        float(
                            cv2.matchTemplate(
                                glyph, template, cv2.TM_CCOEFF_NORMED
                            )[0, 0]
                        ),
                    )
                    for digit, template in self.templates.items()
                ),
                key=lambda item: item[1],
            )
            digits.append(best_digit)
            scores.append(best_score)
        return self._parse_text("".join(digits), float(np.mean(scores)))

    @staticmethod
    def _parse_advantage_text(
        text: str,
        confidence: float,
    ) -> tuple[int, int, float] | None:
        normalized = text.lower().replace(" ", "").replace(",", "")
        match = re.search(r"(<)?(\d{1,3})k", normalized)
        if match is None:
            return None
        bucket = int(match.group(2))
        if match.group(1) is not None:
            return (0, 999, confidence) if bucket == 1 else None
        if not 1 <= bucket <= 500:
            return None
        minimum = bucket * 1_000
        maximum = (bucket + 1) * 1_000 - 1
        assert is_canonical_net_worth_bucket(minimum, maximum)
        return minimum, maximum, confidence

    def _read_advantage_region(
        self,
        image: np.ndarray,
        region: NormalizedRegion | None,
    ) -> tuple[int, int, float] | None:
        if region is None or self.ocr is None:
            return None
        crop = region.crop(image)
        if crop.size == 0:
            return None
        enlarged = cv2.resize(crop, None, fx=8, fy=8, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
        result, _ = self.ocr(gray, use_det=False, use_cls=False, use_rec=True)
        readings = [
            parsed
            for item in result or []
            if (
                parsed := self._parse_advantage_text(
                    str(item[0]),
                    float(item[1]),
                )
            )
            is not None
        ]
        return max(readings, key=lambda reading: reading[2]) if readings else None

    @staticmethod
    def _classify_broadcast_text(
        readings: list[tuple[str, float]],
        *,
        live_marker_sets: tuple[tuple[str, ...], ...] = (),
        min_confidence: float = COMEBACK_STATE_MIN_CONFIDENCE,
    ) -> ReplayGateReading:
        normalized = [
            (re.sub(r"[^a-z0-9]", "", text.casefold()), confidence, text)
            for text, confidence in readings
            if text.strip()
        ]
        replay = [
            (confidence, text)
            for text_value, confidence, text in normalized
            if "highlights" in text_value or "replay" in text_value
        ]
        if replay:
            confidence, text = max(replay)
            if confidence >= min_confidence:
                return ReplayGateReading("replay", confidence, text)
            return ReplayGateReading("untrusted", confidence, text)
        trusted = {
            text_value: (confidence, text)
            for text_value, confidence, text in normalized
            if confidence >= min_confidence
        }
        matched = next(
            (
                markers
                for markers in live_marker_sets
                if markers and all(marker in trusted for marker in markers)
            ),
            None,
        )
        if matched is not None:
            confidence, text = max(trusted[marker] for marker in matched)
            return ReplayGateReading("live", confidence, text)
        confidence = max((item[1] for item in normalized), default=0.0)
        return ReplayGateReading("untrusted", confidence)

    def read_replay_gate(self, image: np.ndarray) -> ReplayGateReading:
        if self.layout.requires_geometry_confirmation:
            return self._read_geometry_replay_gate(image)
        region = self.layout.broadcast_status
        if region is None or self.ocr is None:
            return ReplayGateReading("untrusted", 0.0)
        readings: list[tuple[str, float]] = []
        for status_region in (region, *self.layout.replay_status_regions):
            crop = status_region.crop(image)
            if crop.size == 0:
                continue
            result, _ = self.ocr(crop)
            readings.extend(
                (str(item[1]), float(item[2]))
                for item in result or []
                if len(item) >= 3
                and isinstance(item[1], str)
                and isinstance(item[2], (int, float))
                and not isinstance(item[2], bool)
            )
        return self._classify_broadcast_text(
            readings,
            live_marker_sets=self.layout.live_broadcast_marker_sets,
        )

    def _scoreboard_strip_ocr(
        self,
        image: np.ndarray,
    ) -> tuple[np.ndarray, list[object]] | None:
        strip = self.layout.scoreboard_strip
        if strip is None or self.ocr is None:
            return None
        if self._strip_cache_image is image and self._strip_cache_result is not None:
            return self._strip_cache_result
        crop = strip.crop(image)
        if crop.size == 0:
            return None
        result, _ = self.ocr(crop)
        cached = (crop, list(result or []))
        self._strip_cache_image = image
        self._strip_cache_result = cached
        return cached

    def _read_geometry_replay_gate(self, image: np.ndarray) -> ReplayGateReading:
        positioned = self._scoreboard_strip_ocr(image)
        if positioned is None:
            return ReplayGateReading("untrusted", 0.0)
        crop, result = positioned
        readings = [
            (str(item[1]), float(item[2]))
            for item in result
            if isinstance(item, (list, tuple))
            and len(item) >= 3
            and isinstance(item[1], str)
            and isinstance(item[2], (int, float))
            and not isinstance(item[2], bool)
        ]
        replay = self._classify_broadcast_text(readings)
        if replay.status == "replay" or replay.text is not None:
            return replay
        geometry_confidence = layout_match_confidence(image, self.layout)
        scoreboard = self._scoreboard_from_ocr(crop, result)
        clock_confidence = max(
            (
                confidence
                for text, confidence in readings
                if re.fullmatch(r"-?\d{1,2}:\d{2}", text.strip())
            ),
            default=0.0,
        )
        if (
            geometry_confidence < COMEBACK_STATE_MIN_CONFIDENCE
            or scoreboard is None
            or scoreboard.confidence < 0.8
            or clock_confidence < COMEBACK_STATE_MIN_CONFIDENCE
        ):
            return ReplayGateReading(
                "untrusted",
                max(geometry_confidence, scoreboard.confidence if scoreboard else 0.0),
            )
        return ReplayGateReading(
            "live",
            min(geometry_confidence, scoreboard.confidence, clock_confidence),
            self.layout.name,
        )

    @staticmethod
    def _region_contains(
        region: NormalizedRegion | None,
        x: float,
        y: float,
    ) -> bool:
        return (
            region is not None
            and region.left <= x <= region.right
            and region.top <= y <= region.bottom
        )

    def _positioned_text(
        self,
        item: object,
        crop: np.ndarray,
    ) -> tuple[str, float, float, float] | None:
        strip = self.layout.scoreboard_strip
        if (
            strip is None
            or not isinstance(item, (list, tuple))
            or len(item) < 3
            or not isinstance(item[0], (list, tuple))
            or not isinstance(item[1], str)
            or not isinstance(item[2], (int, float))
            or isinstance(item[2], bool)
        ):
            return None
        points = np.asarray(item[0], dtype=np.float32)
        if points.ndim != 2 or points.shape[1] != 2:
            return None
        local_x = float(points[:, 0].mean()) / max(1, crop.shape[1])
        local_y = float(points[:, 1].mean()) / max(1, crop.shape[0])
        return (
            str(item[1]),
            float(item[2]),
            strip.left + local_x * (strip.right - strip.left),
            strip.top + local_y * (strip.bottom - strip.top),
        )

    def read_positioned_clock(self, image: np.ndarray) -> ClockReading | None:
        positioned = self._scoreboard_strip_ocr(image)
        if positioned is None:
            return None
        crop, result = positioned
        readings: list[ClockReading] = []
        for item in result:
            parsed_item = self._positioned_text(item, crop)
            if parsed_item is None:
                continue
            text, confidence, x, y = parsed_item
            if not self._region_contains(self.layout.clock, x, y):
                continue
            reading = ClockReader._parse_digits(text, confidence)
            if reading is not None:
                readings.append(reading)
        return max(readings, key=lambda item: item.confidence) if readings else None

    def _read_positioned_advantage(
        self,
        image: np.ndarray,
    ) -> NetWorthAdvantageReading:
        positioned = self._scoreboard_strip_ocr(image)
        if positioned is None:
            return NetWorthAdvantageReading(None, None, None, 0.0)
        crop, result = positioned
        radiant: list[tuple[int, int, float]] = []
        dire: list[tuple[int, int, float]] = []
        for item in result:
            parsed_item = self._positioned_text(item, crop)
            if parsed_item is None:
                continue
            text, confidence, x, y = parsed_item
            parsed = self._parse_advantage_text(text, confidence)
            if parsed is None:
                continue
            if self._region_contains(
                self.layout.radiant_net_worth_advantage,
                x,
                y,
            ):
                radiant.append(parsed)
            elif self._region_contains(
                self.layout.dire_net_worth_advantage,
                x,
                y,
            ):
                dire.append(parsed)
        radiant_reading = max(radiant, key=lambda item: item[2]) if radiant else None
        dire_reading = max(dire, key=lambda item: item[2]) if dire else None
        if (radiant_reading is None) == (dire_reading is None):
            confidence = min(
                radiant_reading[2] if radiant_reading is not None else 0.0,
                dire_reading[2] if dire_reading is not None else 0.0,
            )
            return NetWorthAdvantageReading(None, None, None, confidence)
        side: Literal["radiant", "dire"] = (
            "radiant" if radiant_reading is not None else "dire"
        )
        reading = radiant_reading if radiant_reading is not None else dire_reading
        assert reading is not None
        return NetWorthAdvantageReading(side, *reading)

    def _scoreboard_from_ocr(
        self,
        crop: np.ndarray,
        result: list[object],
    ) -> ScoreboardReading | None:
        strip = self.layout.scoreboard_strip
        if strip is None:
            return None
        radiant: list[tuple[int, float]] = []
        dire: list[tuple[int, float]] = []
        for item in result or []:
            positioned_item = self._positioned_text(item, crop)
            if positioned_item is None:
                continue
            text, confidence, normalized_x, normalized_y = positioned_item
            parsed = self._parse_text(text, confidence)
            if parsed is None:
                continue
            if self._region_contains(
                self.layout.radiant_kills,
                normalized_x,
                normalized_y,
            ):
                radiant.append(parsed)
            elif self._region_contains(
                self.layout.dire_kills,
                normalized_x,
                normalized_y,
            ):
                dire.append(parsed)
        if not radiant or not dire:
            return None
        radiant_value = max(radiant, key=lambda item: item[1])
        dire_value = max(dire, key=lambda item: item[1])
        return ScoreboardReading(
            radiant_value[0],
            dire_value[0],
            min(radiant_value[1], dire_value[1]),
        )

    def _read_scoreboard_strip(
        self,
        image: np.ndarray,
    ) -> ScoreboardReading | None:
        positioned = self._scoreboard_strip_ocr(image)
        if positioned is None:
            return None
        return self._scoreboard_from_ocr(*positioned)

    def read(self, image: np.ndarray) -> ScoreboardReading:
        strip_reading = self._read_scoreboard_strip(image)
        if strip_reading is not None:
            return strip_reading
        radiant = self._read_region(image, self.layout.radiant_kills)
        dire = self._read_region(image, self.layout.dire_kills)
        if radiant is None or dire is None:
            confidence = min(
                radiant[1] if radiant is not None else 0.0,
                dire[1] if dire is not None else 0.0,
            )
            return ScoreboardReading(None, None, confidence)
        return ScoreboardReading(
            radiant_kills=radiant[0],
            dire_kills=dire[0],
            confidence=min(radiant[1], dire[1]),
        )

    def read_net_worth_advantage(
        self,
        image: np.ndarray,
    ) -> NetWorthAdvantageReading:
        if self.layout.scoreboard_strip is not None:
            return self._read_positioned_advantage(image)
        radiant = self._read_advantage_region(
            image,
            self.layout.radiant_net_worth_advantage,
        )
        dire = self._read_advantage_region(
            image,
            self.layout.dire_net_worth_advantage,
        )
        if (radiant is None) == (dire is None):
            confidence = min(
                radiant[2] if radiant is not None else 0.0,
                dire[2] if dire is not None else 0.0,
            )
            return NetWorthAdvantageReading(None, None, None, confidence)
        side: Literal["radiant", "dire"] = (
            "radiant" if radiant is not None else "dire"
        )
        reading = radiant if radiant is not None else dire
        assert reading is not None
        return NetWorthAdvantageReading(side, *reading)


class ScoreboardTracker:
    def __init__(
        self,
        *,
        confirmations: int = 2,
        min_confidence: float = COMEBACK_STATE_MIN_CONFIDENCE,
        max_frame_jump: int = 5,
    ) -> None:
        self._recent: deque[ScoreboardReading] = deque(maxlen=confirmations)
        self.min_confidence = min_confidence
        self.max_frame_jump = max_frame_jump

    def reset(self) -> None:
        self._recent.clear()

    def update(self, reading: ScoreboardReading) -> ScoreboardReading | None:
        if (
            reading.radiant_kills is None
            or reading.dire_kills is None
            or reading.confidence < self.min_confidence
        ):
            self.reset()
            return None
        self._recent.append(reading)
        if len(self._recent) < self._recent.maxlen:
            return None
        rows = tuple(self._recent)
        for previous, current in zip(rows, rows[1:]):
            radiant_jump = current.radiant_kills - previous.radiant_kills
            dire_jump = current.dire_kills - previous.dire_kills
            if not (
                0 <= radiant_jump <= self.max_frame_jump
                and 0 <= dire_jump <= self.max_frame_jump
            ):
                self._recent.clear()
                self._recent.append(current)
                return None
            if radiant_jump != 0 or dire_jump != 0:
                self._recent.clear()
                self._recent.append(current)
                return None
        return ScoreboardReading(
            rows[-1].radiant_kills,
            rows[-1].dire_kills,
            min(row.confidence for row in rows),
        )


class NetWorthAdvantageTracker:
    def __init__(
        self,
        *,
        confirmations: int = 2,
        min_confidence: float = COMEBACK_STATE_MIN_CONFIDENCE,
    ) -> None:
        self._recent: deque[NetWorthAdvantageReading] = deque(maxlen=confirmations)
        self.min_confidence = min_confidence

    def reset(self) -> None:
        self._recent.clear()

    def update(
        self,
        reading: NetWorthAdvantageReading,
    ) -> NetWorthAdvantageReading | None:
        if (
            reading.side is None
            or reading.minimum is None
            or reading.maximum is None
            or reading.confidence < self.min_confidence
        ):
            self.reset()
            return None
        self._recent.append(reading)
        if len(self._recent) < self._recent.maxlen:
            return None
        rows = tuple(self._recent)
        identity = (rows[-1].side, rows[-1].minimum, rows[-1].maximum)
        if any(
            (row.side, row.minimum, row.maximum) != identity for row in rows[:-1]
        ):
            self._recent.clear()
            self._recent.append(rows[-1])
            return None
        return NetWorthAdvantageReading(
            rows[-1].side,
            rows[-1].minimum,
            rows[-1].maximum,
            min(row.confidence for row in rows),
        )
