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
    logging.getLogger("web").info(
        "Database path (%s): %s", source, queries.DB_PATH
    )

    uvicorn.run("web.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
