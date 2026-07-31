"""Evaluate per-slot hero recognition against saved Vision evidence frames."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean

import cv2


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vision.hero_recognizer import (  # noqa: E402
    DEFAULT_FEATURE_PATH,
    DraftTracker,
    HeroRecognizer,
)
from vision.layout_selector import select_broadcast_layout  # noqa: E402


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _mean(values: list[float]) -> float:
    return round(fmean(values), 6) if values else 0.0


def evaluate(
    evidence_dir: Path,
    feature_path: Path,
) -> dict[str, object]:
    paths = sorted(
        path
        for pattern in ("*.jpg", "*.jpeg", "*.png")
        for path in evidence_dir.glob(pattern)
    )
    layout_counts: Counter[str] = Counter()
    readers: dict[str, HeroRecognizer] = {}
    trackers: dict[str, DraftTracker] = {}
    slot_rows: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    frame_rows: list[dict[str, object]] = []
    state_rows: defaultdict[str, list[bool]] = defaultdict(list)

    for path in paths:
        image = cv2.imread(str(path))
        if image is None:
            layout_counts["unreadable"] += 1
            continue
        selection = select_broadcast_layout(image)
        layout_name = selection.layout_name or "unsupported"
        layout_counts[layout_name] += 1
        if selection.layout is None:
            continue
        reader = readers.get(layout_name)
        if reader is None:
            reader = HeroRecognizer(feature_path, selection.layout)
            readers[layout_name] = reader
            trackers[layout_name] = DraftTracker()
        reading = reader.read(image)
        confirmed = trackers[layout_name].update(reading)
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

            crop = regions[index].crop(image)
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            saturation = float(hsv[:, :, 1].mean())
            state = "low_saturation_proxy" if saturation < 80.0 else "normal_color"
            state_rows[state].append(diagnostic.accepted)
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
    return {
        "evidence_dir": str(evidence_dir.resolve()),
        "feature_path": str(feature_path.resolve()),
        "thresholds": {"minimum_score": 0.62, "minimum_margin": 0.025},
        "total_files": len(paths),
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=ROOT / "data" / "live_betting" / "live_evidence",
    )
    parser.add_argument("--features", type=Path, default=DEFAULT_FEATURE_PATH)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.evidence_dir, args.features)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
