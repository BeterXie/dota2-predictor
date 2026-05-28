"""Parse OpenDota API JSON responses into flat dicts for database insert."""

from typing import Any


def parse_match_basic(match: dict) -> dict:
    return {
        "match_id": match["match_id"],
        "radiant_team_id": match.get("radiant_team_id"),
        "dire_team_id": match.get("dire_team_id"),
        "radiant_win": match.get("radiant_win"),
        "duration": match.get("duration"),
        "game_mode": match.get("game_mode"),
        "lobby_type": match.get("lobby_type"),
        "start_time": match.get("start_time"),
        "first_blood_time": match.get("first_blood_time"),
        "leagueid": match.get("leagueid"),
        "series_id": match.get("series_id"),
        "series_type": match.get("series_type"),
        "patch": match.get("patch"),
        "region": match.get("region"),
        "radiant_score": match.get("radiant_score"),
        "dire_score": match.get("dire_score"),
        "stomp": match.get("stomp", 0),
        "comeback": match.get("comeback", 0),
        "tower_status_radiant": match.get("tower_status_radiant"),
        "tower_status_dire": match.get("tower_status_dire"),
        "barracks_status_radiant": match.get("barracks_status_radiant"),
        "barracks_status_dire": match.get("barracks_status_dire"),
    }


def parse_players(match: dict) -> list[dict]:
    radiant_team_id = match.get("radiant_team_id")
    dire_team_id = match.get("dire_team_id")

    rows = []
    for p in match.get("players") or []:
        slot = p.get("player_slot", 0)
        is_radiant = slot < 128

        team_id = None
        if is_radiant:
            team_id = p.get("team_id", radiant_team_id)
        else:
            team_id = p.get("team_id", dire_team_id)

        rows.append({
            "match_id": match["match_id"],
            "account_id": p.get("account_id"),
            "player_slot": slot,
            "hero_id": p.get("hero_id"),
            "is_radiant": is_radiant,
            "team_id": team_id,
            "kills": p.get("kills", 0),
            "deaths": p.get("deaths", 0),
            "assists": p.get("assists", 0),
            "gold_per_min": p.get("gold_per_min"),
            "xp_per_min": p.get("xp_per_min"),
            "net_worth": p.get("net_worth"),
            "last_hits": p.get("last_hits"),
            "denies": p.get("denies"),
            "hero_damage": p.get("hero_damage"),
            "hero_healing": p.get("hero_healing"),
            "tower_damage": p.get("tower_damage"),
            "level": p.get("level"),
            "item_0": p.get("item_0"),
            "item_1": p.get("item_1"),
            "item_2": p.get("item_2"),
            "item_3": p.get("item_3"),
            "item_4": p.get("item_4"),
            "item_5": p.get("item_5"),
            "backpack_0": p.get("backpack_0"),
            "backpack_1": p.get("backpack_1"),
            "backpack_2": p.get("backpack_2"),
            "item_neutral": p.get("item_neutral"),
        })
    return rows


def parse_picks_bans(match: dict) -> list[dict]:
    rows = []
    for pb in match.get("picks_bans") or []:
        rows.append({
            "match_id": match["match_id"],
            "hero_id": pb.get("hero_id"),
            "is_pick": pb.get("is_pick"),
            "team": pb.get("team"),
            "ord": pb.get("order"),
        })
    return rows


def parse_teamfights(match: dict) -> tuple[list[dict], list[dict]]:
    """Return (teamfights, teamfight_players) lists."""
    tfs = []
    tf_players = []
    # teamfights are indexed in order; we use that as a local id for linking
    for idx, tf in enumerate(match.get("teamfights") or []):
        tfs.append({
            "tf_local_idx": idx,
            "match_id": match["match_id"],
            "start_time": tf.get("start"),
            "end_time": tf.get("end"),
            "last_death": tf.get("last_death"),
            "deaths": tf.get("deaths"),
        })
        for p in tf.get("players") or []:
            tf_players.append({
                "tf_local_idx": idx,
                "player_slot": p.get("player_slot") if "player_slot" in p else p.get("slot"),
                "deaths": p.get("deaths", 0),
                "buybacks": p.get("buybacks", 0),
                "damage": p.get("damage", 0),
                "healing": p.get("healing", 0),
                "gold_delta": p.get("gold_delta", 0),
                "xp_delta": p.get("xp_delta", 0),
                "kills": p.get("kills", 0),
            })
    return tfs, tf_players


def parse_objectives(match: dict) -> list[dict]:
    rows = []
    for obj in match.get("objectives") or []:
        rows.append({
            "match_id": match["match_id"],
            "time": obj.get("time"),
            "type": obj.get("type"),
            "unit": obj.get("unit"),
            "key": str(obj.get("key", "")) if obj.get("key") else None,
            "player_slot": obj.get("player_slot") if "player_slot" in obj else obj.get("slot"),
        })
    return rows


def parse_gold_adv(match: dict) -> list[dict]:
    adv = match.get("radiant_gold_adv")
    if not adv:
        return []
    return [
        {"match_id": match["match_id"], "time_min": i, "value": v}
        for i, v in enumerate(adv) if v is not None
    ]


def parse_xp_adv(match: dict) -> list[dict]:
    adv = match.get("radiant_xp_adv")
    if not adv:
        return []
    return [
        {"match_id": match["match_id"], "time_min": i, "value": v}
        for i, v in enumerate(adv) if v is not None
    ]


def parse_team_info(match: dict, side: str) -> dict | None:
    """Extract team metadata from the match JSON for a given side (radiant/dire)."""
    team = match.get(f"{side}_team")
    if not team or not team.get("team_id"):
        return None
    return {
        "team_id": team["team_id"],
        "name": team.get("name"),
        "tag": team.get("tag"),
        "logo_url": team.get("logo_url"),
    }


def parse_league_info(match: dict) -> dict | None:
    """Extract league metadata from the match JSON."""
    league = match.get("league")
    if not league or not league.get("leagueid"):
        return None
    return {
        "leagueid": league["leagueid"],
        "name": league.get("name"),
        "tier": str(league.get("tier")) if league.get("tier") is not None else None,
    }


def parse_chat(match: dict) -> list[dict]:
    rows = []
    for msg in match.get("chat") or []:
        rows.append({
            "match_id": match["match_id"],
            "time": msg.get("time"),
            "player_slot": msg.get("player_slot") if "player_slot" in msg else msg.get("slot"),
            "type": msg.get("type"),
            "message": msg.get("key"),
        })
    return rows
