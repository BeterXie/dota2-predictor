from __future__ import annotations

import contextlib
import unittest
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from fastapi.testclient import TestClient

from event_intelligence.storage import IntelligenceStorage
from live_betting.runtime_schema import prepare_runtime_schema
from live_betting.service_coordination import (
    ProcessIdentity,
    SingleInstanceLock,
    TerminationResult,
)
from live_betting.storage import LiveBettingStore
from web import app as web_app
from web import queries
from web.queries import _sort_heroes_by_position
from web.schemas import PrematchRequest


ROOT = Path(__file__).resolve().parent.parent


def hero(hero_id: int, lane_role: int, gpm: int) -> dict:
    return {
        "hero_id": hero_id,
        "account_id": hero_id + 1_000,
        "lane_role": lane_role,
        "gold_per_min": gpm,
        "player_slot": hero_id,
    }


class DraftSortingTests(unittest.TestCase):
    def test_single_offlaner_is_not_duplicated(self) -> None:
        rows = [
            hero(1, 1, 600),
            hero(2, 1, 300),
            hero(3, 2, 550),
            hero(4, 2, 250),
            hero(5, 3, 450),
        ]
        ordered = _sort_heroes_by_position(rows)
        self.assertEqual([row["hero_id"] for row in ordered], [1, 3, 5, 4, 2])
        self.assertEqual(len({row["account_id"] for row in ordered}), 5)


class PrematchSchemaTests(unittest.TestCase):
    def valid(self) -> dict:
        return {
            "radiant_id": 10,
            "dire_id": 20,
            "radiant_heroes": [1, 2, 3, 4, 5],
            "dire_heroes": [6, 7, 8, 9, 10],
        }

    def test_rosters_are_exact_and_bilateral(self) -> None:
        values = self.valid()
        values["radiant_players"] = [101]
        values["dire_players"] = [201]
        with self.assertRaises(ValidationError):
            PrematchRequest(**values)

        values = self.valid()
        values["radiant_players"] = [101, 102, 103, 104, 105]
        with self.assertRaisesRegex(ValidationError, "both sides"):
            PrematchRequest(**values)

    def test_duplicate_draft_is_rejected(self) -> None:
        values = self.valid()
        values["dire_heroes"][-1] = 5
        with self.assertRaisesRegex(ValidationError, "10 distinct"):
            PrematchRequest(**values)


class FetchAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        web_app._fetch_process = None
        web_app._fetch_process_identity = None
        web_app._fetch_authority_environment = None
        web_app._fetch_authority_context = None

    def tearDown(self) -> None:
        web_app._fetch_process = None
        web_app._fetch_process_identity = None
        web_app._fetch_authority_environment = None
        web_app._fetch_authority_context = None

    @staticmethod
    def request(host: str) -> SimpleNamespace:
        return SimpleNamespace(client=SimpleNamespace(host=host))

    def test_fetch_requires_loopback_and_admin_header(self) -> None:
        for host, header in (("192.0.2.1", "fetch"), ("127.0.0.1", None)):
            with self.subTest(host=host, header=header):
                with self.assertRaises(HTTPException) as raised:
                    web_app.trigger_fetch(
                        self.request(host), match_id=1, admin_action=header
                    )
                self.assertEqual(raised.exception.status_code, 403)

    def test_fetch_registration_baseexception_terminates_and_cleans_authority(
        self,
    ) -> None:
        for interruption in (KeyboardInterrupt(), SystemExit(9)):
            with self.subTest(interruption=type(interruption).__name__):
                with tempfile.TemporaryDirectory() as directory:
                    database = Path(directory) / "candidate.db"
                    sqlite3.connect(database).close()
                    handle = SimpleNamespace(pid=7601, poll=lambda: None)
                    exited: list[bool] = []
                    real_process = web_app.psutil.Process

                    def interrupt_child(pid: int):
                        if pid == handle.pid:
                            raise interruption
                        return real_process(pid)

                    @contextlib.contextmanager
                    def authority(_: Path):
                        try:
                            yield {"DOTA2_WEB_FETCH_AUTHORITY_V1": "marker"}
                        finally:
                            exited.append(True)

                    with (
                        patch.object(queries, "DB_PATH", str(database)),
                        patch.object(web_app, "web_fetch_child_authority", authority),
                        patch.object(web_app.subprocess, "Popen", return_value=handle),
                        patch.object(
                            web_app.psutil,
                            "Process",
                            side_effect=interrupt_child,
                        ),
                        patch.object(
                            web_app,
                            "terminate_subprocess_tree",
                            return_value=TerminationResult(True),
                        ) as terminate,
                    ):
                        with self.assertRaises(type(interruption)):
                            web_app.trigger_fetch(
                                self.request("127.0.0.1"),
                                match_id=123,
                                admin_action="fetch",
                            )

                    terminate.assert_called_once()
                    self.assertIs(terminate.call_args.args[0], handle)
                    self.assertEqual(exited, [True])
                    self.assertIsNone(web_app._fetch_process)
                    self.assertIsNone(web_app._fetch_authority_context)

    def test_fetch_popen_baseexception_cleans_published_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            sqlite3.connect(database).close()
            exited: list[bool] = []

            @contextlib.contextmanager
            def authority(_: Path):
                try:
                    yield {"DOTA2_WEB_FETCH_AUTHORITY_V1": "marker"}
                finally:
                    exited.append(True)

            with (
                patch.object(queries, "DB_PATH", str(database)),
                patch.object(web_app, "web_fetch_child_authority", authority),
                patch.object(
                    web_app.subprocess,
                    "Popen",
                    side_effect=KeyboardInterrupt(),
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    web_app.trigger_fetch(
                        self.request("127.0.0.1"),
                        match_id=123,
                        admin_action="fetch",
                    )

            self.assertEqual(exited, [True])
            self.assertIsNone(web_app._fetch_process)
            self.assertIsNone(web_app._fetch_authority_context)

    def test_fetch_cleanup_failure_retains_handle_and_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            sqlite3.connect(database).close()
            handle = SimpleNamespace(pid=7602, poll=lambda: None)
            real_process = web_app.psutil.Process

            def interrupt_child(pid: int):
                if pid == handle.pid:
                    raise KeyboardInterrupt
                return real_process(pid)

            @contextlib.contextmanager
            def authority(_: Path):
                try:
                    yield {"DOTA2_WEB_FETCH_AUTHORITY_V1": "marker"}
                finally:
                    raise RuntimeError("injected authority cleanup failure")

            with (
                patch.object(queries, "DB_PATH", str(database)),
                patch.object(web_app, "web_fetch_child_authority", authority),
                patch.object(web_app.subprocess, "Popen", return_value=handle),
                patch.object(
                    web_app.psutil,
                    "Process",
                    side_effect=interrupt_child,
                ),
                patch.object(
                    web_app,
                    "terminate_subprocess_tree",
                    return_value=TerminationResult(True),
                ),
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    web_app.trigger_fetch(
                        self.request("127.0.0.1"),
                        match_id=123,
                        admin_action="fetch",
                    )

            self.assertTrue(
                any(
                    "authority cleanup failure" in note
                    for note in getattr(raised.exception, "__notes__", ())
                )
            )
            self.assertIs(web_app._fetch_process, handle)
            self.assertIsNotNone(web_app._fetch_authority_context)
            web_app._clear_fetch_process()
            self.assertIsNone(web_app._fetch_process)
            self.assertIsNone(web_app._fetch_authority_context)

    def test_only_one_fetch_process_can_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            sqlite3.connect(database).close()
            command: list[str] = []
            child_environment: dict[str, str] = {}
            handle = SimpleNamespace(pid=7301, poll=lambda: None)

            def popen(value: list[str], **kwargs: object) -> object:
                command[:] = value
                child_environment.update(kwargs["env"])
                return handle

            process = SimpleNamespace(
                pid=7301,
                create_time=lambda: 123.5,
                cmdline=lambda: list(command),
            )
            with (
                patch.object(queries, "DB_PATH", str(database)),
                patch.object(web_app.subprocess, "Popen", side_effect=popen) as spawn,
                patch.object(web_app.psutil, "Process", return_value=process),
                patch.object(
                    web_app,
                    "web_fetch_child_authority",
                    return_value=contextlib.nullcontext({
                        "DOTA2_WEB_FETCH_AUTHORITY_V1": "test-marker"
                    }),
                ) as issue_authority,
            ):
                result = web_app.trigger_fetch(
                    self.request("127.0.0.1"),
                    match_id=123,
                    force=True,
                    admin_action="fetch",
                )
                self.assertEqual(result["status"], "started")
                self.assertEqual(
                    command[3:5],
                    ["--database", str(database.resolve())],
                )
                self.assertEqual(
                    web_app._fetch_process_identity,
                    ProcessIdentity(7301, 123.5),
                )
                self.assertEqual(
                    child_environment["DOTA2_WEB_FETCH_AUTHORITY_V1"],
                    "test-marker",
                )
                with self.assertRaises(HTTPException) as raised:
                    web_app.trigger_fetch(
                        self.request("127.0.0.1"),
                        match_id=123,
                        force=True,
                        admin_action="fetch",
                    )
                self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual(spawn.call_count, 1)
                issue_authority.assert_called_once_with(database.resolve())

    def test_fetch_does_not_spawn_while_supervisor_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            sqlite3.connect(database).close()
            with (
                patch.object(queries, "DB_PATH", str(database)),
                patch.object(web_app.subprocess, "Popen") as spawn,
                SingleInstanceLock(database.with_suffix(".service.lock")),
                self.assertRaises(HTTPException) as raised,
            ):
                web_app.trigger_fetch(
                    self.request("127.0.0.1"),
                    match_id=123,
                    admin_action="fetch",
                )

            self.assertEqual(raised.exception.status_code, 409)
            spawn.assert_not_called()

    def test_each_fetch_process_gets_a_fresh_authority_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            sqlite3.connect(database).close()
            commands: list[list[str]] = []
            child_markers: list[str] = []
            issued = 0

            def authority(_: Path) -> contextlib.AbstractContextManager[dict[str, str]]:
                nonlocal issued
                issued += 1
                return contextlib.nullcontext({
                    "DOTA2_WEB_FETCH_AUTHORITY_V1": f"marker-{issued}"
                })

            def popen(value: list[str], **kwargs: object) -> object:
                commands.append(list(value))
                child_markers.append(
                    kwargs["env"]["DOTA2_WEB_FETCH_AUTHORITY_V1"]
                )
                return SimpleNamespace(pid=7500 + len(commands), poll=lambda: None)

            def process(pid: int) -> object:
                index = pid - 7501
                return SimpleNamespace(
                    pid=pid,
                    create_time=lambda: float(pid),
                    cmdline=lambda: list(commands[index]),
                )

            with (
                patch.object(queries, "DB_PATH", str(database)),
                patch.object(web_app, "web_fetch_child_authority", side_effect=authority),
                patch.object(web_app.subprocess, "Popen", side_effect=popen),
                patch.object(web_app.psutil, "Process", side_effect=process),
            ):
                for match_id in (101, 102):
                    result = web_app.trigger_fetch(
                        self.request("127.0.0.1"),
                        match_id=match_id,
                        admin_action="fetch",
                    )
                    self.assertEqual(result["status"], "started")
                    web_app._clear_fetch_process()

            self.assertEqual(child_markers, ["marker-1", "marker-2"])

    def test_fetch_shutdown_uses_registered_pid_and_creation_time(self) -> None:
        handle = SimpleNamespace(pid=7401, poll=lambda: None)
        identity = ProcessIdentity(7401, 456.5)
        web_app._fetch_process = handle
        web_app._fetch_process_identity = identity
        process = SimpleNamespace(pid=7401)

        with (
            patch.object(web_app.psutil, "Process", return_value=process),
            patch.object(
                web_app,
                "terminate_process_tree",
                return_value=TerminationResult(True),
            ) as terminate,
        ):
            web_app._shutdown_fetch_process()

        self.assertIsNone(web_app._fetch_process)
        self.assertIsNone(web_app._fetch_process_identity)
        self.assertEqual(terminate.call_args.kwargs["expected_root"], identity)


class PrematchMarkupTests(unittest.TestCase):
    def test_comparison_css_is_inside_style_element(self) -> None:
        html = (ROOT / "web" / "static" / "prematch.html").read_text(
            encoding="utf-8"
        )
        style = html.split("<style>", 1)[1].split("</style>", 1)[0]
        legend = html.split('<div class="matrix-legend">', 1)[1].split(
            "</div>", 1
        )[0]
        self.assertIn(".comparison-section", style)
        self.assertNotIn(".comparison-section", legend)

    def test_player_roster_checks_every_array_index(self) -> None:
        html = (ROOT / "web" / "static" / "prematch.html").read_text(
            encoding="utf-8"
        )
        roster_function = html.split("function completePlayerRoster", 1)[1].split(
            "// ---- Predict ----", 1
        )[0]
        self.assertIn("players[index]", roster_function)
        self.assertNotIn("players.every", roster_function)
        self.assertIn("includePlayerRosters ? radiantPlayers : null", html)
        self.assertIn("includePlayerRosters ? direPlayers : null", html)


class WebEntryRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "web-entry-routes.db"
        with IntelligenceStorage(self.path) as storage:
            storage.init_schema()
        with LiveBettingStore(self.path) as store:
            store.init_schema()
        connection = sqlite3.connect(self.path)
        try:
            prepare_runtime_schema(connection)
        finally:
            connection.close()
        self.database_patch = patch.object(queries, "DB_PATH", str(self.path))
        self.database_patch.start()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.directory.cleanup()

    def test_root_opens_monitor_and_preserves_view_query(self) -> None:
        with TestClient(web_app.app) as client:
            response = client.get("/?view=intelligence", follow_redirects=False)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers["location"], "/monitor?view=intelligence")

    def test_legacy_matches_page_has_an_explicit_route(self) -> None:
        with TestClient(web_app.app) as client:
            response = client.get("/matches")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Dota 2 Predictor", response.text)
            self.assertIn('href="/monitor"', response.text)


if __name__ == "__main__":
    unittest.main()
