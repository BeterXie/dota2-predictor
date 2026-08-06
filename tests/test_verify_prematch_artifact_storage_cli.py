from __future__ import annotations

import pytest

from scripts.verify_prematch_artifact_storage import (
    _require_isolated_database,
    build_parser,
)


def test_artifact_storage_cli_bounds_acceptance_cohort() -> None:
    args = build_parser().parse_args(("--artifact-root", "artifacts"))

    assert args.max_maps == 100
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ("--artifact-root", "artifacts", "--max-maps", "99")
        )


def test_artifact_storage_refuses_nonisolated_database() -> None:
    with pytest.raises(ValueError, match="non-isolated"):
        _require_isolated_database(
            "postgresql+psycopg://user:password@localhost/dota2_predictor"
        )

    assert _require_isolated_database(
        "postgresql+psycopg://user:password@localhost/dota2_artifact_acceptance"
    ).endswith("/dota2_artifact_acceptance")
