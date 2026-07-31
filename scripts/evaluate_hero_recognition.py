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
    DraftTracker,
    HeroRecognizer,
)
from vision.layout_selector import select_broadcast_layout  # noqa: E402
from database.engine import build_engine  # noqa: E402
from shared.environment import load_environment_file  # noqa: E402


@dataclass(frozen=True)
class EvidenceSample:
    path: Path
    observed_at: float
    source_frame_hash: str | None = None
    game_clock_seconds: int | None = None


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
                    SELECT captured_at, game_clock_seconds, source_frame_sha256
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
) -> dict[str, object]:
    samples = samples if samples is not None else _directory_samples(evidence_dir)
    if truth_hero_ids is not None and len(truth_hero_ids) != 10:
        raise ValueError("truth_hero_ids must contain exactly ten heroes")
    layout_counts: Counter[str] = Counter()
    readers: dict[str, HeroRecognizer] = {}
    tracker = DraftTracker()
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

    for sample in samples:
        path = sample.path
        image = cv2.imread(str(path))
        if image is None:
            layout_counts["unreadable"] += 1
            continue
        selection = select_broadcast_layout(image)
        layout_name = selection.layout_name or "unsupported"
        layout_counts[layout_name] += 1
        if active_layout_name != selection.layout_name:
            tracker.reset()
            previous_tracker_slots = tracker.slot_statuses
        active_layout_name = selection.layout_name
        if selection.layout is None:
            continue
        reader = readers.get(layout_name)
        if reader is None:
            reader = HeroRecognizer(feature_path, selection.layout)
            readers[layout_name] = reader
        reading = reader.read(image)
        confirmed = tracker.update(
            reading,
            observed_at=sample.observed_at,
            source_frame_hash=sample.source_frame_hash,
            game_clock_seconds=sample.game_clock_seconds,
        )
        accepted = 0
        regions = selection.layout.radiant_heroes + selection.layout.dire_heroes
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
                        }
                    )
        previous_tracker_slots = tracker_slots
        frame_rows.append(
            {
                "file": path.name,
                "layout": layout_name,
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
        "evidence_dir": str(evidence_dir.resolve()),
        "feature_path": str(feature_path.resolve()),
        "thresholds": {"minimum_score": 0.62, "minimum_margin": 0.025},
        "total_files": len(samples),
        "layout_counts": dict(sorted(layout_counts.items())),
        "evaluated_frames": len(frame_rows),
        "complete_frames": sum(bool(row["complete"]) for row in frame_rows),
        "tracker_confirmed_frames": sum(
            bool(row["tracker_confirmed"]) for row in frame_rows
        ),
        "first_tracker_confirmation": first_tracker_confirmation,
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
            "draft_ready": tracker.current_draft is not None,
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
    database_mode = any(
        value is not None
        for value in (
            args.raybet_match_id,
            args.map_number,
            args.opendota_match_id,
            args.mapping_source,
        )
    )
    if database_mode:
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
        )
    else:
        report = evaluate(args.evidence_dir, args.features)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
