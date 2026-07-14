"""Build the compact hero portrait features used by live HUD recognition."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vision.image_features import color_histogram, compute_phash  # noqa: E402


DEFAULT_SOURCE = ROOT / "vision" / "templates" / "heroes"
DEFAULT_OUTPUT = ROOT / "vision" / "templates" / "hero_features.npz"


def build_hero_features(source: Path, output: Path) -> int:
    metadata_path = source / "heroes.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        expected_ids = {int(value) for value in metadata}
    except (OSError, TypeError, ValueError) as error:
        raise ValueError(f"invalid hero metadata: {metadata_path}") from error
    if not expected_ids:
        raise ValueError(f"hero metadata is empty: {metadata_path}")

    source_paths: dict[int, Path] = {}
    for path in source.glob("*.png"):
        try:
            hero_id = int(path.stem)
        except ValueError as error:
            raise ValueError(f"invalid hero portrait filename: {path.name}") from error
        source_paths[hero_id] = path
    if set(source_paths) != expected_ids:
        missing = sorted(expected_ids - set(source_paths))
        unexpected = sorted(set(source_paths) - expected_ids)
        raise ValueError(
            f"hero portrait set does not match metadata; missing={missing} "
            f"unexpected={unexpected}"
        )

    ids: list[int] = []
    hashes: list[np.ndarray] = []
    histograms: list[np.ndarray] = []
    thumbnails: list[np.ndarray] = []
    for hero_id in sorted(expected_ids):
        path = source_paths[hero_id]
        image = cv2.imread(str(path))
        if image is None:
            raise ValueError(f"invalid hero portrait: {path}")
        ids.append(hero_id)
        hashes.append(compute_phash(image, hash_size=8))
        histograms.append(color_histogram(image))
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        thumbnails.append(cv2.resize(gray, (48, 32), interpolation=cv2.INTER_AREA))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".part")
    with temporary.open("wb") as handle:
        np.savez_compressed(
            handle,
            ids=np.asarray(ids, dtype=np.int32),
            hashes=np.asarray(hashes, dtype=np.uint8),
            histograms=np.asarray(histograms, dtype=np.float32),
            thumbnails=np.asarray(thumbnails, dtype=np.uint8),
        )
    temporary.replace(output)
    return len(ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(f"built features for {build_hero_features(args.source, args.output)} heroes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
