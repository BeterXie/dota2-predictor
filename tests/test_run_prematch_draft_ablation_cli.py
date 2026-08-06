from __future__ import annotations

from scripts.run_prematch_draft_ablation import build_parser


def test_draft_ablation_cli_defaults_to_fixed_300_map_cohort() -> None:
    args = build_parser().parse_args(("--artifact-root", "artifacts"))

    assert args.max_maps == 300
    assert args.bootstrap_samples == 1_000
