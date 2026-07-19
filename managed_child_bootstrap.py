"""Start a managed child only after its parent-bound authority is proven."""

from __future__ import annotations

import json
import math
import os
import runpy
import sys
import time
from pathlib import Path
from typing import Callable


_AUTHORITY_ENV = "DOTA2_MANAGER_CHILD_AUTHORITY_V1"
_TARGET_SENTINEL = "--target-argv"
_BIND_TIMEOUT_SECONDS = 1.0
_BIND_POLL_SECONDS = 0.01


def _command_database(command: list[str]) -> Path:
    candidates: list[str] = []
    for index, argument in enumerate(command):
        if argument == _TARGET_SENTINEL:
            break
        if argument == "--database":
            if index + 1 >= len(command):
                raise RuntimeError("managed child target database is invalid")
            candidates.append(command[index + 1])
        elif argument.startswith("--database="):
            candidates.append(argument.split("=", 1)[1])
    if len(candidates) != 1 or not candidates[0]:
        raise RuntimeError("managed child target database is invalid")
    database = Path(candidates[0])
    if not database.is_absolute():
        raise RuntimeError("managed child target database is invalid")
    return database.resolve()


def _bootstrap_arguments(argv: list[str]) -> tuple[Path, list[str]]:
    if len(argv) < 6 or argv[1] != "--database" or argv[3] != _TARGET_SENTINEL:
        raise RuntimeError("managed child bootstrap argv is invalid")
    if argv.count(_TARGET_SENTINEL) != 1:
        raise RuntimeError("managed child target argv is invalid")
    database = Path(argv[2])
    target = argv[4:]
    if not database.is_absolute() or len(target) < 2:
        raise RuntimeError("managed child target argv is invalid")
    database = database.resolve()
    if _command_database(target) != database:
        raise RuntimeError("managed child target database differs")
    return database, target


def _bound_marker(marker: str, persisted: bytes) -> bool:
    if persisted == marker.encode("ascii"):
        return False
    try:
        original = json.loads(marker)
        bound = json.loads(persisted.decode("ascii"))
        if not isinstance(original, dict) or not isinstance(bound, dict):
            raise TypeError
        if set(bound) != {*original, "child_identity"}:
            raise ValueError
        identity = bound.pop("child_identity")
        if bound != original or not isinstance(identity, dict):
            raise ValueError
        if set(identity) != {"pid", "created_at"}:
            raise ValueError
        created_at = float(identity["created_at"])
        if (
            int(identity["pid"]) != os.getpid()
            or not math.isfinite(created_at)
            or created_at <= 0
        ):
            raise ValueError
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RuntimeError("manager child authority marker differs") from error
    return True


def _wait_for_parent_binding() -> None:
    marker = os.environ.get(_AUTHORITY_ENV)
    if marker is None:
        raise RuntimeError("manager child authority is missing")
    try:
        payload = json.loads(marker)
        if not isinstance(payload, dict) or not isinstance(payload["marker_path"], str):
            raise TypeError
        marker_path = Path(payload["marker_path"])
        if not marker_path.is_absolute():
            raise ValueError
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError("manager child authority marker is invalid") from error

    deadline = time.monotonic() + _BIND_TIMEOUT_SECONDS
    while True:
        try:
            persisted = marker_path.read_bytes()
        except OSError as error:
            raise RuntimeError("manager child authority marker is unavailable") from error
        if _bound_marker(marker, persisted):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("manager child authority binding timed out")
        time.sleep(min(_BIND_POLL_SECONDS, remaining))


def _run_target(
    target: list[str],
    entrypoint_parser: Callable[[list[str]], tuple[str, str, list[str]]],
) -> None:
    kind, entrypoint, arguments = entrypoint_parser(target)
    if kind == "module":
        sys.argv[:] = [entrypoint, *arguments]
        runpy.run_module(entrypoint, run_name="__main__", alter_sys=True)
        return
    script = Path(entrypoint)
    if not script.is_absolute():
        raise RuntimeError("managed child target script is relative")
    sys.path[0] = str(script.parent)
    sys.argv[:] = [str(script), *arguments]
    runpy.run_path(str(script), run_name="__main__")


def main() -> int:
    database, target = _bootstrap_arguments(sys.argv)

    # CPython loads site and .pth files before any script; the selected
    # interpreter environment is therefore part of the startup TCB. Project
    # package imports remain behind the parent-bound marker gate below.
    _wait_for_parent_binding()
    from live_betting.service_coordination import (
        database_writer_authority,
        managed_child_entrypoint,
    )

    with database_writer_authority(database):
        _run_target(target, managed_child_entrypoint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
