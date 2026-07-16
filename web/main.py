"""Entry point: python -m web.main"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import uvicorn
import yaml


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config_path = Path(__file__).with_name("config.yaml")
    config = {}
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8000)
    reload = server_cfg.get("reload", False)

    # Resolve database path from config and pass to queries module
    db_path = config.get("database", "../data/dota2.db")
    db_path = str((Path(__file__).parent / db_path).resolve())
    from . import queries
    queries.init_db(db_path)
    logging.getLogger("web").info("Database path: %s", queries.DB_PATH)

    sys.argv = [sys.argv[0]]
    uvicorn.run("web.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
