from __future__ import annotations

from scripts.analyze_rosh_retrospective_utility import build_parser


def test_retrospective_cli_has_only_read_and_report_controls() -> None:
    args = build_parser().parse_args(())

    assert args.bootstrap_samples == 2_000
    assert args.sanity_permutations == 200
    for forbidden in ("persist", "train", "deploy", "calibrate", "order"):
        assert not hasattr(args, forbidden)
