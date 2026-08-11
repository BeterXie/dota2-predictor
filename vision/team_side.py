"""Map the top-left Radiant logo to RayBet team_one or team_two."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import cv2
import httpx
import numpy as np
from sqlalchemy.exc import SQLAlchemyError

from database.session import PostgresSession
from live_betting.raybet import SITE_URL
from live_betting.storage import LiveBettingStore
from vision.image_features import compute_phash
from vision.layouts import BroadcastLayout, NormalizedRegion, STANDARD_DOTA_HUD


ALLOWED_TEAM_LOGO_HOSTS = frozenset(
    {
        "cdn.cloudflare.steamstatic.com",
        "cdn.steamusercontent.com",
        "images.opendota.com",
        "steamcdn-a.akamaihd.net",
        "steamusercontent-a.akamaihd.net",
        "www.ray086.com",
    }
)
MAX_TEAM_LOGO_BYTES = 2 * 1024 * 1024
MAX_TEAM_LOGO_REDIRECTS = 3


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _valid_logo_content_type(value: object) -> bool:
    content_type = str(value or "").partition(";")[0].strip().casefold()
    return content_type.startswith("image/") or content_type == "application/octet-stream"


def _validate_team_logo_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid team logo URL") from error
    hostname = parsed.hostname.casefold() if parsed.hostname is not None else None
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in ALLOWED_TEAM_LOGO_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise ValueError("invalid team logo URL")
    return url


def _download_team_logo(client: httpx.Client, url: str) -> np.ndarray | None:
    current = _validate_team_logo_url(url)
    for redirect_count in range(MAX_TEAM_LOGO_REDIRECTS + 1):
        with client.stream("GET", current) as response:
            status_code = int(response.status_code)
            if status_code in {301, 302, 303, 307, 308}:
                if redirect_count >= MAX_TEAM_LOGO_REDIRECTS:
                    raise ValueError("team logo redirect limit exceeded")
                location = response.headers.get("location")
                if not location:
                    raise ValueError("team logo redirect is missing location")
                current = _validate_team_logo_url(urljoin(current, location))
                continue
            response.raise_for_status()
            if not _valid_logo_content_type(
                response.headers.get("content-type")
            ):
                return None
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError) as error:
                    raise ValueError("invalid team logo content length") from error
                if declared_length < 0 or declared_length > MAX_TEAM_LOGO_BYTES:
                    raise ValueError("team logo response is too large")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_TEAM_LOGO_BYTES:
                    raise ValueError("team logo response is too large")
                chunks.append(chunk)
        return cv2.imdecode(
            np.frombuffer(b"".join(chunks), np.uint8),
            cv2.IMREAD_UNCHANGED,
        )
    raise ValueError("team logo redirect limit exceeded")


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
        "SELECT name, tag, logo_url FROM teams "
        "WHERE logo_url IS NOT NULL AND logo_url != '' "
        "ORDER BY updated_at DESC, team_id DESC"
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


@dataclass(frozen=True)
class TeamSideRecognizerLoad:
    recognizer: TeamSideRecognizer | None
    error: str | None


class TeamSideRecognizer:
    min_logo_similarity = 0.75
    min_assignment_margin = 0.04
    min_team_name_confidence = 0.80

    def __init__(
        self,
        team_one_logo: np.ndarray | None,
        team_two_logo: np.ndarray | None,
        layout: BroadcastLayout = STANDARD_DOTA_HUD,
        *,
        team_names: tuple[str, str] | None = None,
        use_ocr: bool = True,
    ) -> None:
        if (team_one_logo is None) != (team_two_logo is None):
            raise ValueError("team logo templates must be both present or both absent")
        logo_mode = team_one_logo is not None and team_two_logo is not None
        if logo_mode and (
            layout.radiant_team_logo is None or layout.dire_team_logo is None
        ):
            raise ValueError("layout does not define team logo regions")
        self.layout = layout
        self.team_name_keys = (
            tuple(_normalize(name) for name in team_names)
            if team_names is not None
            else None
        )
        if not logo_mode and (
            self.team_name_keys is None
            or any(not name for name in self.team_name_keys)
            or self.team_name_keys[0] == self.team_name_keys[1]
            or layout.radiant_team_name is None
            or layout.dire_team_name is None
        ):
            raise ValueError("name-only team-side recognition requires two distinct names")
        self.team_one_hash = (
            compute_phash(_silhouette(team_one_logo), hash_size=8)
            if team_one_logo is not None
            else None
        )
        self.team_two_hash = (
            compute_phash(_silhouette(team_two_logo), hash_size=8)
            if team_two_logo is not None
            else None
        )
        self.use_ocr = use_ocr
        self.ocr = None
        self._ensure_ocr()

    def _ensure_ocr(self) -> None:
        if (
            self.use_ocr
            and self.ocr is None
            and self.team_name_keys is not None
            and self.layout.radiant_team_name is not None
            and self.layout.dire_team_name is not None
        ):
            try:
                from rapidocr_onnxruntime import RapidOCR

                self.ocr = RapidOCR()
            except ImportError:
                pass

    def set_layout(self, layout: BroadcastLayout) -> None:
        if self.team_one_hash is None:
            if layout.radiant_team_name is None or layout.dire_team_name is None:
                raise ValueError("layout does not define team name regions")
        elif layout.radiant_team_logo is None or layout.dire_team_logo is None:
            raise ValueError("layout does not define team logo regions")
        self.layout = layout
        self._ensure_ocr()

    @staticmethod
    def from_database(
        database_url: str, match_id: str
    ) -> "TeamSideRecognizer | None":
        return TeamSideRecognizer.load_from_database(database_url, match_id).recognizer

    @staticmethod
    def load_from_database(
        database_url: str, match_id: str
    ) -> TeamSideRecognizerLoad:
        with LiveBettingStore(database_url) as store:
            connection = store.connection
            match = connection.execute(
                "SELECT team_one, team_two, raw_json FROM raybet_matches "
                "WHERE raybet_match_id=?",
                (match_id,),
            ).fetchone()
            if not match:
                return TeamSideRecognizerLoad(None, "raybet_match_missing")
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
        images: list[np.ndarray | None] = []
        logo_errors: list[str] = []
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            for index, urls in enumerate(candidates):
                team = "one" if index == 0 else "two"
                if not urls:
                    images.append(None)
                    logo_errors.append(f"team_{team}_logo_missing")
                    continue
                image = None
                for url in urls:
                    try:
                        image = _download_team_logo(client, str(url))
                    except (httpx.HTTPError, cv2.error, ValueError):
                        image = None
                    if image is not None:
                        break
                if image is None:
                    logo_errors.append(f"team_{team}_logo_invalid")
                images.append(image)
        if all(image is not None for image in images):
            return TeamSideRecognizerLoad(
                TeamSideRecognizer(images[0], images[1], team_names=team_names),
                None,
            )
        try:
            recognizer = TeamSideRecognizer(
                None,
                None,
                team_names=team_names,
            )
        except ValueError:
            return TeamSideRecognizerLoad(None, ",".join(logo_errors))
        if recognizer.ocr is None:
            return TeamSideRecognizerLoad(None, ",".join(logo_errors))
        return TeamSideRecognizerLoad(
            recognizer,
            f"{','.join(logo_errors)}:team_name_ocr_fallback",
        )

    @staticmethod
    def _similarity(one: np.ndarray, two: np.ndarray) -> float:
        return 1.0 - float(np.mean(one != two))

    @staticmethod
    def _ocr_left_edge(item: object) -> float:
        try:
            points = np.asarray(item[0], dtype=np.float32)  # type: ignore[index]
        except (IndexError, TypeError, ValueError):
            return float("inf")
        if points.size == 0:
            return float("inf")
        if points.ndim == 1:
            return float(points[0])
        return float(np.min(points[..., 0]))

    def _team_name_readings(
        self,
        image: np.ndarray,
        region: NormalizedRegion | None,
    ) -> tuple[tuple[str, float], ...]:
        if self.ocr is None or region is None:
            return ()
        crop = region.crop(image)
        if crop.size == 0:
            return ()
        enlarged = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        result, _ = self.ocr(enlarged)
        ordered = sorted(
            (item for item in result or () if len(item) >= 3),
            key=self._ocr_left_edge,
        )
        readings = tuple(
            (_normalize(str(item[1])), float(item[2]))
            for item in ordered
            if _normalize(str(item[1]))
        )
        if len(readings) <= 1:
            return readings
        combined = (
            "".join(text for text, _ in readings),
            min(score for _, score in readings),
        )
        return readings + (combined,)

    def _read_team_names(self, image: np.ndarray) -> TeamSideReading | None:
        if self.team_name_keys is None:
            return None
        left = self._team_name_readings(image, self.layout.radiant_team_name)
        right = self._team_name_readings(image, self.layout.dire_team_name)

        def confidence(readings: tuple[tuple[str, float], ...], key: str) -> float:
            return max((score for text, score in readings if text == key), default=0.0)

        team_one, team_two = self.team_name_keys
        direct = min(confidence(left, team_one), confidence(right, team_two))
        swapped = min(confidence(left, team_two), confidence(right, team_one))
        if (
            direct >= self.min_team_name_confidence
            and swapped < self.min_team_name_confidence
        ):
            return TeamSideReading("team_one", min(0.99, direct))
        if (
            swapped >= self.min_team_name_confidence
            and direct < self.min_team_name_confidence
        ):
            return TeamSideReading("team_two", min(0.99, swapped))
        return None

    def read(self, image: np.ndarray) -> TeamSideReading:
        if self.team_one_hash is None or self.team_two_hash is None:
            return self._read_team_names(image) or TeamSideReading(None, 0.0)
        assert self.layout.radiant_team_logo is not None
        assert self.layout.dire_team_logo is not None
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
            return self._read_team_names(image) or TeamSideReading(
                None, min(0.89, absolute_similarity)
            )
        if min(assignment_margins) < self.min_assignment_margin:
            return self._read_team_names(image) or TeamSideReading(
                None, min(0.89, 0.5 + margin * 2)
            )
        if margin < self.min_assignment_margin * 2:
            return self._read_team_names(image) or TeamSideReading(
                None, min(0.89, 0.5 + margin * 2)
            )
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
