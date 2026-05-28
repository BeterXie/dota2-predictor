"""Pre-match feature names — subset of the full 183 features that are available
before a match starts.

Excludes all in-game stats (KDA, GPM from the match itself, tower kills,
teamfights, etc.) and hero role stats (which depend on post-match GPM).
Only features that can be computed from historical data or external APIs
are included, preventing data leakage from the match being predicted.
"""

PREMATCH_FEATURES = frozenset({
    # ---- Identity (5) ----
    "patch",
    "radiant_team_id",
    "dire_team_id",
    "league_id",
    "series_id",

    # ---- H2H (2) ----
    "h2h_radiant_win_rate",
    "h2h_match_count",

    # ---- Radiant team rolling stats from historical matches (12) ----
    "radiant_team_win_rate_10",
    "radiant_team_avg_gpm_10",
    "radiant_team_avg_xpm_10",
    "radiant_team_net_worth_lead_10min_10",
    "radiant_team_win_rate_20",
    "radiant_team_avg_gpm_20",
    "radiant_team_avg_xpm_20",
    "radiant_team_net_worth_lead_10min_20",
    "radiant_team_win_rate_50",
    "radiant_team_avg_gpm_50",
    "radiant_team_avg_xpm_50",
    "radiant_team_net_worth_lead_10min_50",

    # ---- Dire team rolling stats from historical matches (12) ----
    "dire_team_win_rate_10",
    "dire_team_avg_gpm_10",
    "dire_team_avg_xpm_10",
    "dire_team_net_worth_lead_10min_10",
    "dire_team_win_rate_20",
    "dire_team_avg_gpm_20",
    "dire_team_avg_xpm_20",
    "dire_team_net_worth_lead_10min_20",
    "dire_team_win_rate_50",
    "dire_team_avg_gpm_50",
    "dire_team_avg_xpm_50",
    "dire_team_net_worth_lead_10min_50",

    # ---- Diff: team rolling stats (12) ----
    "diff_team_win_rate_10",
    "diff_team_avg_gpm_10",
    "diff_team_avg_xpm_10",
    "diff_team_net_worth_lead_10min_10",
    "diff_team_win_rate_20",
    "diff_team_avg_gpm_20",
    "diff_team_avg_xpm_20",
    "diff_team_net_worth_lead_10min_20",
    "diff_team_win_rate_50",
    "diff_team_avg_gpm_50",
    "diff_team_avg_xpm_50",
    "diff_team_net_worth_lead_10min_50",

    # ---- Hero patch stats (12) ----
    "radiant_avg_hero_win_rate_patch",
    "dire_avg_hero_win_rate_patch",
    "diff_avg_hero_win_rate_patch",
    "radiant_avg_hero_avg_gpm_patch",
    "dire_avg_hero_avg_gpm_patch",
    "diff_avg_hero_avg_gpm_patch",
    "radiant_avg_hero_pick_rate_patch",
    "dire_avg_hero_pick_rate_patch",
    "diff_avg_hero_pick_rate_patch",
    "radiant_avg_hero_ban_rate_patch",
    "dire_avg_hero_ban_rate_patch",
    "diff_avg_hero_ban_rate_patch",

    # ---- Hero counter features (12) ----
    "radiant_avg_hero_advantage",
    "dire_avg_hero_advantage",
    "diff_avg_hero_advantage",
    "radiant_min_hero_advantage",
    "radiant_max_hero_advantage",
    "dire_min_hero_advantage",
    "dire_max_hero_advantage",
    "diff_min_hero_advantage",
    "diff_max_hero_advantage",
    "radiant_hero_advantage_std",
    "dire_hero_advantage_std",
    "diff_hero_advantage_std",
})
