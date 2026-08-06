from __future__ import annotations

from scripts.run_rosh_authority_bridge import build_parser


def test_rosh_authority_bridge_defaults_to_read_only() -> None:
    args = build_parser().parse_args(("--artifact-root", "artifacts"))

    assert str(args.artifact_root) == "artifacts"
    assert args.persist is False


def test_rosh_authority_bridge_persist_is_explicit() -> None:
    args = build_parser().parse_args(("--artifact-root", "artifacts", "--persist"))

    assert args.persist is True
