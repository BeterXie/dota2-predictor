from __future__ import annotations

from datetime import timezone

import pytest

from scripts.run_prospective_team_rating import build_parser
from scripts.run_prospective_team_rating_worker import build_parser as worker_parser


def test_one_shot_cli_accepts_operational_scope() -> None:
    args = build_parser().parse_args(
        [
            "--database-url",
            "postgresql+psycopg://user:pass@localhost/db",
            "--match-id",
            "123",
            "--scan-start",
            "2026-08-07T00:00:00Z",
            "--scan-end",
            "2026-08-08T00:00:00+00:00",
            "--dry-run",
        ]
    )

    assert args.match_id == 123
    assert args.scan_start.tzinfo == timezone.utc
    assert args.scan_end.tzinfo == timezone.utc
    assert args.dry_run


def test_one_shot_cli_rejects_non_utc_and_worker_is_bounded() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--scan-start", "2026-08-07T00:00:00"])

    args = worker_parser().parse_args(["--batch-size", "1", "--poll-seconds", "5"])
    assert args.batch_size == 1
    assert args.poll_seconds == 5.0
