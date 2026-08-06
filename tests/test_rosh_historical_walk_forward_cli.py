from __future__ import annotations

from scripts.audit_rosh_historical_walk_forward import build_parser


def test_phase_one_cli_requires_artifact_root_and_has_no_pilot_flag() -> None:
    args = build_parser().parse_args(("--artifact-root", "artifacts"))

    assert str(args.artifact_root) == "artifacts"
    assert not hasattr(args, "pilot")
    assert not hasattr(args, "persist")
