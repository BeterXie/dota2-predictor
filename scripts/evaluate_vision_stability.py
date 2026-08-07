"""Evaluate the stable HUD reader against a real-frame JSONL manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vision.stable_runtime import StableHudReader  # noqa: E402


def _load_manifest(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict) or not isinstance(payload.get("file"), str):
            raise ValueError(f"invalid manifest row {line_number}")
        rows.append(payload)
    return rows


def _hero_tuple(value: object) -> tuple[int, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("hero expectations must be arrays")
    return tuple(int(item) for item in value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--features", type=Path)
    args = parser.parse_args()

    rows = _load_manifest(args.manifest)
    reader = StableHudReader(args.features) if args.features else StableHudReader()

    total = 0
    layout_correct = 0
    scene_correct = 0
    hero_slot_correct = 0
    hero_slot_expected = 0
    exact_lineups = 0
    exact_lineup_expected = 0
    blockers: dict[str, int] = {}

    for row in rows:
        image_path = args.manifest.parent / str(row["file"])
        image = cv2.imread(str(image_path))
        if image is None:
            raise ValueError(f"cannot read frame: {image_path}")
        reading = reader.read(image)
        total += 1
        diagnostics = reading.diagnostics
        blockers[diagnostics.blocker_code] = blockers.get(diagnostics.blocker_code, 0) + 1

        expected_layout = row.get("layout")
        if expected_layout is not None and reading.selection.layout_name == expected_layout:
            layout_correct += 1
        expected_scene = row.get("scene")
        if expected_scene is not None and reading.screen_state == expected_scene:
            scene_correct += 1

        expected_radiant = _hero_tuple(row.get("radiant_heroes"))
        expected_dire = _hero_tuple(row.get("dire_heroes"))
        if expected_radiant is not None and expected_dire is not None:
            expected = expected_radiant + expected_dire
            actual = reading.draft.radiant_hero_ids + reading.draft.dire_hero_ids
            exact_lineup_expected += 1
            if actual == expected:
                exact_lineups += 1
            if len(reading.draft.slot_diagnostics) == 10:
                for wanted, diagnostic in zip(expected, reading.draft.slot_diagnostics, strict=True):
                    hero_slot_expected += 1
                    if diagnostic.accepted and diagnostic.best_hero_id == wanted:
                        hero_slot_correct += 1

    result = {
        "frames": total,
        "layout_accuracy": layout_correct / total if total else 0.0,
        "scene_accuracy": scene_correct / total if total else 0.0,
        "hero_slot_accuracy": (
            hero_slot_correct / hero_slot_expected if hero_slot_expected else None
        ),
        "exact_lineup_accuracy": (
            exact_lineups / exact_lineup_expected if exact_lineup_expected else None
        ),
        "blockers": dict(sorted(blockers.items())),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
