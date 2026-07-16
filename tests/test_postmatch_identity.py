from __future__ import annotations

import unittest

from live_betting.postmatch_monitor import (
    VisionDraftIdentity,
    _opendota_matches_vision_identity,
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


if __name__ == "__main__":
    unittest.main()
