"""
PrizePicks multi-sport projection fetcher.

Supports any PP league by ID. Returns same projection shape as
data/prizepicks.py but parameterized on league_id and without
stat-map filtering (all stat types returned).
"""

import time
from datetime import datetime, timezone, timedelta

import requests

PP_BASE = "https://partner-api.prizepicks.com/projections"

PP_HEADERS = {
    "User-Agent": "PrizePicks/2.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
    "Accept": "application/json",
}


def get_projections(league_id: int, tonight_only: bool = True) -> list[dict]:
    """
    Fetch projections for any PP league.

    Returns list of dicts with keys:
      player, team, stat_type, line, game_id, start_time, league
    Only standard odds_type, pre_game status, single_stat projections.
    """
    params = {
        "league_id": league_id,
        "per_page": 500,
        "single_stat": "true",
    }

    for attempt in range(4):
        try:
            resp = requests.get(PP_BASE, headers=PP_HEADERS, params=params, timeout=15)
            if resp.status_code == 429:
                wait = 20 * (attempt + 1)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.exceptions.HTTPError:
            if attempt < 3:
                time.sleep(15)
                continue
            return []
        except Exception:
            return []
    else:
        return []

    # Build player name lookup
    players = {}
    for item in data.get("included", []):
        if item.get("type") == "new_player":
            attrs = item.get("attributes", {})
            players[item["id"]] = {
                "name": attrs.get("name", ""),
                "team": attrs.get("team", ""),
            }

    projections = []
    now = datetime.now(timezone.utc)

    for proj in data.get("data", []):
        if proj.get("type") != "projection":
            continue
        attrs = proj.get("attributes", {})

        proj_type = attrs.get("projection_type", "Single Stat")
        if proj_type and proj_type != "Single Stat":
            continue
        if attrs.get("status") != "pre_game":
            continue
        if attrs.get("odds_type") != "standard":
            continue

        if tonight_only:
            start_str = attrs.get("start_time", "")
            try:
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if start > now + timedelta(hours=12) or start < now - timedelta(hours=1):
                    continue
            except (ValueError, TypeError):
                continue

        try:
            line = float(attrs["line_score"])
        except (ValueError, KeyError):
            continue

        start_str = attrs.get("start_time", "")
        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            start = None

        player_id   = proj.get("relationships", {}).get("new_player", {}).get("data", {}).get("id", "")
        player_info = players.get(player_id, {})
        player_name = player_info.get("name", "")

        if not player_name or "+" in player_name:
            continue

        projections.append({
            "player":     player_name,
            "team":       player_info.get("team", ""),
            "stat_type":  attrs.get("stat_type", ""),
            "line":       line,
            "game_id":    attrs.get("game_id", ""),
            "start_time": start,
            "league":     league_id,
        })

    return projections
