"""
PrizePicks unofficial API client.

Fetches today's NBA player prop projections.
Only returns 'standard' lines (not goblin/demon, which have adjusted payouts).
"""

import requests
from datetime import datetime, timezone, timedelta

PP_BASE = "https://partner-api.prizepicks.com/projections"

# Map PrizePicks stat names -> Odds API market keys
STAT_MAP = {
    "Points": "player_points",
    "Rebounds": "player_rebounds",
    "Assists": "player_assists",
    "3-PT Made": "player_threes",
    "Turnovers": "player_turnovers",
}

HEADERS = {
    # Mobile UA bypasses PerimeterX bot protection on the partner API endpoint
    "User-Agent": "PrizePicks/2.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
    "Accept": "application/json",
}


def get_nba_projections(tonight_only: bool = True) -> list[dict]:
    """
    Fetch NBA single-stat standard projections from PrizePicks.
    By default only returns games starting within the next 12 hours (tonight_only=True).
    Returns list of dicts with keys: player, stat_type, odds_stat, line, game_id, start_time.
    """
    params = {
        "league_id": 7,       # NBA
        "per_page": 500,
        "single_stat": "true",
    }
    resp = requests.get(PP_BASE, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    # Build player name lookup from included
    players = {}
    for item in data.get("included", []):
        if item["type"] == "new_player":
            players[item["id"]] = item["attributes"].get("name", "")

    projections = []
    for proj in data.get("data", []):
        attrs = proj.get("attributes", {})

        # partner-api returns projection_type field — skip non-single-stat lines
        proj_type = attrs.get("projection_type", "Single Stat")
        if proj_type and proj_type != "Single Stat":
            continue

        # Only pre-game, standard lines (scanner_prizepicks handles goblin/demon separately)
        if attrs.get("status") != "pre_game":
            continue
        if attrs.get("odds_type") != "standard":
            continue

        # Filter to tonight only (games starting within the next 12 hours)
        if tonight_only:
            start_str = attrs.get("start_time", "")
            try:
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                # 20-hour window captures afternoon/evening games from early-morning scans
                if start > now + timedelta(hours=20) or start < now - timedelta(hours=3):
                    continue
            except (ValueError, TypeError):
                continue

        stat_type = attrs.get("stat_type", "")
        odds_stat = STAT_MAP.get(stat_type)
        if not odds_stat:
            continue  # combo stat or unsupported

        player_id = proj["relationships"]["new_player"]["data"]["id"]
        player_name = players.get(player_id, "")

        # Skip combo players (e.g. "LeBron James + Anthony Davis")
        if "+" in player_name:
            continue

        try:
            line = float(attrs["line_score"])
        except (ValueError, KeyError):
            continue

        projections.append({
            "player": player_name,
            "stat_type": stat_type,
            "odds_stat": odds_stat,
            "line": line,
            "game_id": attrs.get("game_id", ""),
            "start_time": attrs.get("start_time", ""),
        })

    return projections
