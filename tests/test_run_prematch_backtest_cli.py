from __future__ import annotations

import pytest

import scripts.run_prematch_backtest as runner
from event_intelligence.draft_features import AvailabilityMode
from scripts.run_prematch_backtest import build_parser, main


def test_formal_prematch_cli_defaults_to_reconstructed_mode() -> None:
    args = build_parser().parse_args(("--artifact-root", "artifacts"))

    assert args.availability_mode == AvailabilityMode.RECONSTRUCTED.value
    assert not hasattr(args, "bootstrap_samples")


def test_bounded_acceptance_requires_dry_run() -> None:
    with pytest.raises(SystemExit) as error:
        main(("--artifact-root", "artifacts", "--max-maps", "300"))

    assert error.value.code == 2


def test_formal_prematch_cli_rejects_prospective_before_database_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            (
                "--artifact-root",
                "artifacts",
                "--availability-mode",
                AvailabilityMode.PROSPECTIVE.value,
                "--database-url",
                "postgresql+psycopg://must-not-connect.invalid/database",
            )
        )

    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_formal_prematch_cli_rejects_bootstrap_override() -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(
            ("--artifact-root", "artifacts", "--bootstrap-samples", "1000")
        )

    assert error.value.code == 2


def test_formal_runner_builds_and_serializes_report_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    result = object()
    report = object()

    class FakeStorage:
        def __init__(self, _database_url: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def init_schema(self) -> None:
            events.append("schema")

    monkeypatch.setattr(runner, "require_database_url", lambda value: str(value))
    monkeypatch.setattr(runner, "IntelligenceStorage", FakeStorage)
    monkeypatch.setattr(
        runner,
        "run_prematch_backtest",
        lambda *_args, **_kwargs: events.append("run") or result,
    )
    monkeypatch.setattr(
        runner,
        "build_prematch_report",
        lambda value: events.append("report") or report if value is result else None,
    )
    monkeypatch.setattr(
        runner,
        "report_as_dict",
        lambda value: events.append("serialize") or {"ok": value is report},
    )

    def persist(value, _storage, *, report, dry_run):
        assert value is result
        assert report is globals_report
        assert dry_run is True
        events.append("persist")

    globals_report = report
    monkeypatch.setattr(runner, "persist_prematch_backtest_result", persist)

    assert main(("--artifact-root", "artifacts", "--dry-run")) == 0
    assert events == ["schema", "run", "report", "serialize", "persist"]
