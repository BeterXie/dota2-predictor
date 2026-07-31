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


def _directory_samples(evidence_dir: Path) -> list[EvidenceSample]:
    paths = sorted(
        path
        for pattern in ("*.jpg", "*.jpeg", "*.png")
        for path in evidence_dir.glob(pattern)
    )
    return [EvidenceSample(path, path.stat().st_mtime) for path in paths]


def _database_samples(
    database_url: str | None,
    *,
    raybet_match_id: str,
    map_number: int,
    opendota_match_id: int,
    evidence_root: Path,
    captured_after: float | None = None,
    captured_before: float | None = None,
) -> tuple[list[EvidenceSample], tuple[int, ...]]:
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            observations = connection.execute(
                text(
                    """
                    SELECT captured_at, source_frame_sha256
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
    for row in observations:
        observed_at = datetime.fromisoformat(str(row["captured_at"])).timestamp()
        if captured_after is not None and observed_at < captured_after:
            continue
        if captured_before is not None and observed_at >= captured_before:
            continue
        digest = str(row["source_frame_sha256"])
        samples.append(
            EvidenceSample(
                content_root / digest[:2] / f"{digest}.jpg",
                observed_at,
            )
        )
    return samples, truth


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _mean(values: list[float]) -> float:
    return round(fmean(values), 6) if values else 0.0


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
        )
        accepted = 0
        regions = selection.layout.radiant_heroes + selection.layout.dire_heroes
        for index, diagnostic in enumerate(reading.slot_diagnostics):
            key = f"{diagnostic.side}_{diagnostic.slot}"
            row = {
                "accepted": diagnostic.accepted,
                "best_hero_id": diagnostic.best_hero_id,
                "best_score": diagnostic.best_score,
                "margin": diagnostic.margin,
                "reason": diagnostic.reason,
            }
            slot_rows[key].append(row)
            accepted += int(diagnostic.accepted)
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
        slot_summary[key] = {
            "samples": len(rows),
            "accepted": accepted_count,
            "success_rate": _rate(accepted_count, len(rows)),
            "mean_best_score": _mean([float(row["best_score"]) for row in rows]),
            "mean_margin": _mean([float(row["margin"]) for row in rows]),
            "failure_reasons": dict(sorted(reasons.items())),
            "top_candidates": dict(candidates.most_common(5)),
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
        )
    )
    if database_mode:
        if (
            args.raybet_match_id is None
            or args.map_number is None
            or args.opendota_match_id is None
        ):
            parser.error(
                "--raybet-match-id, --map-number, and --opendota-match-id "
                "are required together"
            )
        load_environment_file(ROOT / ".env")
        samples, truth = _database_samples(
            args.database_url,
            raybet_match_id=args.raybet_match_id,
            map_number=args.map_number,
            opendota_match_id=args.opendota_match_id,
            evidence_root=args.evidence_root,
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
                "raybet_match_id": args.raybet_match_id,
                "map_number": args.map_number,
                "opendota_match_id": args.opendota_match_id,
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
