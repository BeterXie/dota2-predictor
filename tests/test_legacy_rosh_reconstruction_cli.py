from __future__ import annotations

from pathlib import Path

from scripts.audit_legacy_rosh_reconstruction import build_parser


ROOT = Path(__file__).resolve().parents[1]


def test_cli_is_read_only_and_has_no_persist_option() -> None:
    parser = build_parser()
    args = parser.parse_args(())

    assert not hasattr(args, "persist")
    source = (
        ROOT / "scripts" / "audit_legacy_rosh_reconstruction.py"
    ).read_text(encoding="utf-8")
    assert "SET TRANSACTION READ ONLY" in source
