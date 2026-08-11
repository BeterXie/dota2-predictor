"""Integrity-checked, explicitly promoted hero features per broadcast profile."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


DEFAULT_CALIBRATION_ROOT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "live_betting"
    / "vision_calibration"
)


def promoted_profile_feature_path(
    profile_id: str,
    *,
    calibration_root: Path = DEFAULT_CALIBRATION_ROOT,
) -> Path | None:
    if re.fullmatch(r"[a-z0-9_]{1,100}", profile_id) is None:
        return None
    root = calibration_root.resolve() / "promoted"
    feature_path = root / f"{profile_id}.npz"
    manifest_path = root / f"{profile_id}.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(manifest, dict)
        or manifest.get("profile_id") != profile_id
        or manifest.get("source_identity_verified") is not True
        or not feature_path.is_file()
    ):
        return None
    expected = str(manifest.get("feature_sha256") or "")
    if len(expected) != 64 or _sha256(feature_path) != expected:
        return None
    return feature_path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["DEFAULT_CALIBRATION_ROOT", "promoted_profile_feature_path"]
