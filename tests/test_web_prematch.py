from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from web import app as web_app
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

    def tearDown(self) -> None:
        web_app._fetch_process = None

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

    def test_only_one_fetch_process_can_run(self) -> None:
        process = SimpleNamespace(poll=lambda: None)
        with patch.object(web_app.subprocess, "Popen", return_value=process) as popen:
            result = web_app.trigger_fetch(
                self.request("127.0.0.1"),
                match_id=123,
                force=True,
                admin_action="fetch",
            )
            self.assertEqual(result["status"], "started")
            with self.assertRaises(HTTPException) as raised:
                web_app.trigger_fetch(
                    self.request("127.0.0.1"),
                    match_id=123,
                    force=True,
                    admin_action="fetch",
                )
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(popen.call_count, 1)


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


if __name__ == "__main__":
    unittest.main()
