from __future__ import annotations

import argparse
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from fetch.stratz_detail import StratzDetailError

from live_betting.postmatch_monitor import (
    VisionDraftIdentity,
    _archive_optional_stratz_enrichment,
    _opendota_matches_vision_identity,
    _stratz_enrichment_health,
    resolve_data_paths,
)


class OpenDotaExactIdentityTests(unittest.TestCase):
    identity = VisionDraftIdentity(
        radiant_hero_ids=frozenset(range(1, 6)),
        dire_hero_ids=frozenset(range(6, 11)),
        radiant_team_side="team_one",
    )

    @staticmethod
    def detail() -> dict[str, object]:
        return {
            "leagueid": 19785,
            "radiant_team_id": 101,
            "dire_team_id": 202,
            "players": [
                *(
                    {"player_slot": slot, "hero_id": slot + 1}
                    for slot in range(5)
                ),
                *(
                    {"player_slot": slot, "hero_id": slot - 122}
                    for slot in range(128, 133)
                ),
            ],
        }

    def matches(
        self,
        detail: dict[str, object],
        identity: VisionDraftIdentity | None = None,
    ) -> bool:
        return _opendota_matches_vision_identity(
            detail,
            identity or self.identity,
            team_one_id=101,
            team_two_id=202,
            opendota_league_id=19785,
        )

    def test_accepts_only_the_exact_partitioned_identity(self) -> None:
        self.assertTrue(self.matches(self.detail()))

    def test_rejects_same_ten_heroes_on_swapped_sides(self) -> None:
        detail = self.detail()
        players = detail["players"]
        assert isinstance(players, list)
        for player in players:
            assert isinstance(player, dict)
            hero_id = int(player["hero_id"])
            player["hero_id"] = hero_id + 5 if hero_id <= 5 else hero_id - 5

        self.assertFalse(self.matches(detail))

    def test_requires_exact_player_slot_coverage_and_unique_heroes(self) -> None:
        cases = {
            "missing_slot": {"player_slot": None},
            "string_slot": {"player_slot": "0"},
            "duplicate_slot": {"player_slot": 1},
            "duplicate_hero": {"hero_id": 2},
        }
        for name, replacement in cases.items():
            with self.subTest(name=name):
                detail = self.detail()
                players = detail["players"]
                assert isinstance(players, list)
                player = players[0]
                assert isinstance(player, dict)
                player.update(replacement)
                self.assertFalse(self.matches(detail))

    def test_radiant_team_id_must_agree_with_anchored_team_side(self) -> None:
        detail = self.detail()
        detail["radiant_team_id"] = 202
        detail["dire_team_id"] = 101

        self.assertFalse(self.matches(detail))
        team_two_radiant = VisionDraftIdentity(
            radiant_hero_ids=self.identity.radiant_hero_ids,
            dire_hero_ids=self.identity.dire_hero_ids,
            radiant_team_side="team_two",
        )
        self.assertTrue(self.matches(detail, team_two_radiant))

    def test_requires_the_registered_opendota_league(self) -> None:
        for value in (None, True, "19785", 19786):
            with self.subTest(leagueid=value):
                detail = self.detail()
                detail["leagueid"] = value
                self.assertFalse(self.matches(detail))


class PostmatchEnrichmentTests(unittest.TestCase):
    def test_stratz_http_failure_is_structured_without_fake_artifact(self) -> None:
        class Result:
            @staticmethod
            def fetchone() -> None:
                return None

        class Connection:
            @staticmethod
            def execute(*_args: object, **_kwargs: object) -> Result:
                return Result()

        class Client:
            @staticmethod
            async def get_match(_match_id: int) -> dict[str, object]:
                raise StratzDetailError("STRATZ detail request returned HTTP 403")

        class Archive:
            @staticmethod
            def archive_json(**_kwargs: object) -> None:
                raise AssertionError("failed response must not create a primary artifact")

        result = asyncio.run(
            _archive_optional_stratz_enrichment(
                SimpleNamespace(connection=Connection()),  # type: ignore[arg-type]
                Client(),  # type: ignore[arg-type]
                Archive(),  # type: ignore[arg-type]
                9001,
            )
        )

        self.assertEqual(
            result,
            {
                "match_id": 9001,
                "status": "failed",
                "reason": "stratz_http_403",
                "attempted": True,
            },
        )

    def test_stratz_enrichment_failures_degrade_worker_health_details(self) -> None:
        health = _stratz_enrichment_health(
            [
                {
                    "status": "display_already_synced",
                    "stratz_enrichment": [
                        {
                            "match_id": 9001,
                            "status": "failed",
                            "reason": "stratz_http_403",
                            "attempted": True,
                        },
                        {
                            "match_id": 9002,
                            "status": "available",
                            "reason": "artifact_already_registered",
                            "attempted": False,
                        },
                    ],
                }
            ],
            configured=True,
        )

        self.assertEqual(health["attempted"], 1)
        self.assertEqual(health["available"], 1)
        self.assertEqual(health["failed"], 1)
        self.assertEqual(health["failure_reasons"], ["stratz_http_403"])

    def test_postmatch_archive_uses_configured_data_directory(self) -> None:
        data_dir = Path.cwd() / "configured-data"
        args = argparse.Namespace(archive_root=None)

        resolved = resolve_data_paths(args, {"DATA_DIR": str(data_dir)})

        self.assertEqual(resolved.archive_root, data_dir.resolve() / "raw-sources")

    def test_explicit_postmatch_archive_root_overrides_data_directory(self) -> None:
        archive_root = Path.cwd() / "explicit-raw"
        args = argparse.Namespace(archive_root=archive_root)

        resolved = resolve_data_paths(args, {"DATA_DIR": "ignored"})

        self.assertEqual(resolved.archive_root, archive_root.resolve())


if __name__ == "__main__":
    unittest.main()
