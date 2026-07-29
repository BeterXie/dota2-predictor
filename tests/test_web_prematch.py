from __future__ import annotations

import contextlib
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
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
from live_betting.stratz_rosh_client import StratzRoshError
from web import app as web_app
from web import queries
from web.queries import _sort_heroes_by_position
from web.routers.leagues import router as league_router
from web.schemas import PrematchRequest


ROOT = Path(__file__).resolve().parent.parent


class PrematchShellRouteTests(unittest.TestCase):
    def test_prematch_uses_integrated_monitor_frontend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory)
            index = dist / "index.html"
            index.write_text("<div id='root'></div>", encoding="utf-8")
            with patch.object(web_app, "MONITOR_DIST_DIR", dist):
                response = web_app.serve_prematch_page()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Path(response.path).resolve(), index.resolve())
        self.assertEqual(
            response.headers["cache-control"],
            "no-cache, no-store, must-revalidate",
        )

    def test_matches_redirects_to_monitor_history(self) -> None:
        response = web_app.serve_legacy_matches()

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/monitor?view=replay")


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

        values = self.valid()
        values["radiant_players"] = [101, 102, 103, 104, 105]
        values["dire_players"] = [201, 202, 203, 204, 205]
        with self.assertRaisesRegex(ValidationError, "trusted source match"):
            PrematchRequest(**values)

        values["source_match_id"] = 123
        self.assertEqual(PrematchRequest(**values).source_match_id, 123)

    def test_duplicate_draft_is_rejected(self) -> None:
        values = self.valid()
        values["dire_heroes"][-1] = 5
        with self.assertRaisesRegex(ValidationError, "10 distinct"):
            PrematchRequest(**values)


class LeagueApiTests(unittest.TestCase):
    def test_league_response_preserves_match_count(self) -> None:
        app = FastAPI()
        app.include_router(league_router)
        with patch.object(
            queries,
            "get_leagues",
            return_value=[{
                "leagueid": 19785,
                "name": "Esports World Cup 2026",
                "tier": "professional",
                "match_count": 157,
            }],
        ):
            with TestClient(app) as client:
                response = client.get("/api/leagues")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["match_count"], 157)


class PrematchRoshRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "prematch-rosh.db"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE teams (team_id INTEGER PRIMARY KEY)")
        connection.executemany("INSERT INTO teams VALUES (?)", [(10,), (20,)])
        connection.commit()
        connection.close()
        self.database_patch = patch.object(queries, "DB_PATH", str(self.database))
        self.database_patch.start()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.directory.cleanup()

    @staticmethod
    def score(*, pure: float, adjusted: float | None, mode: str, coverage: int):
        values = {
            "pure_lineup_score": pure,
            "effective_lineup_score": adjusted if adjusted is not None else pure,
            "scoring_mode": mode,
            "player_coverage_count": coverage,
            "formula_version": "rosh-test",
            "source_name": "stratz",
            "source_week": 1_800_000_000,
            "source_as_of": datetime(2026, 7, 28, tzinfo=timezone.utc),
            "evidence_hash": "a" * 64,
            "evidence": {"pure_minute_table": [{"minute": 20}]},
        }
        if mode == "current_player_adjusted":
            values["current_player_adjusted_lineup_score"] = adjusted
        else:
            values["player_adjusted_lineup_score"] = adjusted
            values["stake_multiplier"] = 1.0 if adjusted is not None else 0.5
        return SimpleNamespace(**values)

    @staticmethod
    def output():
        def format_output(prediction, *_args):
            return {"prediction": prediction}

        return SimpleNamespace(
            format_output=format_output,
            save_prediction=lambda *_args: "prediction.json",
            _sanitize=lambda value: value,
        )

    @staticmethod
    def request(**values) -> PrematchRequest:
        return PrematchRequest(
            radiant_id=10,
            dire_id=20,
            radiant_heroes=[1, 2, 3, 4, 5],
            dire_heroes=[6, 7, 8, 9, 10],
            **values,
        )

    @staticmethod
    def local_draft() -> dict:
        return {
            "match_id": 123,
            "radiant_team_id": 10,
            "dire_team_id": 20,
            "radiant_heroes": [
                {"hero_id": hero_id, "account_id": 100 + hero_id}
                for hero_id in range(1, 6)
            ],
            "dire_heroes": [
                {"hero_id": hero_id, "account_id": 200 + hero_id}
                for hero_id in range(6, 11)
            ],
        }

    def test_manual_lineup_uses_pure_rosh_probability(self) -> None:
        score = self.score(pure=8.7, adjusted=None, mode="pure", coverage=0)
        client = SimpleNamespace(fetch_lineup_score=Mock(return_value=score))
        with patch.object(web_app, "_get_prematch_builder", return_value=(client, self.output())):
            result = web_app.create_prematch_prediction(self.request())

        prediction = result["prediction"]
        self.assertEqual(prediction["radiant_win_prob"], 0.587)
        self.assertEqual(prediction["scoring_mode"], "pure")
        self.assertEqual(prediction["player_coverage_count"], 0)
        call = client.fetch_lineup_score.call_args
        self.assertEqual(call.args, ([1, 2, 3, 4, 5], [6, 7, 8, 9, 10]))
        self.assertIsNotNone(call.kwargs["as_of"].utcoffset())

    def test_trusted_match_uses_current_player_highlights(self) -> None:
        score = self.score(
            pure=8.7,
            adjusted=9.1,
            mode="current_player_adjusted",
            coverage=10,
        )
        fetched = SimpleNamespace(
            context={
                "radiant_picks": [{"heroId": hero_id} for hero_id in range(1, 6)],
                "dire_picks": [{"heroId": hero_id} for hero_id in range(6, 11)],
            },
            score=score,
            minute_table=({"minute": 20, "win_rate_graph": 9.1},),
        )
        client = SimpleNamespace(
            fetch_historical_match_score=Mock(return_value=fetched)
        )
        request = self.request(
            source_match_id=123,
            radiant_players=[101, 102, 103, 104, 105],
            dire_players=[206, 207, 208, 209, 210],
        )
        with (
            patch.object(queries, "get_match_draft", return_value=self.local_draft()),
            patch.object(web_app, "_get_prematch_builder", return_value=(client, self.output())),
        ):
            result = web_app.create_prematch_prediction(request)

        prediction = result["prediction"]
        self.assertEqual(prediction["radiant_win_prob"], 0.591)
        self.assertEqual(prediction["scoring_mode"], "current_player_adjusted")
        self.assertEqual(prediction["player_coverage_count"], 10)
        client.fetch_historical_match_score.assert_called_once_with(
            123,
            include_current_player_adjustment=True,
        )

    def test_source_mismatch_is_rejected_before_stratz_request(self) -> None:
        client = SimpleNamespace(fetch_historical_match_score=Mock())
        request = self.request(source_match_id=123)
        draft = self.local_draft()
        draft["radiant_team_id"] = 99
        with (
            patch.object(queries, "get_match_draft", return_value=draft),
            patch.object(web_app, "_get_prematch_builder", return_value=(client, self.output())),
            self.assertRaises(HTTPException) as raised,
        ):
            web_app.create_prematch_prediction(request)

        self.assertEqual(raised.exception.status_code, 400)
        client.fetch_historical_match_score.assert_not_called()

    def test_stratz_failure_is_service_unavailable(self) -> None:
        client = SimpleNamespace(
            fetch_lineup_score=Mock(side_effect=StratzRoshError("rate limited"))
        )
        with (
            patch.object(web_app, "_get_prematch_builder", return_value=(client, self.output())),
            self.assertRaises(HTTPException) as raised,
        ):
            web_app.create_prematch_prediction(self.request())

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("STRATZ Rosh", raised.exception.detail)


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
        self.assertIn("source_match_id: loadedSourceMatchId", html)
        self.assertIn("invalidateLoadedSource();", html)
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

    def test_legacy_matches_page_redirects_to_monitor_history(self) -> None:
        with TestClient(web_app.app) as client:
            response = client.get("/matches", follow_redirects=False)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers["location"], "/monitor?view=replay")


if __name__ == "__main__":
    unittest.main()
