from __future__ import annotations

from scripts.verify_rosh_authority_bridge import build_parser


def test_rosh_authority_bridge_verifier_uses_database_url() -> None:
    args = build_parser().parse_args(("--database-url", "postgresql+psycopg://x"))

    assert args.database_url == "postgresql+psycopg://x"
