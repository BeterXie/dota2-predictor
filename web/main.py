"""Entry point: python -m web.main"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import uvicorn
import yaml

from database.engine import require_database_url
from live_betting.process_control import terminate_subprocess_tree
from live_betting.runtime_schema import verify_runtime_schema
from live_betting.storage import LiveBettingStore
from shared.environment import load_environment_file


ROOT = Path(__file__).resolve().parents[1]


def _start_postmatch_worker(
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> Any:
    return popen_factory(
        [
            sys.executable,
            "-u",
            "-m",
            "live_betting.postmatch_monitor",
            "--all",
            "--interval",
            "60",
            "--schema-prepared",
        ],
        cwd=str(ROOT),
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        env=os.environ.copy(),
    )


def _start_strict_ingest_worker(
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> Any:
    return popen_factory(
        [
            sys.executable,
            "-u",
            str(ROOT / "scripts" / "run_strict_event_ingest.py"),
            "--interval",
            "30",
            "--schema-prepared",
        ],
        cwd=str(ROOT),
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        env=os.environ.copy(),
    )


def _start_map_decision_worker(
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> Any:
    return popen_factory(
        [
            sys.executable,
            "-u",
            "-m",
            "live_betting.map_decision_checkpoints",
            "--interval",
            "1",
            "--schema-prepared",
        ],
        cwd=str(ROOT),
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        env=os.environ.copy(),
    )


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
    load_environment_file(ROOT / ".env")
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
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    from . import queries
    from .routers.control import control_service
    queries.init_db(database_url)
    postmatch_process = None
    strict_ingest_process = None
    map_decision_process = None
    try:
        with LiveBettingStore(database_url) as store:
            store.init_schema()
            verify_runtime_schema(store.connection)
            raybet_result = control_service.ensure_started(
                store.connection,
                "raybet_collector",
                ignore_supervisor_heartbeat=True,
            )
            vision_result = control_service.ensure_started(
                store.connection,
                "vision_supervisor",
                ignore_supervisor_heartbeat=True,
            )
        postmatch_process = _start_postmatch_worker()
        strict_ingest_process = _start_strict_ingest_worker()
        map_decision_process = _start_map_decision_worker()
        logging.getLogger("web").info(
            "PostgreSQL database configured from %s", source
        )
        logging.getLogger("web").info(
            "RayBet startup: %s",
            raybet_result.get("detail") or raybet_result.get("status"),
        )
        logging.getLogger("web").info(
            "Stable Vision startup: %s",
            vision_result.get("detail") or vision_result.get("status"),
        )
        logging.getLogger("web").info(
            "Postmatch sync startup: pid=%s",
            postmatch_process.pid,
        )
        logging.getLogger("web").info(
            "Strict ingest startup: pid=%s",
            strict_ingest_process.pid,
        )
        logging.getLogger("web").info(
            "Map decision checkpoint startup: pid=%s",
            map_decision_process.pid,
        )
        uvicorn.run(
            "web.app:app",
            host=host,
            port=port,
            reload=reload,
        )
    finally:
        if map_decision_process is not None:
            terminate_subprocess_tree(map_decision_process)
        if strict_ingest_process is not None:
            terminate_subprocess_tree(strict_ingest_process)
        if postmatch_process is not None:
            terminate_subprocess_tree(postmatch_process)
        control_service.close()
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url


if __name__ == "__main__":
    main()
