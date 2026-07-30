"""Map the top-left Radiant logo to RayBet team_one or team_two."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from urllib.parse import urljoin

import cv2
import httpx
import numpy as np
from sqlalchemy.exc import SQLAlchemyError

from database.session import PostgresSession
from live_betting.raybet import SITE_URL
from live_betting.storage import LiveBettingStore
from vision.image_features import compute_phash
from vision.layouts import BroadcastLayout, STANDARD_DOTA_HUD


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _silhouette(image: np.ndarray) -> np.ndarray:
    if image.ndim == 3 and image.shape[2] == 4:
        alpha = image[:, :, 3]
        gray = cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2GRAY)
        gray = cv2.bitwise_and(gray, gray, mask=alpha)
    elif image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if float((binary > 0).mean()) > 0.5:
        binary = 255 - binary
    points = cv2.findNonZero(binary)
    if points is None:
        return np.zeros((64, 64, 3), dtype=np.uint8)
    x, y, width, height = cv2.boundingRect(points)
    content = binary[y : y + height, x : x + width]
    side = max(width, height) + 8
    canvas = np.zeros((side, side), dtype=np.uint8)
    left = (side - width) // 2
    top = (side - height) // 2
    canvas[top : top + height, left : left + width] = content
    canvas = cv2.resize(canvas, (64, 64), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)


def _team_table_logos(
    connection: PostgresSession, team_names: tuple[str, str]
) -> list[str | None]:
    rows = connection.execute(
        "SELECT name, tag, logo_url FROM teams WHERE logo_url IS NOT NULL AND logo_url != ''"
    ).fetchall()
    output: list[str | None] = []
    for team_name in team_names:
        target = _normalize(team_name)
        name_match = next(
            (str(row[2]) for row in rows if _normalize(str(row[0] or "")) == target),
            None,
        )
        tag_match = next(
            (str(row[2]) for row in rows if _normalize(str(row[1] or "")) == target),
            None,
        )
        output.append(name_match or tag_match)
    return output


def _raw_raybet_logos(payload: object) -> list[str | None]:
    if not isinstance(payload, dict):
        return [None, None]
    by_position: dict[int, str] = {}
    for team in payload.get("team") or []:
        if not isinstance(team, dict) or not team.get("team_logo"):
            continue
        try:
            position = int(team.get("pos"))
        except (TypeError, ValueError):
            continue
        if position in {1, 2}:
            by_position[position] = urljoin(SITE_URL, str(team["team_logo"]))
    return [by_position.get(1), by_position.get(2)]


@dataclass(frozen=True)
class TeamSideReading:
    radiant_team_side: str | None
    confidence: float


class TeamSideRecognizer:
    min_logo_similarity = 0.75
    min_assignment_margin = 0.04

    def __init__(
        self,
        team_one_logo: np.ndarray,
        team_two_logo: np.ndarray,
        layout: BroadcastLayout = STANDARD_DOTA_HUD,
    ) -> None:
        if layout.radiant_team_logo is None or layout.dire_team_logo is None:
            raise ValueError("layout does not define team logo regions")
        self.team_one_hash = compute_phash(_silhouette(team_one_logo), hash_size=8)
        self.team_two_hash = compute_phash(_silhouette(team_two_logo), hash_size=8)
        self.layout = layout

    @staticmethod
    def from_database(
        database_url: str, match_id: str
    ) -> "TeamSideRecognizer | None":
        with LiveBettingStore(database_url) as store:
            connection = store.connection
            match = connection.execute(
                "SELECT team_one, team_two, raw_json FROM raybet_matches "
                "WHERE raybet_match_id=?",
                (match_id,),
            ).fetchone()
            if not match:
                return None
            team_names = (str(match[0] or ""), str(match[1] or ""))
            try:
                table_urls = _team_table_logos(connection, team_names)
            except SQLAlchemyError:
                table_urls = [None, None]
            try:
                raw_payload = json.loads(str(match[2] or "{}"))
            except (TypeError, ValueError):
                raw_payload = {}
            fallback_urls = _raw_raybet_logos(raw_payload)
            candidates = [
                tuple(
                    dict.fromkeys(
                        url for url in (table_urls[index], fallback_urls[index]) if url
                    )
                )
                for index in range(2)
            ]
        if not all(candidates):
            return None
        images: list[np.ndarray] = []
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            for urls in candidates:
                image = None
                for url in urls:
                    try:
                        response = client.get(str(url))
                        response.raise_for_status()
                        image = cv2.imdecode(
                            np.frombuffer(response.content, np.uint8),
                            cv2.IMREAD_UNCHANGED,
                        )
                    except (httpx.HTTPError, cv2.error, ValueError):
                        image = None
                    if image is not None:
                        break
                if image is None:
                    return None
                images.append(image)
        return TeamSideRecognizer(images[0], images[1])

    @staticmethod
    def _similarity(one: np.ndarray, two: np.ndarray) -> float:
        return 1.0 - float(np.mean(one != two))

    def read(self, image: np.ndarray) -> TeamSideReading:
        left = compute_phash(
            _silhouette(self.layout.radiant_team_logo.crop(image)), hash_size=8
        )
        right = compute_phash(
            _silhouette(self.layout.dire_team_logo.crop(image)), hash_size=8
        )
        left_one = self._similarity(left, self.team_one_hash)
        left_two = self._similarity(left, self.team_two_hash)
        right_one = self._similarity(right, self.team_one_hash)
        right_two = self._similarity(right, self.team_two_hash)
        direct = left_one + right_two
        swapped = left_two + right_one
        margin = abs(direct - swapped)
        if direct > swapped:
            matched = (left_one, right_two)
            assignment_margins = (left_one - left_two, right_two - right_one)
        else:
            matched = (left_two, right_one)
            assignment_margins = (left_two - left_one, right_one - right_two)
        absolute_similarity = min(matched)
        if absolute_similarity < self.min_logo_similarity:
            return TeamSideReading(None, min(0.89, absolute_similarity))
        if min(assignment_margins) < self.min_assignment_margin:
            return TeamSideReading(None, min(0.89, 0.5 + margin * 2))
        if margin < self.min_assignment_margin * 2:
            return TeamSideReading(None, min(0.89, 0.5 + margin * 2))
        return TeamSideReading(
            "team_one" if direct > swapped else "team_two",
            min(0.99, 0.80 + margin + absolute_similarity * 0.15),
        )


class TeamSideTracker:
    def __init__(self, confirmations: int = 3) -> None:
        self._recent: deque[TeamSideReading] = deque(maxlen=confirmations)

    def reset(self) -> None:
        self._recent.clear()

    def update(self, reading: TeamSideReading) -> TeamSideReading | None:
        if reading.radiant_team_side is None:
            self._recent.clear()
            return None
        self._recent.append(reading)
        if len(self._recent) < self._recent.maxlen:
            return None
        if len({row.radiant_team_side for row in self._recent}) != 1:
            return None
        return TeamSideReading(
            reading.radiant_team_side, min(row.confidence for row in self._recent)
        )
