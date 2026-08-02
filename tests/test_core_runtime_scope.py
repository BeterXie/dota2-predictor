from __future__ import annotations

from argparse import Namespace

import pytest

from scripts.run_dota_shadow_service import _commands, _parser
from web.control import COMPONENTS


def _arguments(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "once": False,
        "start_collector": False,
        "start_mail": False,
        "start_strict_ingest": False,
        "disable_historical_rosh": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_historical_rosh_remains_the_default_supervised_worker() -> None:
    assert set(_commands(_arguments())) == {"historical_rosh"}
    assert set(_commands(_arguments(start_collector=True))) == {
        "collector",
        "historical_rosh",
    }


def test_strict_opendota_ingest_can_join_the_core_runtime() -> None:
    commands = _commands(
        _arguments(start_collector=True, start_strict_ingest=True)
    )

    assert set(commands) == {"collector", "strict_ingest", "historical_rosh"}
    assert commands["strict_ingest"][1:3] == [
        "scripts/run_strict_event_ingest.py",
        "--archive-root",
    ]
    assert commands["strict_ingest"][-1] == "--schema-prepared"


def test_retired_paper_and_browser_flags_are_rejected() -> None:
    parser = _parser()

    for flag in (
        "--start-companion",
        "--start-vision",
        "--start-shadow",
        "--start-postmatch",
        "--start-draft-publisher",
    ):
        with pytest.raises(SystemExit):
            parser.parse_args([flag])


def test_web_control_exposes_only_core_runtime_workers() -> None:
    assert tuple(COMPONENTS) == ("raybet_collector", "mail_worker")
