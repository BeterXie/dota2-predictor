from __future__ import annotations
# ruff: noqa: E402

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

rehearsal = pytest.importorskip(
    "scripts.cross_version_handoff_rehearsal",
    reason="cross-version SQLite handoff rehearsal is no longer a product path",
)


class CrossVersionHandoffRehearsalTests(unittest.TestCase):
    def test_last_json_line_uses_final_nonempty_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stdout.jsonl"
            path.write_text('{"phase": 1}\n\n{"phase": 2}\n', encoding="utf-8")
            self.assertEqual(rehearsal._last_json_line(path), {"phase": 2})

    def test_run_rejects_nonempty_output_directory_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            (output / "existing.txt").write_text("preserve", encoding="utf-8")
            recovery = root / "recovery"
            recovery.mkdir()
            production = root / "production.db"
            production.write_bytes(b"sentinel")
            with self.assertRaisesRegex(ValueError, "output directory must be empty"):
                rehearsal.run_rehearsal(
                    Namespace(
                        recovery_workspace=recovery,
                        output_dir=output,
                        production_database=production,
                        python_executable=None,
                    )
                )
            self.assertEqual((output / "existing.txt").read_text(), "preserve")

    def test_run_rejects_nonbaseline_recovery_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recovery = root / "recovery"
            recovery.mkdir()
            production = root / "production.db"
            production.write_bytes(b"sentinel")
            with patch.object(rehearsal, "_workspace_head", return_value="wrong"):
                with self.assertRaisesRegex(ValueError, "exact clean baseline"):
                    rehearsal.run_rehearsal(
                        Namespace(
                            recovery_workspace=recovery,
                            output_dir=root / "output",
                            production_database=production,
                            python_executable=None,
                        )
                    )

    def test_run_writes_passed_manifest_when_every_guarded_phase_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recovery = root / "recovery"
            (recovery / "live_betting").mkdir(parents=True)
            (recovery / "live_betting" / "pending_order_recovery.py").write_text(
                "# candidate\n", encoding="utf-8"
            )
            production = root / "production.db"
            production.write_bytes(b"sentinel")
            output = root / "output"

            phase_payloads = {
                "01-build": {"shadow_orders": 1},
                "02-add-successor": {
                    "guard_result": "passed",
                    "attempted_production_connections": 0,
                },
                "03-recovery": {
                    "status": "recovery_progress",
                    "filled": 1,
                    "pending_after": 0,
                },
                "04-settle": {
                    "guard_result": "passed",
                    "attempted_production_connections": 0,
                    "first_settlement_inserted": True,
                    "second_settlement_inserted": False,
                },
                "05-verify": {
                    "guard_result": "passed",
                    "attempted_production_connections": 0,
                    "counts_stable": True,
                    "second_process_inserted": False,
                },
                "06-notification-gate": {
                    "status": "passed",
                    "guard_result": "passed",
                    "attempted_production_connections": 0,
                },
                "07-revocation": {
                    "status": "passed",
                    "guard_result": "passed",
                    "attempted_production_connections": 0,
                },
            }

            def fake_run(command, *, cwd, stdout_path, stderr_path, env=None):
                phase = stdout_path.name.split(".", 1)[0]
                stdout_path.parent.mkdir(parents=True, exist_ok=True)
                stdout_path.write_text(
                    json.dumps(phase_payloads[phase]) + "\n", encoding="utf-8"
                )
                stderr_path.write_text("", encoding="utf-8")
                if phase == "01-build":
                    fixture = output / "fixture" / "rollback-fixture.db"
                    fixture.parent.mkdir(parents=True, exist_ok=True)
                    fixture.write_bytes(b"fixture")
                return {
                    "command": list(command),
                    "cwd": str(cwd),
                    "exit_status": 0,
                    "stdout": {
                        "path": str(stdout_path.resolve()),
                        "sha256": rehearsal._sha256(stdout_path),
                        "bytes": stdout_path.stat().st_size,
                    },
                    "stderr": {
                        "path": str(stderr_path.resolve()),
                        "sha256": rehearsal._sha256(stderr_path),
                        "bytes": 0,
                    },
                }

            with (
                patch.object(
                    rehearsal,
                    "_workspace_head",
                    side_effect=[rehearsal.BASELINE_COMMIT, "current-head"],
                ),
                patch.object(rehearsal, "_run_command", side_effect=fake_run),
            ):
                status = rehearsal.run_rehearsal(
                    Namespace(
                        recovery_workspace=recovery,
                        output_dir=output,
                        production_database=production,
                        python_executable=None,
                    )
                )

            self.assertEqual(status, 0)
            result = json.loads(
                (output / "rehearsal-result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["schema"], rehearsal.RESULT_SCHEMA)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["recovery_head"], rehearsal.BASELINE_COMMIT)
            self.assertEqual(result["current_head"], "current-head")
            self.assertEqual(result["attempted_production_connections"], 0)


if __name__ == "__main__":
    unittest.main()
