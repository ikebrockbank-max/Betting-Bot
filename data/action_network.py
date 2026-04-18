"""
Action Network unofficial API client — no API key required.

Provides multi-book player prop lines for NBA, MLB, NHL, NFL, WNBA.
Books covered: DraftKings, FanDuel, BetMGM, Caesars, BetRivers, PointsBet.

No API key needed — completely free.
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

_CACHE_DIR = Path("logs/.odds_cache")
_CACHE_TTL = 7200  # 2 hours

# Supported sports: AN scoreboard key → PP league IDs it covers
SPORT_CONFIG = {
    "nba":  {"league_ids": {7, 84, 192, 237, 250}, "endpoint": "nba"},
    "mlb":  {"league_ids": {2},                     "endpoint": "mlb"},
    "nhl":  {"league_ids": {8, 227, 231},            "endpoint": "nhl"},
    "nfl":  {"league_ids": {9},                      "endpoint": "nfl"},
    "wnba": {"league_ids": {3, 252},                 "endpoint": "wnba"},
}

# PP league_id → AN sport key (reverse lookup)
LEAGUE_TO_SPORT = {
    lid: sport
    for sport, cfg in SPORT_CONFIG.items()
    for lid in cfg["league_ids"]
}

# Action Network prop type keys → standard market key
# Core IDs are consistent across sports; sport-specific ones added per sport
AN_PROP_MAP = {
    # Basketball (NBA / WNBA)
    "core_bet_type_27_points":          "player_points",
    "core_bet_type_23_rebounds":        "player_rebounds",
    "core_bet_type_26_assists":         "player_assists",
    "core_bet_type_21_3fgm":            "player_threes",
    "core_bet_type_580_turnovers":      "player_turnovers",
    "core_bet_type_569_blocks":         "player_blocks",
    "core_bet_type_570_steals":         "player_steals",
    "core_bet_type_571_pra":            "player_points_rebounds_assists",
    "core_bet_type_572_pr":             "player_points_rebounds",
    "core_bet_type_573_pa":             "player_points_assists",
    "core_bet_type_574_ra":             "player_rebounds_assists",
    # Baseball (MLB)
    "core_bet_type_97_strikeouts":      "pitcher_strikeouts",
    "core_bet_type_98_hits":            "batter_hits",
    "core_bet_type_99_total_bases":     "batter_total_bases",
    "core_bet_type_100_runs":           "batter_runs_scored",
    "core_bet_type_101_rbis":           "batter_rbis",
    "core_bet_type_102_home_runs":      "batter_home_runs",
    # Hockey (NHL)
    "core_bet_type_62_shots":           "player_shots_on_goal",
    "core_bet_type_63_points":          "player_points",
    "core_bet_type_64_goals":           "player_goals",
    "core_bet_type_65_assists":         "player_assists",
    # Football (NFL)
    "core_bet_type_37_pass_yards":      "player_pass_yds",
    "core_bet_type_38_rush_yards":      "player_rush_yds",
    "core_bet_type_39_rec_yards":       "player_reception_yds",
    "core_bet_type_40_receptions":      "player_receptions",
    "core_bet_type_41_pass_tds":        "player_pass_tds",
    "core_bet_type_42_rush_tds":        "player_rush_tds",
    "core_bet_type_43_rec_tds":         "player_reception_tds",
}

BOOK_NAMES = {
    "15":  "DraftKings",
    "30":  "FanDuel",
    "76":  "BetMGM",
    "75":  "Caesars",
    "123": "BetRivers",
    "69":  "PointsBet",
    "68":  "Barstool",
    "79":  "Unibet",
}


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_get(key: str):
    path = _CACHE_DIR / f"{key}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if time.time() - data["ts"] < _CACHE_TTL:
                return data["payload"]
        except Exception:
            pass
    return None


def _cache_set(key: str, payload):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.json").write_text(
        json.dumps({"ts": time.time(), "payload": payload})
    )


# ── API calls ─────────────────────────────────────────────────────────────────

def get_events(sport: str = "nba") -> list[dict]:
    """
    Return today's games for a sport from Action Network.
    Each dict: {id, away_team, home_team, start_time, status}
    """
    cache_key = f"an_events_{sport}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            f"{AN_V1}/scoreboard/{sport}",
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[action_network] Failed to fetch {sport} events: {e}")
        return []

    team_lookup: dict[int, str] = {}
    events = []

    for game in data.get("games", []):
        for team in game.get("teams", []):
            team_lookup[team["id"]] = team.get("full_name", team.get("name", ""))

        away = team_lookup.get(game.get("away_team_id", 0), "")
        home = team_lookup.get(game.get("home_team_id", 0), "")

        events.append({
            "id":         game["id"],
            "away_team":  away,
            "home_team":  home,
            "start_time": game.get("start_time", ""),
            "status":     game.get("status", ""),
        })

    _cache_set(cache_key, events)
    return events


def get_player_props(game_id: int) -> dict:
    """
    Fetch multi-book player prop lines for a game.
    Returns Odds API-compatible format:
      {"bookmakers": [{"title": "DraftKings", "markets": [...]}]}
    """
    cache_key = f"an_props_{game_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            f"{AN_BASE}/games/{game_id}/props",
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        print(f"[action_network] Failed to fetch props for game {game_id}: {e}")
        return {"bookmakers": []}

    # Build player_id → full_name lookup
    player_names = {
        str(pid): info.get("full_name", info.get("name", ""))
        for pid, info in raw.get("players", {}).items()
    }

    # {book_id: {stat_key: {(player_name, line): {Over/Under: odds}}}}
    by_book: dict[str, dict] = {}

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
                if book_id_str not in BOOK_NAMES:
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

                    by_book.setdefault(book_id_str, {})
                    by_book[book_id_str].setdefault(stat_key, {})
                    key = (player_name, float(line_val))
                    by_book[book_id_str][stat_key].setdefault(key, {})
                    by_book[book_id_str][stat_key][key][side] = int(odds_val)

    # Convert to Odds API format
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
            bookmakers.append({"title": BOOK_NAMES[book_id_str], "markets": markets})

    result = {"bookmakers": bookmakers}
    _cache_set(cache_key, result)
    return result


# ── Multi-sport consensus fetch ───────────────────────────────────────────────

def get_all_consensus(league_ids: set[int] | None = None) -> dict:
    """
    Fetch consensus lines for all supported sports (or filtered by league_ids).

    Returns {(player_name_normalized, market_key): [list of book lines]}
    — caller computes median.
    """
    sports_to_fetch = set(SPORT_CONFIG.keys())
    if league_ids:
        sports_to_fetch = {
            LEAGUE_TO_SPORT[lid]
            for lid in league_ids
            if lid in LEAGUE_TO_SPORT
        }

    all_props: dict = {}  # game_id → props
    for sport in sports_to_fetch:
        try:
            events = get_events(sport)
            for ev in events:
                props = get_player_props(ev["id"])
                all_props[ev["id"]] = props
        except Exception as e:
            print(f"[action_network] Error fetching {sport}: {e}")

    return all_props
