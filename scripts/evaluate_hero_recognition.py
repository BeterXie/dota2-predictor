"""Evaluate per-slot hero recognition against saved Vision evidence frames."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import fmean

import cv2
from sqlalchemy import text
from sqlalchemy.engine import Connection


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vision.hero_recognizer import (  # noqa: E402
    DEFAULT_FEATURE_PATH,
    DraftReading,
    DraftTracker,
    HeroRecognizer,
)
from vision.frame_quality import FrameQualityTracker  # noqa: E402
from vision.layout_selector import select_broadcast_layout  # noqa: E402
from vision.layout_tracker import LayoutTracker  # noqa: E402
from vision.scoreboard_reader import ScoreboardReader  # noqa: E402
from vision.screen_state import classify_screen_state  # noqa: E402
from vision.stable_runtime import (  # noqa: E402
    StableDraftTracker,
    StableHeroRecognizer,
    _LAYOUTS,
    broadcast_layout_scores,
)
from database.engine import build_engine  # noqa: E402
from shared.environment import load_environment_file  # noqa: E402


@dataclass(frozen=True)
class EvidenceSample:
    path: Path
    observed_at: float
    source_frame_hash: str | None = None
    game_clock_seconds: int | None = None
    target_identity_confirmed: bool | None = None


def _observation_samples(
    observation_jsonl: Path,
    *,
    captured_after: float | None = None,
    captured_before: float | None = None,
) -> tuple[list[EvidenceSample], dict[str, object]]:
    samples: list[EvidenceSample] = []
    raybet_match_ids: set[str] = set()
    map_numbers: set[int] = set()
    missing_frames = 0
    invalid_rows = 0
    selected_clocks: list[int | None] = []

    try:
        lines = observation_jsonl.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"unable to read observation JSONL: {observation_jsonl}") from error

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"invalid observation JSONL row {line_number}: {observation_jsonl}"
            ) from error
        if not isinstance(row, dict):
            invalid_rows += 1
            continue

        match_id = row.get("raybet_match_id")
        if match_id is not None:
            raybet_match_ids.add(str(match_id))
        map_number = row.get("map_number")
        if map_number is not None:
            try:
                map_numbers.add(int(map_number))
            except (TypeError, ValueError):
                invalid_rows += 1
                continue

        try:
            observed_at = datetime.fromisoformat(
                str(row["captured_at_utc"]).replace("Z", "+00:00")
            ).timestamp()
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"invalid captured_at_utc on JSONL row {line_number}"
            ) from error
        if captured_after is not None and observed_at < captured_after:
            continue
        if captured_before is not None and observed_at >= captured_before:
            continue
        digest_value = row.get("source_frame_sha256")
        path_value = row.get("source_frame_path")
        if not digest_value or not path_value:
            missing_frames += 1
            continue
        path = Path(str(path_value))
        if not path.is_file():
            missing_frames += 1
            continue
        clock_value = row.get("game_clock_seconds")
        try:
            clock = None if clock_value is None else int(clock_value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"invalid game_clock_seconds on JSONL row {line_number}"
            ) from error
        selected_clocks.append(clock)
        samples.append(
            EvidenceSample(
                path,
                observed_at,
                str(digest_value),
                clock,
                bool(row.get("radiant_team_side")),
            )
        )

    if len(raybet_match_ids) != 1:
        raise ValueError("observation JSONL must contain exactly one RayBet match")
    if len(map_numbers) > 1:
        raise ValueError("observation JSONL spans multiple numbered maps")
    if not samples:
        raise ValueError("observation JSONL has no retained Vision evidence frames")
    _validate_single_map_clocks(selected_clocks)
    return samples, {
        "source": "observation_jsonl",
        "observation_jsonl": str(observation_jsonl.resolve()),
        "raybet_match_id": next(iter(raybet_match_ids)),
        "map_numbers": sorted(map_numbers),
        "jsonl_rows": len(lines),
        "retained_frames": len(samples),
        "missing_frames": missing_frames,
        "invalid_rows": invalid_rows,
        "captured_after": (
            datetime.fromtimestamp(captured_after).astimezone().isoformat()
            if captured_after is not None
            else None
        ),
        "captured_before": (
            datetime.fromtimestamp(captured_before).astimezone().isoformat()
            if captured_before is not None
            else None
        ),
    }


@dataclass(frozen=True)
class DraftTruthMapping:
    raybet_match_id: str
    raybet_map_number: int
    opendota_match_id: int
    mapping_source: str
    raybet_team_one_name: str
    raybet_team_two_name: str
    team_one_id: int
    team_two_id: int
    opendota_radiant_team_id: int
    opendota_dire_team_id: int
    mapping_id: int | None = None

    def report_context(self, truth_hero_count: int) -> dict[str, object]:
        return {
            "raybet_match_id": self.raybet_match_id,
            "raybet_map_number": self.raybet_map_number,
            "opendota_match_id": self.opendota_match_id,
            "mapping_source": self.mapping_source,
            "mapping_id": self.mapping_id,
            "raybet_team_one_name": self.raybet_team_one_name,
            "raybet_team_two_name": self.raybet_team_two_name,
            "team_one_id": self.team_one_id,
            "team_two_id": self.team_two_id,
            "opendota_radiant_team_id": self.opendota_radiant_team_id,
            "opendota_dire_team_id": self.opendota_dire_team_id,
            "truth_hero_count": truth_hero_count,
        }


def _directory_samples(evidence_dir: Path) -> list[EvidenceSample]:
    paths = sorted(
        path
        for pattern in ("*.jpg", "*.jpeg", "*.png")
        for path in evidence_dir.glob(pattern)
    )
    return [EvidenceSample(path, path.stat().st_mtime) for path in paths]


def _validate_exact_mapping(
    *,
    raybet_match_id: str,
    map_number: int,
    opendota_match_id: int,
    mapping_source: str,
    raybet_team_one_name: str,
    raybet_team_two_name: str,
    team_one_id: int,
    team_two_id: int,
    radiant_team_id: int,
    dire_team_id: int,
    mapping_id: int | None = None,
) -> DraftTruthMapping:
    expected = (team_one_id, team_two_id)
    actual = (radiant_team_id, dire_team_id)
    if mapping_source not in {"manual_exact", "persisted_exact"}:
        raise ValueError("mapping source is not exact")
    if not raybet_team_one_name.strip() or not raybet_team_two_name.strip():
        raise ValueError("RayBet mapping requires both team names")
    if map_number <= 0:
        raise ValueError("RayBet map number must be positive")
    if any(team_id <= 0 for team_id in expected + actual):
        raise ValueError("exact mapping requires four positive team IDs")
    if team_one_id == team_two_id or radiant_team_id == dire_team_id:
        raise ValueError("exact mapping requires two unique teams")
    if set(expected) != set(actual):
        raise ValueError("RayBet and OpenDota team IDs do not match exactly")
    return DraftTruthMapping(
        raybet_match_id=raybet_match_id,
        raybet_map_number=map_number,
        opendota_match_id=opendota_match_id,
        mapping_source=mapping_source,
        raybet_team_one_name=raybet_team_one_name,
        raybet_team_two_name=raybet_team_two_name,
        team_one_id=team_one_id,
        team_two_id=team_two_id,
        opendota_radiant_team_id=radiant_team_id,
        opendota_dire_team_id=dire_team_id,
        mapping_id=mapping_id,
    )


def _exact_mapping(
    connection: Connection,
    *,
    raybet_match_id: str,
    map_number: int,
    opendota_match_id: int,
    mapping_source: str,
    manual_team_one_id: int | None,
    manual_team_two_id: int | None,
) -> DraftTruthMapping:
    raybet = connection.execute(
        text(
            """
            SELECT team_one, team_two
            FROM raybet_matches
            WHERE raybet_match_id = :raybet_match_id
            """
        ),
        {"raybet_match_id": raybet_match_id},
    ).mappings().one_or_none()
    if (
        raybet is None
        or raybet["team_one"] is None
        or raybet["team_two"] is None
    ):
        raise ValueError("RayBet match requires both team names")
    opendota = connection.execute(
        text(
            """
            SELECT radiant_team_id, dire_team_id
            FROM matches
            WHERE match_id = :match_id
            """
        ),
        {"match_id": opendota_match_id},
    ).mappings().one_or_none()
    if (
        opendota is None
        or opendota["radiant_team_id"] is None
        or opendota["dire_team_id"] is None
    ):
        raise ValueError("OpenDota match requires both team IDs")

    mapping_id = None
    if mapping_source == "manual_exact":
        if manual_team_one_id is None or manual_team_two_id is None:
            raise ValueError("manual_exact requires both explicit team IDs")
        team_one_id = manual_team_one_id
        team_two_id = manual_team_two_id
    elif mapping_source == "persisted_exact":
        mappings = connection.execute(
            text(
                """
                SELECT mapping.mapping_id,
                       mapping.canonical_team_one_id,
                       mapping.canonical_team_two_id,
                       mapping.acceptance_mode
                FROM strict_live_map_mappings AS mapping
                LEFT JOIN strict_live_map_mapping_invalidations AS invalidation
                  ON invalidation.mapping_id = mapping.mapping_id
                WHERE mapping.raybet_match_id = :raybet_match_id
                  AND mapping.map_number = :map_number
                  AND invalidation.invalidation_id IS NULL
                ORDER BY mapping.mapping_id
                LIMIT 2
                """
            ),
            {
                "raybet_match_id": raybet_match_id,
                "map_number": map_number,
            },
        ).mappings().all()
        if len(mappings) != 1:
            raise ValueError("persisted exact mapping must resolve to exactly one row")
        persisted = mappings[0]
        if persisted["acceptance_mode"] not in {"manual_exact", "automatic_exact"}:
            raise ValueError("persisted mapping is not exact")
        mapping_id = int(persisted["mapping_id"])
        team_one_id = int(persisted["canonical_team_one_id"])
        team_two_id = int(persisted["canonical_team_two_id"])
    else:
        raise ValueError("mapping_source must be manual_exact or persisted_exact")

    return _validate_exact_mapping(
        raybet_match_id=raybet_match_id,
        map_number=map_number,
        opendota_match_id=opendota_match_id,
        mapping_source=mapping_source,
        raybet_team_one_name=str(raybet["team_one"]),
        raybet_team_two_name=str(raybet["team_two"]),
        team_one_id=team_one_id,
        team_two_id=team_two_id,
        radiant_team_id=int(opendota["radiant_team_id"]),
        dire_team_id=int(opendota["dire_team_id"]),
        mapping_id=mapping_id,
    )


def _database_samples(
    database_url: str | None,
    *,
    raybet_match_id: str,
    map_number: int,
    opendota_match_id: int,
    evidence_root: Path,
    mapping_source: str,
    manual_team_one_id: int | None = None,
    manual_team_two_id: int | None = None,
    captured_after: float | None = None,
    captured_before: float | None = None,
) -> tuple[list[EvidenceSample], tuple[int, ...], DraftTruthMapping]:
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            mapping = _exact_mapping(
                connection,
                raybet_match_id=raybet_match_id,
                map_number=map_number,
                opendota_match_id=opendota_match_id,
                mapping_source=mapping_source,
                manual_team_one_id=manual_team_one_id,
                manual_team_two_id=manual_team_two_id,
            )
            observations = connection.execute(
                text(
                    """
                    SELECT captured_at, game_clock_seconds, source_frame_sha256,
                           radiant_team_side
                    FROM vision_observations
                    WHERE raybet_match_id = :raybet_match_id
                      AND map_number = :map_number
                      AND screen_state = 'game'
                      AND source_frame_sha256 IS NOT NULL
                    ORDER BY captured_at
                    """
                ),
                {
                    "raybet_match_id": raybet_match_id,
                    "map_number": map_number,
                },
            ).mappings().all()
            truth = tuple(
                int(row.hero_id)
                for row in connection.execute(
                    text(
                        """
                        SELECT hero_id
                        FROM match_players
                        WHERE match_id = :match_id
                        ORDER BY player_slot
                        """
                    ),
                    {"match_id": opendota_match_id},
                )
            )
    finally:
        engine.dispose()
    if len(truth) != 10 or len(set(truth)) != 10:
        raise ValueError("OpenDota truth must contain ten unique heroes")
    content_root = (
        evidence_root
        if evidence_root.name == "sha256"
        else evidence_root / "sha256"
    )
    samples: list[EvidenceSample] = []
    selected_clocks: list[int | None] = []
    for row in observations:
        observed_at = datetime.fromisoformat(str(row["captured_at"])).timestamp()
        if captured_after is not None and observed_at < captured_after:
            continue
        if captured_before is not None and observed_at >= captured_before:
            continue
        digest = str(row["source_frame_sha256"])
        selected_clocks.append(
            None
            if row["game_clock_seconds"] is None
            else int(row["game_clock_seconds"])
        )
        samples.append(
            EvidenceSample(
                content_root / digest[:2] / f"{digest}.jpg",
                observed_at,
                digest,
                selected_clocks[-1],
                row["radiant_team_side"] is not None,
            )
        )
    if not samples:
        raise ValueError("exact mapping has no Vision evidence frames")
    _validate_single_map_clocks(selected_clocks)
    return samples, truth, mapping


def _validate_single_map_clocks(clocks: list[int | None]) -> None:
    previous = None
    for current in clocks:
        if current is None:
            continue
        if previous is not None and current < previous - 180:
            raise ValueError("Vision evidence crosses a game clock reset")
        previous = current


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _mean(values: list[float]) -> float:
    return round(fmean(values), 6) if values else 0.0


def _render_variant_usage(
    usage: dict[tuple[int, str], Counter[str]],
    variant_counts: dict[int, int],
    *,
    include_truth: bool,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for (hero_id, variant), counts in sorted(usage.items()):
        row: dict[str, object] = {
            "hero_id": hero_id,
            "variant": variant,
            "hero_variant_count": variant_counts[hero_id],
            "selected": counts["selected"],
            "accepted": counts["accepted"],
        }
        if include_truth:
            row.update(
                {
                    "correct": counts["correct"],
                    "wrong": counts["wrong"],
                    "accepted_correct": counts["accepted_correct"],
                    "accepted_wrong": counts["accepted_wrong"],
                }
            )
        rows.append(row)
    return rows


def evaluate(
    evidence_dir: Path,
    feature_path: Path,
    *,
    samples: list[EvidenceSample] | None = None,
    truth_hero_ids: tuple[int, ...] | None = None,
    truth_context: dict[str, object] | None = None,
    stable: bool = False,
    layout_profile: str | None = None,
    runtime_gates: bool = True,
) -> dict[str, object]:
    samples = samples if samples is not None else _directory_samples(evidence_dir)
    if truth_hero_ids is not None and len(truth_hero_ids) != 10:
        raise ValueError("truth_hero_ids must contain exactly ten heroes")
    if truth_hero_ids is not None and len(set(truth_hero_ids)) != 10:
        raise ValueError("truth_hero_ids must contain ten unique heroes")
    layout_counts: Counter[str] = Counter()
    scene_counts: Counter[str] = Counter()
    replay_gate_counts: Counter[str] = Counter()
    tracking_blocker_counts: Counter[str] = Counter()
    if layout_profile is not None and layout_profile not in _LAYOUTS:
        raise ValueError(f"unsupported layout profile: {layout_profile}")
    readers: dict[str, HeroRecognizer] = {}
    scoreboard_readers: dict[str, ScoreboardReader] = {}
    tracker = StableDraftTracker() if stable else DraftTracker()
    layout_tracker = LayoutTracker() if stable and layout_profile is None else None
    frame_quality_tracker = FrameQualityTracker() if stable else None
    active_layout_name: str | None = None
    slot_rows: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    frame_rows: list[dict[str, object]] = []
    state_rows: defaultdict[str, list[bool]] = defaultdict(list)
    best_confusion: Counter[tuple[int, int]] = Counter()
    accepted_confusion: Counter[tuple[int, int]] = Counter()
    tracker_confusion: Counter[tuple[int, int]] = Counter()
    variant_usage: defaultdict[tuple[int, str], Counter[str]] = defaultdict(Counter)
    variant_counts: dict[int, int] = {}
    previous_tracker_slots = tracker.slot_statuses
    wrong_locks: list[dict[str, object]] = []
    tracker_confirmation_index: int | None = None
    tracker_confirmation_at: float | None = None
    tracker_confirmation_trackable_index: int | None = None
    first_trackable_at: float | None = None
    trackable_frame_count = 0
    exact_post_lock_frames = 0
    post_lock_frames = 0
    layout_switches: list[dict[str, object]] = []

    for sample in samples:
        path = sample.path
        image = cv2.imread(str(path))
        if image is None:
            layout_counts["unreadable"] += 1
            continue
        if layout_profile is not None:
            layout = _LAYOUTS[layout_profile]
            layout_name = layout.name
        elif layout_tracker is not None:
            layout_state = layout_tracker.update(broadcast_layout_scores(image))
            layout = _LAYOUTS.get(layout_state.layout_name or "")
            layout_name = layout.name if layout is not None else "unsupported"
        else:
            selection = select_broadcast_layout(image)
            layout = selection.layout
            layout_name = selection.layout_name or "unsupported"
        layout_counts[layout_name] += 1
        if stable:
            if (
                layout is not None
                and active_layout_name is not None
                and active_layout_name != layout_name
            ):
                layout_switches.append(
                    {
                        "file": path.name,
                        "from": active_layout_name,
                        "to": layout_name,
                    }
                )
                tracker.reset()
                previous_tracker_slots = tracker.slot_statuses
            if layout is not None:
                active_layout_name = layout_name
        elif active_layout_name != (layout.name if layout is not None else None):
            tracker.reset()
            previous_tracker_slots = tracker.slot_statuses
            active_layout_name = layout.name if layout is not None else None
        if layout is None:
            continue
        if stable and runtime_gates:
            screen_state, _ = classify_screen_state(image, layout)
            scene_counts[screen_state] += 1
            scoreboard_reader = scoreboard_readers.get(layout_name)
            if scoreboard_reader is None:
                scoreboard_reader = ScoreboardReader(layout)
                scoreboard_readers[layout_name] = scoreboard_reader
            replay_gate = scoreboard_reader.read_replay_gate(image)
            replay_gate_counts[replay_gate.status] += 1
            assert frame_quality_tracker is not None
            quality = frame_quality_tracker.assess(image)
            target_identity_confirmed = sample.target_identity_confirmed is not False
            if screen_state != "game":
                tracking_blocker = "screen_not_game"
            elif replay_gate.status != "live":
                tracking_blocker = f"replay_gate_{replay_gate.status}"
            elif not quality.usable:
                tracking_blocker = quality.reason or "frame_quality_unusable"
            elif not target_identity_confirmed:
                tracking_blocker = "target_identity_unconfirmed"
            else:
                tracking_blocker = None
            tracking_blocker_counts[tracking_blocker or "trackable"] += 1
            trackable = tracking_blocker is None
        else:
            screen_state = "unfiltered"
            target_identity_confirmed = sample.target_identity_confirmed is not False
            tracking_blocker = None
            trackable = True
        if trackable:
            if first_trackable_at is None:
                first_trackable_at = sample.observed_at
            trackable_frame_count += 1
        reader = readers.get(layout_name)
        if reader is None:
            reader_type = StableHeroRecognizer if stable else HeroRecognizer
            reader = reader_type(feature_path, layout)
            readers[layout_name] = reader
        reading = reader.read(image)
        if stable and runtime_gates and trackable:
            max_seconds = layout.draft_recognition_max_clock_seconds
            tracking_reading = (
                DraftReading((), (), 0.0)
                if max_seconds is not None
                and (
                    sample.game_clock_seconds is None
                    or sample.game_clock_seconds > max_seconds
                )
                else reading
            )
            confirmed = tracker.update(
                tracking_reading,
                observed_at=sample.observed_at,
                source_frame_hash=sample.source_frame_hash,
                game_clock_seconds=sample.game_clock_seconds,
            )
        elif stable and runtime_gates:
            confirmed = tracker.current_draft
        else:
            confirmed = tracker.update(
                reading,
                observed_at=sample.observed_at,
                source_frame_hash=sample.source_frame_hash,
                game_clock_seconds=sample.game_clock_seconds,
            )
        accepted = 0
        regions = layout.radiant_heroes + layout.dire_heroes
        for index, diagnostic in enumerate(reading.slot_diagnostics):
            key = f"{diagnostic.side}_{diagnostic.slot}"
            row = {
                "accepted": diagnostic.accepted,
                "best_hero_id": diagnostic.best_hero_id,
                "best_variant": diagnostic.best_variant,
                "best_score": diagnostic.best_score,
                "margin": diagnostic.margin,
                "reason": diagnostic.reason,
            }
            slot_rows[key].append(row)
            accepted += int(diagnostic.accepted)
            if (
                diagnostic.best_hero_id is not None
                and diagnostic.best_variant is not None
            ):
                usage_key = (diagnostic.best_hero_id, diagnostic.best_variant)
                counts = variant_usage[usage_key]
                variant_counts[diagnostic.best_hero_id] = diagnostic.hero_variant_count
                counts["selected"] += 1
                counts["accepted"] += int(diagnostic.accepted)
                if truth_hero_ids is not None:
                    correct = diagnostic.best_hero_id == truth_hero_ids[index]
                    counts["correct" if correct else "wrong"] += 1
                    if diagnostic.accepted:
                        counts[
                            "accepted_correct" if correct else "accepted_wrong"
                        ] += 1
            if truth_hero_ids is not None and diagnostic.best_hero_id is not None:
                truth_id = truth_hero_ids[index]
                best_confusion[(truth_id, diagnostic.best_hero_id)] += 1
                if diagnostic.accepted:
                    accepted_confusion[(truth_id, diagnostic.best_hero_id)] += 1

            crop = regions[index].crop(image)
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            saturation = float(hsv[:, :, 1].mean())
            state = "low_saturation_proxy" if saturation < 80.0 else "normal_color"
            state_rows[state].append(diagnostic.accepted)
        tracker_slots = tracker.slot_statuses
        if truth_hero_ids is not None:
            for index, status in enumerate(tracker_slots):
                previous = previous_tracker_slots[index]
                if status.state != "locked" or previous.state == "locked":
                    continue
                assert status.hero_id is not None
                truth_id = truth_hero_ids[index]
                tracker_confusion[(truth_id, status.hero_id)] += 1
                if status.hero_id != truth_id:
                    wrong_locks.append(
                        {
                            "file": path.name,
                            "observed_at": datetime.fromtimestamp(
                                sample.observed_at
                            ).astimezone().isoformat(),
                            "side": status.side,
                            "slot": status.slot,
                            "truth_hero_id": truth_id,
                            "locked_hero_id": status.hero_id,
                            "unique_crop_cluster_count": (
                                status.unique_crop_cluster_count
                            ),
                        }
                    )
        previous_tracker_slots = tracker_slots
        if confirmed is not None and tracker_confirmation_index is None:
            tracker_confirmation_index = len(frame_rows)
            tracker_confirmation_at = sample.observed_at
            tracker_confirmation_trackable_index = trackable_frame_count - 1
        if tracker_confirmation_index is not None:
            post_lock_frames += 1
            if truth_hero_ids is not None and confirmed is not None:
                exact_post_lock_frames += int(
                    confirmed.radiant_hero_ids + confirmed.dire_hero_ids
                    == truth_hero_ids
                )
        frame_rows.append(
            {
                "file": path.name,
                "layout": layout_name,
                "screen_state": screen_state,
                "target_identity_confirmed": target_identity_confirmed,
                "tracking_blocker": tracking_blocker,
                "trackable": trackable,
                "recognized_slots": accepted,
                "complete": len(reading.radiant_hero_ids) == 5
                and len(reading.dire_hero_ids) == 5,
                "tracker_confirmed": confirmed is not None,
                "tracker_hero_ids": (
                    list(confirmed.radiant_hero_ids + confirmed.dire_hero_ids)
                    if confirmed is not None
                    else None
                ),
            }
        )

    slot_summary: dict[str, object] = {}
    for key, rows in sorted(slot_rows.items()):
        accepted_count = sum(bool(row["accepted"]) for row in rows)
        reasons = Counter(str(row["reason"]) for row in rows if not row["accepted"])
        candidates = Counter(
            str(row["best_hero_id"])
            for row in rows
            if row["best_hero_id"] is not None
        )
        variants = Counter(
            str(row["best_variant"])
            for row in rows
            if row["best_variant"] is not None
        )
        slot_summary[key] = {
            "samples": len(rows),
            "accepted": accepted_count,
            "success_rate": _rate(accepted_count, len(rows)),
            "mean_best_score": _mean([float(row["best_score"]) for row in rows]),
            "mean_margin": _mean([float(row["margin"]) for row in rows]),
            "failure_reasons": dict(sorted(reasons.items())),
            "top_candidates": dict(candidates.most_common(5)),
            "top_variants": dict(variants.most_common(5)),
        }

    period_summary: dict[str, object] = {}
    period_names = ("capture_early", "capture_middle", "capture_late")
    for index, name in enumerate(period_names):
        start = len(frame_rows) * index // 3
        end = len(frame_rows) * (index + 1) // 3
        rows = frame_rows[start:end]
        accepted = sum(int(row["recognized_slots"]) for row in rows)
        period_summary[name] = {
            "frames": len(rows),
            "slot_success_rate": _rate(accepted, len(rows) * 10),
            "complete_frames": sum(bool(row["complete"]) for row in rows),
        }

    failures = sorted(
        (
            {
                "slot": key,
                "failures": int(summary["samples"]) - int(summary["accepted"]),
            }
            for key, summary in slot_summary.items()
        ),
        key=lambda item: (-item["failures"], item["slot"]),
    )
    first_tracker_confirmation = next(
        (row for row in frame_rows if row["tracker_confirmed"]),
        None,
    )
    report: dict[str, object] = {
        "runtime": "stable" if stable else "legacy",
        "evaluation_mode": "runtime" if stable and runtime_gates else "perception",
        "layout_profile": layout_profile or "auto",
        "evidence_dir": str(evidence_dir.resolve()),
        "feature_path": str(feature_path.resolve()),
        "thresholds": {"minimum_score": 0.62, "minimum_margin": 0.025},
        "total_files": len(samples),
        "layout_counts": dict(sorted(layout_counts.items())),
        "scene_counts": dict(sorted(scene_counts.items())),
        "replay_gate_counts": dict(sorted(replay_gate_counts.items())),
        "tracking_blocker_counts": dict(sorted(tracking_blocker_counts.items())),
        "evaluated_frames": len(frame_rows),
        "trackable_frames": sum(bool(row["trackable"]) for row in frame_rows),
        "target_identity_confirmed_frames": sum(
            bool(row["target_identity_confirmed"]) for row in frame_rows
        ),
        "complete_frames": sum(bool(row["complete"]) for row in frame_rows),
        "tracker_confirmed_frames": sum(
            bool(row["tracker_confirmed"]) for row in frame_rows
        ),
        "first_tracker_confirmation": first_tracker_confirmation,
        "layout_switch_count": len(layout_switches),
        "layout_switches": layout_switches,
        "mean_recognized_slots": _mean(
            [float(row["recognized_slots"]) for row in frame_rows]
        ),
        "slots": slot_summary,
        "most_failed_slots": failures,
        "capture_periods": period_summary,
        "variant_usage": _render_variant_usage(
            variant_usage,
            variant_counts,
            include_truth=truth_hero_ids is not None,
        ),
        "portrait_state_proxy": {
            state: {
                "samples": len(values),
                "success_rate": _rate(sum(values), len(values)),
            }
            for state, values in sorted(state_rows.items())
        },
        "notes": [
            "capture periods are chronological thirds, not OCR-derived game-clock bins",
            "low_saturation_proxy is a diagnostic proxy, not a death-state label",
            "this report does not change recognition thresholds",
        ],
    }
    if truth_hero_ids is not None:
        final_slots = tracker.slot_statuses
        accepted_total = sum(accepted_confusion.values())
        accepted_correct = sum(
            count
            for (truth_id, candidate_id), count in accepted_confusion.items()
            if truth_id == candidate_id
        )
        best_total = sum(best_confusion.values())
        best_correct = sum(
            count
            for (truth_id, candidate_id), count in best_confusion.items()
            if truth_id == candidate_id
        )

        def rendered_confusion(
            counts: Counter[tuple[int, int]],
        ) -> list[dict[str, int]]:
            return [
                {
                    "truth_hero_id": truth_id,
                    "candidate_hero_id": candidate_id,
                    "count": count,
                }
                for (truth_id, candidate_id), count in sorted(counts.items())
            ]

        report["truth_evaluation"] = {
            "context": truth_context or {},
            "truth_hero_ids": list(truth_hero_ids),
            "best_candidate_accuracy": _rate(best_correct, best_total),
            "accepted_precision": _rate(accepted_correct, accepted_total),
            "wrong_lock_count": len(wrong_locks),
            "wrong_locks": wrong_locks,
            "final_locked_slots": sum(item.state == "locked" for item in final_slots),
            "final_correct_locked_slots": sum(
                item.state == "locked" and item.hero_id == truth_hero_ids[index]
                for index, item in enumerate(final_slots)
            ),
            "final_locked_crop_cluster_counts": [
                {
                    "side": item.side,
                    "slot": item.slot,
                    "hero_id": item.hero_id,
                    "unique_crop_cluster_count": item.unique_crop_cluster_count,
                }
                for item in final_slots
                if item.state == "locked"
            ],
            "draft_ready": tracker.current_draft is not None,
            "lock_frame_index": tracker_confirmation_index,
            "lock_trackable_frame_index": tracker_confirmation_trackable_index,
            "lock_latency_seconds": (
                round(tracker_confirmation_at - first_trackable_at, 6)
                if tracker_confirmation_at is not None
                and first_trackable_at is not None
                else None
            ),
            "post_lock_frames": post_lock_frames,
            "exact_post_lock_frames": exact_post_lock_frames,
            "exact_post_lock_rate": _rate(exact_post_lock_frames, post_lock_frames),
            "best_candidate_confusion": rendered_confusion(best_confusion),
            "accepted_confusion": rendered_confusion(accepted_confusion),
            "tracker_lock_confusion": rendered_confusion(tracker_confusion),
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=ROOT / "data" / "live_betting" / "live_evidence",
    )
    parser.add_argument("--features", type=Path, default=DEFAULT_FEATURE_PATH)
    parser.add_argument("--stable", action="store_true")
    parser.add_argument("--perception-only", action="store_true")
    parser.add_argument("--layout-profile", choices=tuple(sorted(_LAYOUTS)))
    parser.add_argument("--observation-jsonl", type=Path)
    parser.add_argument("--truth-hero-ids", type=int, nargs=10)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--database-url")
    parser.add_argument("--raybet-match-id")
    parser.add_argument("--map-number", type=int)
    parser.add_argument("--opendota-match-id", type=int)
    parser.add_argument(
        "--mapping-source",
        choices=("manual_exact", "persisted_exact"),
    )
    parser.add_argument("--team-one-id", type=int)
    parser.add_argument("--team-two-id", type=int)
    parser.add_argument("--captured-after", type=datetime.fromisoformat)
    parser.add_argument("--captured-before", type=datetime.fromisoformat)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=ROOT / "data" / "live_betting" / "vision_evidence",
    )
    args = parser.parse_args()
    if args.perception_only and not args.stable:
        parser.error("--perception-only requires --stable")
    database_mode = any(
        value is not None
        for value in (
            args.raybet_match_id,
            args.map_number,
            args.opendota_match_id,
            args.mapping_source,
        )
    )
    if args.observation_jsonl is not None and database_mode:
        parser.error("--observation-jsonl cannot be combined with database mapping mode")
    if args.observation_jsonl is not None and args.truth_hero_ids is None:
        parser.error("--observation-jsonl requires --truth-hero-ids")
    if args.observation_jsonl is None and args.truth_hero_ids is not None:
        parser.error("--truth-hero-ids requires --observation-jsonl")
    if args.observation_jsonl is not None:
        samples, context = _observation_samples(
            args.observation_jsonl,
            captured_after=(
                args.captured_after.timestamp()
                if args.captured_after is not None
                else None
            ),
            captured_before=(
                args.captured_before.timestamp()
                if args.captured_before is not None
                else None
            ),
        )
        report = evaluate(
            args.evidence_dir,
            args.features,
            samples=samples,
            truth_hero_ids=tuple(args.truth_hero_ids),
            truth_context=context,
            stable=args.stable,
            layout_profile=args.layout_profile,
            runtime_gates=not args.perception_only,
        )
    elif database_mode:
        if (
            args.raybet_match_id is None
            or args.map_number is None
            or args.opendota_match_id is None
            or args.mapping_source is None
        ):
            parser.error(
                "--raybet-match-id, --map-number, --opendota-match-id, and "
                "--mapping-source are required together"
            )
        if args.mapping_source == "manual_exact" and (
            args.team_one_id is None or args.team_two_id is None
        ):
            parser.error("manual_exact requires --team-one-id and --team-two-id")
        load_environment_file(ROOT / ".env")
        samples, truth, mapping = _database_samples(
            args.database_url,
            raybet_match_id=args.raybet_match_id,
            map_number=args.map_number,
            opendota_match_id=args.opendota_match_id,
            evidence_root=args.evidence_root,
            mapping_source=args.mapping_source,
            manual_team_one_id=args.team_one_id,
            manual_team_two_id=args.team_two_id,
            captured_after=(
                args.captured_after.timestamp()
                if args.captured_after is not None
                else None
            ),
            captured_before=(
                args.captured_before.timestamp()
                if args.captured_before is not None
                else None
            ),
        )
        report = evaluate(
            args.evidence_root,
            args.features,
            samples=samples,
            truth_hero_ids=truth,
            truth_context={
                **mapping.report_context(len(truth)),
                "captured_after": (
                    args.captured_after.isoformat()
                    if args.captured_after is not None
                    else None
                ),
                "captured_before": (
                    args.captured_before.isoformat()
                    if args.captured_before is not None
                    else None
                ),
            },
            stable=args.stable,
            layout_profile=args.layout_profile,
            runtime_gates=not args.perception_only,
        )
    else:
        report = evaluate(
            args.evidence_dir,
            args.features,
            stable=args.stable,
            layout_profile=args.layout_profile,
            runtime_gates=not args.perception_only,
        )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
