"""Add Alembic-managed feature cache tables.

Revision ID: 20260730_0016
Revises: 20260730_0015
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0016"
down_revision: str | None = "20260730_0015"
branch_labels: str | None = None
depends_on: str | None = None


_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "match_feature_cache": (
        ("match_id", "bigint"),
        ("duration", "integer"),
        ("radiant_win", "boolean"),
        ("first_blood_radiant", "boolean"),
        ("first_blood_time", "integer"),
        ("radiant_gold_adv_10min", "integer"),
        ("radiant_xp_adv_10min", "integer"),
        ("radiant_gold_adv_max", "integer"),
        ("radiant_gold_adv_min", "integer"),
        ("radiant_gold_adv_mean", "float"),
        ("gold_adv_swings", "integer"),
        ("radiant_tower_kills", "integer"),
        ("dire_tower_kills", "integer"),
        ("radiant_barracks_kills", "integer"),
        ("dire_barracks_kills", "integer"),
        ("radiant_first_tower_time", "integer"),
        ("dire_first_tower_time", "integer"),
        ("teamfight_count", "integer"),
        ("radiant_teamfight_wins", "integer"),
        ("radiant_tf_kd_ratio", "float"),
        ("stomp_value", "integer"),
        ("comeback_value", "integer"),
        ("radiant_score", "integer"),
        ("dire_score", "integer"),
        ("patch", "integer"),
        ("radiant_team_id", "integer"),
        ("dire_team_id", "integer"),
        ("league_id", "integer"),
        ("series_id", "integer"),
        ("h2h_radiant_win_rate", "float"),
        ("h2h_match_count", "integer"),
    ),
    "team_feature_cache": (
        ("match_id", "bigint"),
        ("is_radiant", "boolean"),
        ("team_id", "integer"),
        ("total_kills", "integer"),
        ("total_deaths", "integer"),
        ("total_assists", "integer"),
        ("avg_gpm", "float"),
        ("avg_xpm", "float"),
        ("total_net_worth", "integer"),
        ("total_last_hits", "integer"),
        ("total_denies", "integer"),
        ("gpm_std", "float"),
        ("max_net_worth", "integer"),
        ("total_hero_damage", "integer"),
        ("first_blood", "boolean"),
        ("team_win_rate_10", "float"),
        ("team_avg_gpm_10", "float"),
        ("team_avg_xpm_10", "float"),
        ("team_net_worth_lead_10min_10", "float"),
        ("team_win_rate_20", "float"),
        ("team_avg_gpm_20", "float"),
        ("team_avg_xpm_20", "float"),
        ("team_net_worth_lead_10min_20", "float"),
        ("team_win_rate_50", "float"),
        ("team_avg_gpm_50", "float"),
        ("team_avg_xpm_50", "float"),
        ("team_net_worth_lead_10min_50", "float"),
    ),
    "hero_feature_cache": (
        ("match_id", "bigint"),
        ("hero_id", "integer"),
        ("player_slot", "integer"),
        ("is_radiant", "boolean"),
        ("team_id", "integer"),
        ("kills", "integer"),
        ("deaths", "integer"),
        ("assists", "integer"),
        ("gpm", "integer"),
        ("xpm", "integer"),
        ("net_worth", "integer"),
        ("last_hits", "integer"),
        ("denies", "integer"),
        ("hero_damage", "integer"),
        ("hero_healing", "integer"),
        ("tower_damage", "integer"),
        ("level", "integer"),
        ("role", "integer"),
        ("hero_win_rate_patch", "float"),
        ("hero_avg_gpm_patch", "float"),
        ("hero_pick_rate_patch", "float"),
        ("hero_ban_rate_patch", "float"),
    ),
    "draft_feature_cache": (
        ("match_id", "bigint"),
        ("order", "integer"),
        ("is_pick", "boolean"),
        ("hero_id", "integer"),
        ("team", "integer"),
        ("phase", "text"),
    ),
}

_PRIMARY_KEYS = {
    "match_feature_cache": ("match_id",),
    "team_feature_cache": ("match_id", "is_radiant"),
    "hero_feature_cache": ("match_id", "player_slot"),
    "draft_feature_cache": ("match_id", "order"),
}

_TYPES = {
    "bigint": sa.BigInteger,
    "integer": sa.Integer,
    "float": sa.Float,
    "boolean": sa.Boolean,
    "text": sa.Text,
}


def upgrade() -> None:
    for table_name, columns in _COLUMNS.items():
        primary_key = _PRIMARY_KEYS[table_name]
        definitions: list[sa.SchemaItem] = []
        for name, type_name in columns:
            arguments: list[sa.SchemaItem] = []
            if name == "match_id":
                arguments.append(
                    sa.ForeignKey("matches.match_id", ondelete="CASCADE")
                )
            definitions.append(
                sa.Column(
                    name,
                    _TYPES[type_name](),
                    *arguments,
                    nullable=name not in primary_key,
                )
            )
        definitions.append(sa.PrimaryKeyConstraint(*primary_key))
        op.create_table(table_name, *definitions)


def downgrade() -> None:
    for table_name in reversed(tuple(_COLUMNS)):
        op.drop_table(table_name)
