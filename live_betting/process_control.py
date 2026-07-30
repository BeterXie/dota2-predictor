"""Process supervision helpers independent of database storage."""

from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass
from typing import Any, Callable

import psutil


MARKET_SOURCE_POLICY = "direct_primary"
_PATH_OPTIONS = frozenset(
    {
        "--archive-root",
        "--coverage-report",
        "--evidence-dir",
        "--log-dir",
        "--output",
        "--output-dir",
        "--raw-dir",
        "--report",
        "--vision-jsonl",
    }
)


@dataclass(frozen=True, order=True)
class ProcessIdentity:
    pid: int
    created_at: float


@dataclass(frozen=True)
class TerminationResult:
    ok: bool
    detail: str | None = None


def _path_argument(command: list[str], index: int) -> bool:
    argument = command[index]
    option, separator, _value = argument.partition("=")
    if separator:
        return option in _PATH_OPTIONS
    return index > 0 and command[index - 1] in _PATH_OPTIONS


def command_comparison_key(command: list[str]) -> tuple[str, ...]:
    if os.name != "nt":
        return tuple(command)
    return tuple(
        ntpath.normcase(ntpath.normpath(argument))
        if _path_argument(command, index)
        else argument
        for index, argument in enumerate(command)
    )


def terminate_process_tree(
    process: Any,
    *,
    process_factory: Callable[[int], Any] = psutil.Process,
    expected_root: ProcessIdentity | None = None,
    terminate_timeout: float = 8,
    kill_timeout: float = 3,
    max_tree_passes: int = 8,
) -> TerminationResult:
    del max_tree_passes
    try:
        identity = ProcessIdentity(int(process.pid), float(process.create_time()))
    except (AttributeError, OSError, TypeError, ValueError, psutil.Error) as error:
        return TerminationResult(False, f"root_identity_unverifiable:{type(error).__name__}")
    if expected_root is not None and identity != expected_root:
        return TerminationResult(False, "root_identity_changed")
    try:
        children = list(process.children(recursive=True))
    except (AttributeError, OSError, psutil.Error) as error:
        return TerminationResult(False, f"tree_enumeration_failed:{type(error).__name__}")
    targets = [*reversed(children), process]
    for target in targets:
        try:
            target.terminate()
        except psutil.NoSuchProcess:
            continue
        except (AttributeError, OSError, psutil.Error):
            pass
    _, alive = psutil.wait_procs(targets, timeout=terminate_timeout)
    for target in alive:
        try:
            target.kill()
        except psutil.NoSuchProcess:
            continue
        except (AttributeError, OSError, psutil.Error):
            pass
    _, survivors = psutil.wait_procs(alive, timeout=kill_timeout)
    verified: list[str] = []
    for target in survivors:
        try:
            current = process_factory(int(target.pid))
            current.create_time()
        except psutil.NoSuchProcess:
            continue
        except (AttributeError, OSError, TypeError, ValueError, psutil.Error):
            verified.append(str(getattr(target, "pid", "unknown")))
        else:
            verified.append(str(target.pid))
    if verified:
        return TerminationResult(False, "process_still_alive:" + ",".join(verified))
    return TerminationResult(True)


def terminate_subprocess_tree(
    process_handle: Any,
    *,
    process_factory: Callable[[int], Any] = psutil.Process,
    terminate_timeout: float = 8,
    kill_timeout: float = 3,
    max_tree_passes: int = 8,
) -> TerminationResult:
    try:
        if process_handle.poll() is not None:
            return TerminationResult(True)
        process = process_factory(int(process_handle.pid))
        identity = ProcessIdentity(int(process.pid), float(process.create_time()))
    except psutil.NoSuchProcess:
        return TerminationResult(process_handle.poll() is not None)
    except (AttributeError, OSError, TypeError, ValueError, psutil.Error) as error:
        return TerminationResult(
            False,
            f"subprocess_identity_unverifiable:{type(error).__name__}",
        )
    return terminate_process_tree(
        process,
        process_factory=process_factory,
        expected_root=identity,
        terminate_timeout=terminate_timeout,
        kill_timeout=kill_timeout,
        max_tree_passes=max_tree_passes,
    )


__all__ = [
    "MARKET_SOURCE_POLICY",
    "ProcessIdentity",
    "TerminationResult",
    "command_comparison_key",
    "terminate_process_tree",
    "terminate_subprocess_tree",
]
