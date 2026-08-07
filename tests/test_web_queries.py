from __future__ import annotations

from unittest.mock import patch

from web.queries import get_hero_grid


def test_hero_grid_groups_current_picker_catalog() -> None:
    rows = [
        {
            "hero_id": 1,
            "localized_name": "Strength Hero",
            "primary_attr": "str",
            "hero_key": "strength_hero",
        },
        {
            "hero_id": 2,
            "localized_name": "Universal Hero",
            "primary_attr": "all",
            "hero_key": "universal_hero",
        },
    ]

    with patch("web.queries._safe_execute", return_value=rows):
        grid = get_hero_grid()

    assert [hero["hero_id"] for hero in grid["str"]] == [1]
    assert [hero["hero_id"] for hero in grid["all"]] == [2]
    assert grid["str"][0]["image_url"].endswith("/strength_hero.png")
    assert grid["agi"] == []
    assert grid["int"] == []
