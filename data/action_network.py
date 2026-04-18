"""
Action Network unofficial API client — no API key required.

Provides multi-book player prop lines (over/under with odds) for NBA games,
used as a drop-in replacement for The Odds API when credits run out.

Coverage: DraftKings, FanDuel, BetMGM, Caesars, and others depending on game.
"""

import json
import time
from pathlib import Path

import requests

AN_BASE = "https://api.actionnetwork.com/web/v2"
AN_V1   = "https://api.actionnetwork.com/web/v1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json",
    "Referer": "https://www.actionnetwork.com/",
}

# Cache directory (shared with odds.py cache)
_CACHE_DIR = Path("logs/.odds_cache")
_CACHE_TTL = 7200  # 2 hours — matches odds.py

# Map AN prop type keys → our internal stat names (same as Odds API)
AN_PROP_MAP = {
    "core_bet_type_27_points":   "player_points",
    "core_bet_type_23_rebounds": "player_rebounds",
    "core_bet_type_26_assists":  "player_assists",
    "core_bet_type_21_3fgm":     "player_threes",
    "core_bet_type_580_turnovers": "player_turnovers",
}


def _cache_get(key: str):
    path = _CACHE_DIR / f"{key}.json"
    if path.exists():
        data = json.loads(path.read_text())
        if time.time() - data["ts"] < _CACHE_TTL:
            return data["payload"]
    return None


def _cache_set(key: str, payload):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.json").write_text(
        json.dumps({"ts": time.time(), "payload": payload})
    )


def get_events() -> list[dict]:
    """
    Return today's NBA games from Action Network.
    Each dict has: id, away_team, home_team, start_time.
    """
    cached = _cache_get("an_events_nba")
    if cached is not None:
        return cached

    resp = requests.get(f"{AN_V1}/scoreboard/nba", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    # Build team_id → name lookup from game data
    team_lookup = {}
    events = []
    for game in data.get("games", []):
        for team in game.get("teams", []):
            team_lookup[team["id"]] = team["full_name"]

        away_name = team_lookup.get(game["away_team_id"], "")
        home_name = team_lookup.get(game["home_team_id"], "")
        if not away_name or not home_name:
            continue

        events.append({
            "id":        game["id"],
            "away_team": away_name,
            "home_team": home_name,
            "start_time": game.get("start_time", ""),
            "status":    game.get("status", ""),
        })

    _cache_set("an_events_nba", events)
    return events


def get_player_props(game_id: int) -> dict:
    """
    Fetch multi-book player prop lines for a game.

    Returns same structure as odds.py's get_player_props():
        {
          "bookmakers": [
            {
              "title": "DraftKings",
              "markets": [
                {
                  "key": "player_points",
                  "outcomes": [
                    {"description": "LeBron James", "name": "Over",
                     "price": -115, "point": 24.5},
                    {"description": "LeBron James", "name": "Under",
                     "price": -105, "point": 24.5},
                  ]
                }
              ]
            }
          ]
        }
    """
    cache_key = f"an_props_{game_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    resp = requests.get(
        f"{AN_BASE}/games/{game_id}/props",
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    raw = resp.json()

    # Build player_id → full_name lookup
    player_names = {
        str(pid): info["full_name"]
        for pid, info in raw.get("players", {}).items()
    }

    # Collect lines: book_id → stat → [(player, side, line, odds)]
    # AN book IDs: 15=DraftKings, 30=FanDuel, 76=BetMGM, 75=Caesars, 123=BetRivers
    book_names = {
        "15":  "DraftKings",
        "30":  "FanDuel",
        "76":  "BetMGM",
        "75":  "Caesars",
        "123": "BetRivers",
        "69":  "PointsBet",
        "68":  "Barstool",
        "79":  "Unibet",
        "247": "BetUS",
        "71":  "Bovada",
    }

    # Intermediate structure: {book_id: {stat_key: {(player_name, line): {Over/Under: odds}}}}
    by_book = {}

    for prop_type, outcomes in raw.get("player_props", {}).items():
        stat_key = AN_PROP_MAP.get(prop_type)
        if not stat_key:
            continue

        for outcome in outcomes:
            pid = str(outcome.get("player_id", ""))
            player_name = player_names.get(pid)
            if not player_name:
                continue

            for book_id_str, book_lines in outcome.get("lines", {}).items():
                if book_id_str not in book_names:
                    continue
                for bl in book_lines:
                    side_raw = bl.get("side", "").lower()
                    side = "Over" if side_raw == "over" else ("Under" if side_raw == "under" else None)
                    if not side:
                        continue
                    line_val = bl.get("value")
                    odds_val = bl.get("odds")
                    if line_val is None or odds_val is None:
                        continue

                    if book_id_str not in by_book:
                        by_book[book_id_str] = {}
                    if stat_key not in by_book[book_id_str]:
                        by_book[book_id_str][stat_key] = {}

                    key = (player_name, float(line_val))
                    if key not in by_book[book_id_str][stat_key]:
                        by_book[book_id_str][stat_key][key] = {}
                    by_book[book_id_str][stat_key][key][side] = int(odds_val)

    # Convert to Odds API compatible format
    bookmakers = []
    for book_id_str, stats in by_book.items():
        markets = []
        for stat_key, pairs in stats.items():
            outcomes_list = []
            for (player_name, line_val), sides in pairs.items():
                for side, price in sides.items():
                    outcomes_list.append({
                        "description": player_name,
                        "name":        side,
                        "price":       price,
                        "point":       line_val,
                    })
            if outcomes_list:
                markets.append({"key": stat_key, "outcomes": outcomes_list})

        if markets:
            bookmakers.append({
                "title":   book_names[book_id_str],
                "markets": markets,
            })

    result = {"bookmakers": bookmakers}
    _cache_set(cache_key, result)
    return result
