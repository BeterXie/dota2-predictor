"""Entry point: python -m web.main"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Mapping, Sequence

import uvicorn
import yaml

from live_betting.database_protocol import verify_prepared_database
from live_betting.milestone_revocation import MilestoneRevocationConfig
from live_betting.service_coordination import (
    add_single_database_argument,
    service_data_paths,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(parser)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.yaml"),
    )
    return parser


def _load_config(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError(f"web config must be a mapping: {path}")
    return value


def resolve_database_path(
    cli_database: Path | None,
    config: Mapping[str, object],
    config_path: Path,
    environment: Mapping[str, str],
) -> tuple[Path, str]:
    """Resolve the one runtime database authority by documented precedence."""

    if cli_database is not None:
        return cli_database.resolve(), "cli"
    configured_environment = environment.get("DATABASE_PATH")
    if configured_environment:
        return Path(configured_environment).resolve(), "environment"
    configured_file = config.get("database")
    if configured_file:
        configured_path = Path(str(configured_file))
        if not configured_path.is_absolute():
            configured_path = config_path.resolve().parent / configured_path
        return configured_path.resolve(), "config"
    return (Path(__file__).resolve().parents[1] / "data" / "dota2.db"), "default"


def _configured_path(value: object, config_path: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"milestone revocation {label} path is required")
    path = Path(value)
    if not path.is_absolute():
        path = config_path.resolve().parent / path
    return path.resolve()


def resolve_milestone_revocation_config(
    config: Mapping[str, object],
    config_path: Path,
    runtime_database: Path,
) -> MilestoneRevocationConfig | None:
    value = config.get("milestone_revocation")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "ledger",
        "database",
        "raw_root",
        "anchor",
        "pair_baseline_manifest",
    }:
        raise ValueError("milestone revocation configuration is incomplete")
    anchor = value["anchor"]
    pair = value["pair_baseline_manifest"]
    if (
        not isinstance(anchor, dict)
        or set(anchor) != {"path", "sha256"}
        or not isinstance(pair, dict)
        or set(pair) != {"path", "sha256"}
    ):
        raise ValueError("milestone revocation external evidence configuration is incomplete")
    configured_database = _configured_path(value["database"], config_path, "database")
    if configured_database != runtime_database.resolve():
        raise ValueError("milestone revocation database differs from runtime database")
    for label, digest in (
        ("anchor", anchor["sha256"]),
        ("pair baseline manifest", pair["sha256"]),
    ):
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"milestone revocation {label} SHA-256 is invalid")
    return MilestoneRevocationConfig(
        root=_configured_path(value["ledger"], config_path, "ledger"),
        database_path=configured_database,
        raw_root=_configured_path(value["raw_root"], config_path, "raw root"),
        expected_anchor=_configured_path(anchor["path"], config_path, "anchor"),
        expected_anchor_hash=str(anchor["sha256"]),
        pair_manifest=_configured_path(pair["path"], config_path, "pair baseline manifest"),
        expected_pair_manifest_hash=str(pair["sha256"]),
    )


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = _parser().parse_args(argv)
    config_path = args.config.resolve()
    config = _load_config(config_path)

    server_cfg = config.get("server", {})
    if not isinstance(server_cfg, dict):
        raise ValueError("web server config must be a mapping")
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8000)
    reload = server_cfg.get("reload", False)

    database, source = resolve_database_path(
        args.database,
        config,
        config_path,
        os.environ,
    )
    paths = service_data_paths(database)
    verify_prepared_database(
        paths.database,
        odds_raw_root=paths.odds_raw_root,
    )
    # The environment handoff keeps uvicorn reload children on this same path.
    os.environ["DATABASE_PATH"] = str(database)
    from . import queries
    queries.init_db(str(database))
    from .app import app, configure_milestone_revocation
    revocation_config = resolve_milestone_revocation_config(
        config, config_path, database
    )
    configure_milestone_revocation(app, revocation_config)
    logging.getLogger("web").info(
        "Database path (%s): %s", source, queries.DB_PATH
    )

    if reload and revocation_config is not None:
        raise ValueError("reload cannot preserve explicit milestone revocation app state")
    uvicorn.run(
        "web.app:app" if revocation_config is None else app,
        host=host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    main()
