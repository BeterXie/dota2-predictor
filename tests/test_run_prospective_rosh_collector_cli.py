from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from event_intelligence.prospective_rosh_collector import CollectionReport
from scripts import run_prospective_rosh_collector as cli
from scripts import run_prospective_rosh_collector_worker as worker


UTC = timezone.utc


def _report(*, stopped: bool = False) -> CollectionReport:
    return CollectionReport(
        scanned=0,
        paired=0,
        p0_only=0,
        retry_scheduled=0,
        terminal_failure=0,
        unchanged=0,
        settlements_stored=0,
        causal_audits_stored=0,
        acceptance_limit=5,
        acceptance_collected=5 if stopped else 0,
        acceptance_stopped=stopped,
        results=(),
        acceptance=(),
    )


class Storage:
    connection = object()

    def __init__(self, _url: str) -> None:
        self.initialized = False

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def init_schema(self, *, seed_events: bool) -> None:
        assert seed_events is False
        self.initialized = True


def test_parser_hard_limits_real_acceptance_to_five_through_ten() -> None:
    assert cli.build_parser().parse_args(["--acceptance-limit", "5"]).acceptance_limit == 5
    assert cli.build_parser().parse_args(["--acceptance-limit", "10"]).acceptance_limit == 10
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--acceptance-limit", "4"])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--acceptance-limit", "11"])


def test_one_shot_dry_run_never_constructs_stratz_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "require_database_url", lambda value: value or "postgresql://db")
    monkeypatch.setattr(cli, "IntelligenceStorage", Storage)
    monkeypatch.setattr(cli, "ProspectiveRoshCollectorRepository", lambda connection: connection)
    monkeypatch.setattr(
        cli,
        "StratzRoshClient",
        lambda: (_ for _ in ()).throw(AssertionError("network client constructed")),
    )

    def run(_repository: object, transport: object, **kwargs: object) -> CollectionReport:
        captured["transport"] = transport
        captured.update(kwargs)
        return _report()

    monkeypatch.setattr(cli, "run_collector_once", run)
    assert cli.main(
        [
            "--database-url",
            "postgresql://db",
            "--dry-run",
            "--acceptance-limit",
            "5",
            "--artifact-root",
            str(tmp_path),
        ]
    ) == 0
    assert captured["dry_run"] is True
    assert captured["acceptance_limit"] == 5


def test_worker_stops_only_after_bounded_acceptance_is_settled_and_audited(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0
    monkeypatch.setattr(worker, "require_database_url", lambda value: value)
    monkeypatch.setattr(worker, "IntelligenceStorage", Storage)
    monkeypatch.setattr(
        worker,
        "ProspectiveRoshCollectorRepository",
        lambda connection: connection,
    )

    def run(*_args: object, **_kwargs: object) -> CollectionReport:
        nonlocal calls
        calls += 1
        return _report(stopped=True)

    monkeypatch.setattr(worker, "run_collector_once", run)
    assert worker.run_worker(
        "postgresql://db",
        batch_size=1,
        poll_seconds=0.01,
        acceptance_limit=5,
        artifact_root=tmp_path,
        clock=lambda: datetime(2026, 8, 8, tzinfo=UTC),
        transport=SimpleNamespace(),
    ) == 0
    assert calls == 1
