"""Create the core match and hero schema.

Revision ID: 20260730_0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matches",
        sa.Column("match_id", sa.BigInteger(), primary_key=True),
        sa.Column("radiant_team_id", sa.BigInteger()),
        sa.Column("dire_team_id", sa.BigInteger()),
        sa.Column("radiant_win", sa.Boolean()),
        sa.Column("duration", sa.Integer()),
        sa.Column("game_mode", sa.Integer()),
        sa.Column("lobby_type", sa.Integer()),
        sa.Column("start_time", sa.BigInteger()),
        sa.Column("first_blood_time", sa.Integer()),
        sa.Column("leagueid", sa.BigInteger()),
        sa.Column("series_id", sa.BigInteger()),
        sa.Column("series_type", sa.Integer()),
        sa.Column("patch", sa.Integer()),
        sa.Column("region", sa.Integer()),
        sa.Column("radiant_score", sa.Integer()),
        sa.Column("dire_score", sa.Integer()),
        sa.Column("stomp", sa.Integer()),
        sa.Column("comeback", sa.Integer()),
        sa.Column("tower_status_radiant", sa.Integer()),
        sa.Column("tower_status_dire", sa.Integer()),
        sa.Column("barracks_status_radiant", sa.Integer()),
        sa.Column("barracks_status_dire", sa.Integer()),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("idx_matches_radiant_team", "matches", ["radiant_team_id"])
    op.create_index("idx_matches_dire_team", "matches", ["dire_team_id"])
    op.create_index("idx_matches_league", "matches", ["leagueid"])
    op.create_index("idx_matches_start_time", "matches", ["start_time"])
    op.create_index("idx_matches_series", "matches", ["series_id"])

    op.create_table(
        "teams",
        sa.Column("team_id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.Text()),
        sa.Column("tag", sa.Text()),
        sa.Column("logo_url", sa.Text()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_table(
        "leagues",
        sa.Column("leagueid", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.Text()),
        sa.Column("tier", sa.Text()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_table(
        "heroes",
        sa.Column("hero_id", sa.BigInteger(), primary_key=True),
        sa.Column("localized_name", sa.Text()),
        sa.Column("primary_attr", sa.Text()),
        sa.Column("attack_type", sa.Text()),
        sa.Column("roles", sa.Text()),
        sa.Column("hero_key", sa.Text()),
        sa.Column("pro_pick", sa.Integer(), server_default="0"),
        sa.Column("pro_win", sa.Integer(), server_default="0"),
        sa.Column("pro_ban", sa.Integer(), server_default="0"),
        sa.Column("pub_pick", sa.Integer(), server_default="0"),
        sa.Column("pub_win", sa.Integer(), server_default="0"),
        sa.Column("turbo_picks", sa.Integer(), server_default="0"),
        sa.Column("turbo_wins", sa.Integer(), server_default="0"),
        sa.Column("win_rate", sa.Double(), server_default="0"),
    )

    op.create_table(
        "match_players",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("match_id", sa.BigInteger(), sa.ForeignKey("matches.match_id")),
        sa.Column("account_id", sa.BigInteger()),
        sa.Column("player_slot", sa.Integer()),
        sa.Column("hero_id", sa.BigInteger(), sa.ForeignKey("heroes.hero_id")),
        sa.Column("is_radiant", sa.Boolean()),
        sa.Column("team_id", sa.BigInteger()),
        sa.Column("kills", sa.Integer()),
        sa.Column("deaths", sa.Integer()),
        sa.Column("assists", sa.Integer()),
        sa.Column("gold_per_min", sa.Integer()),
        sa.Column("xp_per_min", sa.Integer()),
        sa.Column("net_worth", sa.Integer()),
        sa.Column("last_hits", sa.Integer()),
        sa.Column("denies", sa.Integer()),
        sa.Column("hero_damage", sa.Integer()),
        sa.Column("hero_healing", sa.Integer()),
        sa.Column("tower_damage", sa.Integer()),
        sa.Column("level", sa.Integer()),
        sa.Column("item_0", sa.Integer()),
        sa.Column("item_1", sa.Integer()),
        sa.Column("item_2", sa.Integer()),
        sa.Column("item_3", sa.Integer()),
        sa.Column("item_4", sa.Integer()),
        sa.Column("item_5", sa.Integer()),
        sa.Column("backpack_0", sa.Integer()),
        sa.Column("backpack_1", sa.Integer()),
        sa.Column("backpack_2", sa.Integer()),
        sa.Column("item_neutral", sa.Integer()),
        sa.Column("firstblood_claimed", sa.Integer()),
        sa.Column("gold_10min", sa.Integer()),
        sa.Column("lh_10min", sa.Integer()),
        sa.Column("xp_10min", sa.Integer()),
        sa.Column("kills_10min", sa.Integer()),
        sa.Column("deaths_10min", sa.Integer()),
        sa.Column("assists_10min", sa.Integer()),
        sa.Column("obs_placed_10min", sa.Integer()),
        sa.Column("sen_placed_10min", sa.Integer()),
        sa.Column("lane_efficiency", sa.Double()),
        sa.Column("lane_role", sa.Integer()),
        sa.Column("is_roaming", sa.Boolean()),
        sa.Column("kda", sa.Double()),
        sa.Column("observer_kills_10min", sa.Integer()),
        sa.Column("sentry_kills_10min", sa.Integer()),
    )
    op.create_index("idx_match_players_match", "match_players", ["match_id"])
    op.create_index("idx_match_players_hero", "match_players", ["hero_id"])
    op.create_index("idx_match_players_team", "match_players", ["team_id"])
    op.create_index("idx_match_players_account", "match_players", ["account_id"])

    op.create_table(
        "picks_bans",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("match_id", sa.BigInteger(), sa.ForeignKey("matches.match_id")),
        sa.Column("hero_id", sa.BigInteger(), sa.ForeignKey("heroes.hero_id")),
        sa.Column("is_pick", sa.Boolean()),
        sa.Column("team", sa.Integer()),
        sa.Column("ord", sa.Integer()),
    )
    op.create_index("idx_picks_bans_match", "picks_bans", ["match_id"])

    op.create_table(
        "gold_advantage",
        sa.Column("match_id", sa.BigInteger(), sa.ForeignKey("matches.match_id")),
        sa.Column("time_min", sa.Integer()),
        sa.Column("value", sa.Integer()),
    )
    op.create_index("idx_gold_adv_match", "gold_advantage", ["match_id"])
    op.create_table(
        "xp_advantage",
        sa.Column("match_id", sa.BigInteger(), sa.ForeignKey("matches.match_id")),
        sa.Column("time_min", sa.Integer()),
        sa.Column("value", sa.Integer()),
    )
    op.create_index("idx_xp_adv_match", "xp_advantage", ["match_id"])

    op.create_table(
        "teamfights",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("match_id", sa.BigInteger(), sa.ForeignKey("matches.match_id")),
        sa.Column("start_time", sa.Integer()),
        sa.Column("end_time", sa.Integer()),
        sa.Column("last_death", sa.Integer()),
        sa.Column("deaths", sa.Integer()),
    )
    op.create_index("idx_teamfights_match", "teamfights", ["match_id"])
    op.create_table(
        "teamfight_players",
        sa.Column("teamfight_id", sa.BigInteger(), sa.ForeignKey("teamfights.id")),
        sa.Column("player_slot", sa.Integer()),
        sa.Column("deaths", sa.Integer()),
        sa.Column("buybacks", sa.Integer()),
        sa.Column("damage", sa.Integer()),
        sa.Column("healing", sa.Integer()),
        sa.Column("gold_delta", sa.Integer()),
        sa.Column("xp_delta", sa.Integer()),
        sa.Column("kills", sa.Integer()),
    )
    op.create_index("idx_tf_players_tf", "teamfight_players", ["teamfight_id"])

    op.create_table(
        "objectives",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("match_id", sa.BigInteger(), sa.ForeignKey("matches.match_id")),
        sa.Column("time", sa.Integer()),
        sa.Column("type", sa.Text()),
        sa.Column("unit", sa.Text()),
        sa.Column("key", sa.Text()),
        sa.Column("player_slot", sa.Integer()),
    )
    op.create_index("idx_objectives_match", "objectives", ["match_id"])
    op.create_table(
        "chat",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("match_id", sa.BigInteger(), sa.ForeignKey("matches.match_id")),
        sa.Column("time", sa.Integer()),
        sa.Column("player_slot", sa.Integer()),
        sa.Column("type", sa.Text()),
        sa.Column("message", sa.Text()),
    )

    op.create_table(
        "hero_matchups",
        sa.Column("hero_id", sa.BigInteger(), sa.ForeignKey("heroes.hero_id"), primary_key=True),
        sa.Column("vs_hero_id", sa.BigInteger(), sa.ForeignKey("heroes.hero_id"), primary_key=True),
        sa.Column("games_played", sa.Integer()),
        sa.Column("wins", sa.Integer()),
        sa.Column("synergy", sa.Double()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("idx_hero_matchups_hero", "hero_matchups", ["hero_id"])
    op.create_table(
        "hero_duration_stats",
        sa.Column("hero_id", sa.BigInteger(), sa.ForeignKey("heroes.hero_id"), primary_key=True),
        sa.Column("duration_min", sa.Integer(), primary_key=True),
        sa.Column("games_played", sa.Integer()),
        sa.Column("wins", sa.Integer()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("idx_hero_duration_hero", "hero_duration_stats", ["hero_id"])
    op.create_table(
        "hero_benchmarks",
        sa.Column("hero_id", sa.BigInteger(), sa.ForeignKey("heroes.hero_id"), primary_key=True),
        sa.Column("metric", sa.Text(), primary_key=True),
        sa.Column("p50", sa.Double()),
        sa.Column("p75", sa.Double()),
        sa.Column("p90", sa.Double()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("idx_hero_benchmarks_hero", "hero_benchmarks", ["hero_id"])


def downgrade() -> None:
    op.drop_table("hero_benchmarks")
    op.drop_table("hero_duration_stats")
    op.drop_table("hero_matchups")
    op.drop_table("chat")
    op.drop_table("objectives")
    op.drop_table("teamfight_players")
    op.drop_table("teamfights")
    op.drop_table("xp_advantage")
    op.drop_table("gold_advantage")
    op.drop_table("picks_bans")
    op.drop_table("match_players")
    op.drop_table("heroes")
    op.drop_table("leagues")
    op.drop_table("teams")
    op.drop_table("matches")
