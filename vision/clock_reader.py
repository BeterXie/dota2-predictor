"""Template-based Dota HUD clock reader with explicit confidence."""

from __future__ import annotations

from dataclasses import dataclass
import re

import cv2
import numpy as np

from .layouts import BroadcastLayout, STANDARD_DOTA_HUD


@dataclass(frozen=True)
class ClockReading:
    seconds: int | None
    confidence: float
    text: str | None


def _render_templates(size: tuple[int, int] = (24, 36)) -> dict[str, np.ndarray]:
    width, height = size
    templates = {}
    for char in "0123456789":
        canvas = np.zeros((height, width), dtype=np.uint8)
        scale = 1.0
        thickness = 2
        (tw, th), _ = cv2.getTextSize(char, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
        cv2.putText(
            canvas,
            char,
            ((width - tw) // 2, (height + th) // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            255,
            thickness,
            cv2.LINE_AA,
        )
        templates[char] = canvas
    return templates


class ClockReader:
    def __init__(
        self, layout: BroadcastLayout = STANDARD_DOTA_HUD, *, use_ocr: bool = True
    ) -> None:
        self.layout = layout
        self.templates = _render_templates()
        self.ocr = None
        if use_ocr:
            try:
                from rapidocr_onnxruntime import RapidOCR

                self.ocr = RapidOCR()
            except ImportError:
                pass

    @staticmethod
    def _parse_digits(text: str, confidence: float) -> ClockReading | None:
        compact = re.sub(r"[^0-9-]", "", text)
        negative = compact.startswith("-")
        digits = compact.removeprefix("-")
        if len(digits) not in {3, 4}:
            return None
        minutes = int(digits[:-2])
        seconds = int(digits[-2:])
        if seconds >= 60:
            return None
        total = minutes * 60 + seconds
        if negative:
            total = -total
        rendered = f"{'-' if negative else ''}{minutes}:{seconds:02d}"
        return ClockReading(total, confidence, rendered)

    def read(self, image: np.ndarray) -> ClockReading:
        crop = self.layout.clock.crop(image)
        if self.ocr is not None:
            enlarged = cv2.resize(crop, None, fx=8, fy=8, interpolation=cv2.INTER_CUBIC)
            gray_ocr = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
            result, _ = self.ocr(gray_ocr, use_det=False, use_cls=False, use_rec=True)
            if result:
                parsed = self._parse_digits(str(result[0][0]), float(result[0][1]))
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
            if height >= binary.shape[0] * 0.2 and width >= 2:
                boxes.append((x, y, width, height))
        boxes.sort()
        if len(boxes) < 3:
            return ClockReading(None, 0.0, None)

        chars = []
        scores = []
        for x, y, width, height in boxes:
            if width <= height * 0.25:
                continue
            glyph = binary[y : y + height, x : x + width]
            glyph = cv2.resize(glyph, (24, 36), interpolation=cv2.INTER_AREA)
            best_char, best_score = None, -1.0
            for char, template in self.templates.items():
                score = float(
                    cv2.matchTemplate(glyph, template, cv2.TM_CCOEFF_NORMED)[0, 0]
                )
                if score > best_score:
                    best_char, best_score = char, score
            chars.append(best_char)
            scores.append(best_score)
        if len(chars) not in {3, 4}:
            return ClockReading(None, max(scores, default=0.0), None)
        digits = "".join(chars)
        minutes = int(digits[:-2])
        seconds = int(digits[-2:])
        if seconds >= 60:
            return ClockReading(None, float(np.mean(scores)), digits)
        text = f"{minutes}:{seconds:02d}"
        return ClockReading(minutes * 60 + seconds, float(np.mean(scores)), text)
