from __future__ import annotations

from scripts.report_rosh_support_funnel import build_parser


def test_rosh_support_funnel_requires_artifact_root() -> None:
    args = build_parser().parse_args(("--artifact-root", "artifacts"))

    assert str(args.artifact_root) == "artifacts"
