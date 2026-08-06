"""Freeze the one-feature prospective R.O.S.H. shadow candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_intelligence.legacy_rosh_reconstruction import (  # noqa: E402
    LEGACY_ROSH_FORMULA_VERSION,
)
from event_intelligence.prospective_rosh_candidate import (  # noqa: E402
    ProspectiveRoshCandidate,
    freeze_prospective_rosh_candidate,
)
from event_intelligence.raw_archive import canonical_json_bytes  # noqa: E402
from event_intelligence.rosh_retrospective_utility import (  # noqa: E402
    CanonicalSelection,
    CohortLoadResult,
    RetrospectiveRow,
)


EXPECTED_OOF_HASH = "428883895cefe6c73ac219119dbe928762497d9b9c8944d6531df127654b9896"
EXPECTED_SUPPORT = 513


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must include a timezone")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retrospective-analysis",
        type=Path,
        required=True,
        help="Frozen retrospective utility JSON containing the 513-row OOF manifest",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frozen-at", type=_aware_datetime, required=True)
    parser.add_argument("--prospective-start-at", type=_aware_datetime, required=True)
    return parser


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ValueError(f"{field} must be an object")
    return value


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _series_identity(value: object, match_id: int) -> tuple[int | None, str]:
    if not isinstance(value, str):
        raise ValueError("series_key must be a string")
    if value == f"match:{match_id}":
        return None, value
    prefix = "series:"
    if not value.startswith(prefix) or not value[len(prefix) :].isdigit():
        raise ValueError("series_key must use the frozen series identity")
    series_id = int(value[len(prefix) :])
    if series_id <= 0:
        raise ValueError("series_id must be positive")
    return series_id, value


def _row(value: object) -> RetrospectiveRow:
    row = _mapping(value, "OOF row")
    match_id = _integer(row.get("match_id"), "match_id", minimum=1)
    series_id, series_key = _series_identity(row.get("series_key"), match_id)
    outcome = _integer(row.get("outcome"), "outcome")
    if outcome not in (0, 1):
        raise ValueError("outcome must be binary")
    cutoff_raw = row.get("prediction_cutoff")
    if not isinstance(cutoff_raw, str):
        raise ValueError("prediction_cutoff must be an ISO timestamp")
    cutoff = _aware_datetime(cutoff_raw)
    patch = row.get("patch")
    if patch is not None:
        patch = _integer(patch, "patch", minimum=1)
    score_key = row.get("score_key")
    if (
        not isinstance(score_key, str)
        or len(score_key) != 64
        or any(character not in "0123456789abcdef" for character in score_key)
    ):
        raise ValueError("score_key must be a lowercase SHA-256 digest")
    formula = row.get("formula_version")
    if formula != LEGACY_ROSH_FORMULA_VERSION:
        raise ValueError("OOF row does not use the frozen legacy pure scorer")
    return RetrospectiveRow(
        match_id=match_id,
        score_key=score_key,
        formula_version=formula,
        prediction_cutoff=cutoff,
        pure_lineup_score=float(row["pure_lineup_score"]),
        radiant_win=outcome,
        series_id=series_id,
        series_key=series_key,
        event_id=str(row["event_id"]),
        patch=patch,
        month=str(row["month"]),
        team_probability=float(row["m0_team_probability"]),
    )


def cohort_from_analysis(payload: object) -> CohortLoadResult:
    analysis = _mapping(payload, "analysis")
    incremental = _mapping(analysis.get("incremental"), "incremental")
    cohort = _mapping(analysis.get("cohort"), "cohort")
    if (
        analysis.get("analysis_mode") != "retrospective_exploratory"
        or analysis.get("formula_version") != LEGACY_ROSH_FORMULA_VERSION
        or analysis.get("leakage_free_oos") is not False
        or analysis.get("deployment_evidence") is not False
        or analysis.get("rosh_input_fields") != ["pure_lineup_score"]
        or analysis.get("baseline_input")
        != "team_rating_predictions.raw_probability"
        or analysis.get("forbidden_fields_used") != []
    ):
        raise ValueError("retrospective analysis contract does not match")
    support = _integer(incremental.get("support"), "support")
    rows_raw = incremental.get("oof_predictions")
    if not isinstance(rows_raw, list) or support != EXPECTED_SUPPORT:
        raise ValueError("candidate requires the frozen 513-row OOF manifest")
    observed_hash = hashlib.sha256(canonical_json_bytes(rows_raw)).hexdigest()
    if (
        incremental.get("oof_predictions_hash") != observed_hash
        or observed_hash != EXPECTED_OOF_HASH
    ):
        raise ValueError("retrospective OOF manifest hash mismatch")
    rows = tuple(_row(value) for value in rows_raw)
    if len(rows) != support or len({row.match_id for row in rows}) != support:
        raise ValueError("retrospective OOF manifest identity mismatch")

    selection_raw = _mapping(
        cohort.get("canonical_selection"), "canonical_selection"
    )
    selection = CanonicalSelection(
        rows_before=_integer(selection_raw.get("rows_before"), "rows_before"),
        duplicate_groups=_integer(
            selection_raw.get("duplicate_groups"), "duplicate_groups"
        ),
        duplicate_rows=_integer(
            selection_raw.get("duplicate_rows"), "duplicate_rows"
        ),
        conflicting_score_groups=_integer(
            selection_raw.get("conflicting_score_groups"),
            "conflicting_score_groups",
        ),
        rows_after=_integer(selection_raw.get("rows_after"), "rows_after"),
        removed_rows=_integer(selection_raw.get("removed_rows"), "removed_rows"),
        rule=str(selection_raw.get("rule")),
    )
    formula_versions = cohort.get("formula_versions")
    if formula_versions != [
        {"support": 561, "value": LEGACY_ROSH_FORMULA_VERSION}
    ]:
        raise ValueError("retrospective formula support does not match")
    if cohort.get("source_unchanged") is not True:
        raise ValueError("retrospective source database was not read-only")
    ordered = tuple(sorted(rows, key=lambda row: (row.prediction_cutoff, row.match_id)))
    return CohortLoadResult(
        candidates=ordered,
        paired=ordered,
        canonical_selection=selection,
        evidence_hash_valid=_integer(
            cohort.get("evidence_hash_valid"), "evidence_hash_valid"
        ),
        formal_valid_results=_integer(
            cohort.get("formal_valid_results"), "formal_valid_results"
        ),
        missing_team_rating=_integer(
            cohort.get("missing_team_rating"), "missing_team_rating"
        ),
        formula_versions=((LEGACY_ROSH_FORMULA_VERSION, 561),),
        source_unchanged=True,
    )


def freeze_from_analysis(
    analysis_path: Path,
    *,
    frozen_at: datetime,
    prospective_start_at: datetime,
) -> ProspectiveRoshCandidate:
    try:
        payload = json.loads(analysis_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("retrospective analysis JSON is unavailable or invalid") from error
    return freeze_prospective_rosh_candidate(
        cohort_from_analysis(payload),
        frozen_at=frozen_at,
        prospective_start_at=prospective_start_at,
    )


def _persist_immutable(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError("candidate output exists with different content")
        return
    path.write_bytes(body)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    candidate = freeze_from_analysis(
        args.retrospective_analysis,
        frozen_at=args.frozen_at,
        prospective_start_at=args.prospective_start_at,
    )
    body = candidate.canonical_bytes() + b"\n"
    _persist_immutable(args.output, body)
    print(candidate.artifact_hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
