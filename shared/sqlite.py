"""Consistent SQLite connection policy for project-owned databases."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TypeAlias


BUSY_TIMEOUT_MS = 5_000
PathLike: TypeAlias = str | Path


def configure_connection(
    connection: sqlite3.Connection,
    *,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Apply the safety settings required on every project-owned connection."""

    if busy_timeout_ms <= 0:
        raise ValueError("busy_timeout_ms must be positive")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return connection


def connect(
    database: PathLike,
    *,
    read_only: bool = False,
    row_factory: type[sqlite3.Row] | None = None,
    wal: bool = False,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open and configure one SQLite connection.

    Read-only callers use SQLite's URI mode so a typo cannot create a new
    database. WAL is opt-in because changing journal mode is a write operation.
    """

    if read_only:
        path = Path(database).resolve()
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=busy_timeout_ms / 1_000,
        )
    else:
        connection = sqlite3.connect(
            str(database),
            timeout=busy_timeout_ms / 1_000,
        )
    if row_factory is not None:
        connection.row_factory = row_factory
    configure_connection(connection, busy_timeout_ms=busy_timeout_ms)
    if wal:
        if read_only:
            connection.close()
            raise ValueError("wal cannot be enabled by a read-only connection")
        connection.execute("PRAGMA journal_mode=WAL")
    return connection


__all__ = ["BUSY_TIMEOUT_MS", "configure_connection", "connect"]
