"""Entry point: python -m web.main"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Mapping, Sequence

import uvicorn
import yaml

from database.engine import require_database_url
from live_betting.runtime_schema import verify_runtime_schema
from live_betting.storage import LiveBettingStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
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


def resolve_database_url(
    cli_database_url: str | None,
    config: Mapping[str, object],
    config_path: Path,
    environment: Mapping[str, str],
) -> tuple[str, str]:
    """Resolve the one PostgreSQL runtime authority by documented precedence."""

    if cli_database_url is not None:
        return require_database_url(cli_database_url), "cli"
    configured_environment = environment.get("DATABASE_URL")
    if configured_environment:
        return require_database_url(configured_environment), "environment"
    configured_file = config.get("database_url")
    if configured_file:
        return require_database_url(str(configured_file)), "config"
    return require_database_url(None, environ=environment), "environment"


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

    database_url, source = resolve_database_url(
        args.database_url,
        config,
        config_path,
        os.environ,
    )
    os.environ["DATABASE_URL"] = database_url
    from . import queries
    queries.init_db(database_url)
    with LiveBettingStore(database_url) as store:
        store.init_schema()
        verify_runtime_schema(store.connection)
    from .app import app, configure_milestone_revocation
    revocation_config = None
    configure_milestone_revocation(app, None)
    logging.getLogger("web").info(
        "PostgreSQL database configured from %s", source
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
