"""
Hero role inference based on farm priority within a match.

For each team in a match, players are sorted by gold_per_min
descending and assigned positions 1 (highest GPM) to 5 (lowest GPM).
"""

import pandas as pd


def infer_roles(players_df: pd.DataFrame) -> pd.Series:
    """Assign position 1-5 to each player based on farm priority within their team.

    Within each (match_id, is_radiant) group, the player with highest
    gold_per_min gets role=1 (carry), lowest gets role=5 (hard support).

    Returns a Series with the same index as players_df.
    """
    roles = pd.Series(0, index=players_df.index, dtype="int32")

    for (match_id, is_radiant), idx in players_df.groupby(
        ["match_id", "is_radiant"]
    ).groups.items():
        group_gpm = players_df.loc[idx, "gold_per_min"]
        # Rank descending: highest GPM -> 1, lowest -> 5
        ranked = group_gpm.rank(method="first", ascending=False).astype("int32")
        roles.loc[idx] = ranked

    return roles
