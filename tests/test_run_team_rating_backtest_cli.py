from __future__ import annotations

import pytest

from event_intelligence.draft_features import AvailabilityMode
from scripts.run_team_rating_backtest import build_parser, main


def test_formal_team_rating_cli_defaults_to_reconstructed_mode() -> None:
    args = build_parser().parse_args([])

    assert args.availability_mode == AvailabilityMode.RECONSTRUCTED.value


def test_formal_team_rating_cli_rejects_prospective_before_database_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            (
                "--availability-mode",
                AvailabilityMode.PROSPECTIVE.value,
                "--database-url",
                "postgresql+psycopg://must-not-connect.invalid/database",
            )
        )

    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
