from __future__ import annotations

import sys
from pathlib import Path

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
