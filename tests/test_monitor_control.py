from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import psutil

from web.control import COMPONENTS, ControlService


def test_control_exposes_retained_collector_and_stable_vision(tmp_path: Path) -> None:
    service = ControlService(project_dir=tmp_path, python_executable=sys.executable)

    assert tuple(COMPONENTS) == ("raybet_collector", "vision_supervisor")
    command = service.command_for("raybet_collector")
    assert command[:4] == [sys.executable, "-u", "-m", "live_betting.monitor"]
    assert "--schema-prepared" in command
    vision_command = service.command_for("vision_supervisor")
    assert Path(vision_command[2]).name == "supervise_raybet_streams_stable.py"
    assert "--schema-prepared" in vision_command


def test_control_command_keeps_raw_artifacts_under_project_data(tmp_path: Path) -> None:
    service = ControlService(project_dir=tmp_path, python_executable=sys.executable)

    command = service.command_for("raybet_collector")
    raw_root = Path(command[command.index("--raw-dir") + 1])

    assert raw_root == tmp_path.resolve() / "data" / "live_betting" / "raw-v2"


class _HeartbeatConnection:
    def execute(self, query: str, params: tuple[str, ...]):
        if "monitor_process_registry" in query:
            return _RowResult(
                {
                    "status": "running",
                    "pid": 9876,
                    "process_created_at": 123.0,
                }
            )
        if "service_health" in query:
            return _RowResult(
                {"last_heartbeat_at": datetime.now(timezone.utc).isoformat()}
            )
        raise AssertionError(query)


class _RowResult:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def fetchone(self):
        return self.row


def test_stale_registered_supervisor_pid_does_not_trust_fresh_heartbeat(
    tmp_path: Path,
) -> None:
    def missing_process(_: int):
        raise psutil.NoSuchProcess(9876)

    service = ControlService(
        project_dir=tmp_path,
        process_factory=missing_process,
    )

    assert service._fresh_supervisor_heartbeat(
        _HeartbeatConnection(), "vision_supervisor"
    ) is None
