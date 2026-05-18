"""
NBA Stats API client (stats.nba.com — no API key required).

Provides per-player recent averages (L5, L10) and season averages,
including minutes-adjusted context to account for playing time changes
caused by teammate injuries or role shifts.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Referer": "https://www.nba.com/",
    "Accept": "application/json",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}

CACHE_PATH       = Path("logs/.nba_player_cache.json")
STATS_CACHE_PATH = Path("logs/.nba_stats_cache.json")   # per-player stats, TTL 4h

# PrizePicks / ParlayPlay stat name → NBA game-log column
STAT_COL = {
    "Points":            "PTS",
    "Rebounds":          "REB",
    "Assists":           "AST",
    "3-PT Made":         "FG3M",
    "3-Pointers Made":   "FG3M",
    "3PT Made":          "FG3M",
    "Turnovers":         "TOV",
    "Blocks":            "BLK",
    "Steals":            "STL",
}

# Combined stats: sum these columns per game
COMBINED_STAT_COLS: dict[str, list[str]] = {
    "Pts+Reb+Ast":             ["PTS", "REB", "AST"],
    "Points+Rebounds+Assists": ["PTS", "REB", "AST"],
    "Pts + Reb + Ast":         ["PTS", "REB", "AST"],
    "Points+Rebounds":         ["PTS", "REB"],
    "Pts+Reb":                 ["PTS", "REB"],
    "Pts + Reb":               ["PTS", "REB"],
    "Points+Assists":          ["PTS", "AST"],
    "Pts+Ast":                 ["PTS", "AST"],
    "Pts + Ast":               ["PTS", "AST"],
    "Rebounds+Assists":        ["REB", "AST"],
    "Reb+Ast":                 ["REB", "AST"],
    "Reb + Ast":               ["REB", "AST"],
}

# Minutes change thresholds for flagging
MIN_BUMP_PCT   = 0.15   # L5 minutes > 15% above season avg → elevated role
MIN_DROP_PCT   = 0.15   # L5 minutes > 15% below season avg → reduced role


STATS_CACHE_TTL = 4 * 3600  # 4 hours

def _load_stats_cache() -> dict:
    try:
        if STATS_CACHE_PATH.exists():
            return json.loads(STATS_CACHE_PATH.read_text())
    except Exception:
        pass
    return {}

def _save_stats_cache(cache: dict):
    STATS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATS_CACHE_PATH.write_text(json.dumps(cache))


def _load_player_ids() -> dict[str, int]:
    """Return {full_name_lower: player_id}. Cached daily."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if CACHE_PATH.exists():
        cached = json.loads(CACHE_PATH.read_text())
        if cached.get("date") == today:
            return cached["players"]

    resp = requests.get(
        "https://stats.nba.com/stats/commonallplayers",
        headers=NBA_HEADERS,
        params={"LeagueID": "00", "Season": "2025-26", "IsOnlyCurrentSeason": 1},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    hdrs = data["resultSets"][0]["headers"]
    rows = data["resultSets"][0]["rowSet"]

    players = {}
    for row in rows:
        p = dict(zip(hdrs, row))
        name = p.get("DISPLAY_FIRST_LAST", "").strip()
        pid = p.get("PERSON_ID")
        if name and pid:
            players[name.lower()] = pid

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps({"date": today, "players": players}))
    return players


def _find_player_id(name: str, players: dict[str, int]) -> int | None:
    key = name.lower().strip()
    if key in players:
        return players[key]
    last = key.split()[-1]
    matches = {k: v for k, v in players.items() if k.split()[-1] == last}
    if len(matches) == 1:
        return list(matches.values())[0]
    return None


def _parse_minutes(min_str) -> float:
    """Parse NBA minutes string '32:15' → 32.25."""
    if min_str is None:
        return 0.0
    try:
        if ":" in str(min_str):
            parts = str(min_str).split(":")
            return float(parts[0]) + float(parts[1]) / 60
        return float(min_str)
    except (ValueError, IndexError):
        return 0.0


def get_player_stats(
    player_name: str,
    stat_type: str,
    season: str = "2025-26",
) -> dict | None:
    """
    Fetch game log and return stat + minutes context.
    Supports single stats (Points, Rebounds, ...) and combined stats
    (Pts+Reb+Ast, Points+Rebounds, etc.) via COMBINED_STAT_COLS.

    Returns dict with:
        player_id       int
        season_avg      float   — full season average for the stat
        l10_avg         float   — last 10 games average
        l5_avg          float   — last 5 games average
        last_5          list    — raw values, most recent first
        games_played    int

        season_min      float   — avg minutes per game (season)
        l5_min          float   — avg minutes per game (last 5)
        min_change_pct  float   — how much l5_min differs from season_min
        minutes_flag    str|None — "elevated" | "reduced" | None

        season_per36    float   — stat per 36 minutes (season)
        l5_per36        float   — stat per 36 minutes (last 5)
        per36_change    float   — change in per-36 rate

    Returns None if player not found or stat unsupported.
    """
    # Check stats cache first (4h TTL)
    cache_key = f"{player_name.lower()}|{stat_type}|{season}"
    _sc = _load_stats_cache()
    _entry = _sc.get(cache_key)
    if _entry and (time.time() - _entry.get("ts", 0)) < STATS_CACHE_TTL:
        return _entry.get("data")

    # Determine columns to fetch
    col  = STAT_COL.get(stat_type)
    cols = COMBINED_STAT_COLS.get(stat_type)  # list of cols to sum, or None
    if not col and not cols:
        return None
    fetch_cols = cols if cols else [col]

    players = _load_player_ids()
    player_id = _find_player_id(player_name, players)
    if not player_id:
        return None

    rows, hdrs = [], []
    for season_type in ("Playoffs", "Regular Season"):
        try:
            resp = requests.get(
                "https://stats.nba.com/stats/playergamelog",
                headers=NBA_HEADERS,
                params={
                    "PlayerID": player_id,
                    "Season": season,
                    "SeasonType": season_type,
                    "LeagueID": "00",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            rows = data["resultSets"][0]["rowSet"]
            hdrs = data["resultSets"][0]["headers"]
            if rows:
                break  # got data — stop trying
            time.sleep(0.3)
        except Exception:
            continue

    if not rows:
        return None

    all_vals = []
    all_mins = []

    for row in rows:
        g = dict(zip(hdrs, row))
        # Sum all required columns for this game
        try:
            val = sum(float(g[c]) for c in fetch_cols if g.get(c) is not None)
        except (TypeError, KeyError):
            continue
        mins = _parse_minutes(g.get("MIN"))
        if mins > 0:
            all_vals.append(val)
            all_mins.append(mins)

    if not all_vals:
        return None

    n = len(all_vals)

    # Season averages use ALL games (including rest games)
    season_avg = sum(all_vals) / n
    season_min = sum(all_mins) / n

    # Filter out rest/garbage-time games for recent averages.
    # A rest game = player played < 60% of their season average minutes.
    # This prevents end-of-season sitting from contaminating L5/L10.
    rest_threshold = season_min * 0.60
    full_games = [
        (v, m) for v, m in zip(all_vals, all_mins)
        if m >= rest_threshold
    ]
    rest_games_removed = n - len(full_games)

    # Use filtered list for recent averages (still most-recent first)
    fv = [v for v, _ in full_games]
    fm = [m for _, m in full_games]

    nf = len(fv)
    if nf == 0:
        # All games were rest games — fall back to raw
        fv, fm, nf = all_vals, all_mins, n

    n5  = min(5, nf)
    n10 = min(10, nf)

    l10_avg = sum(fv[:n10]) / n10
    l5_avg  = sum(fv[:n5])  / n5
    l5_min  = sum(fm[:n5])  / n5

    stat_vals = fv
    min_vals  = fm

    # Per-36 rates (normalize to 36 minutes to isolate true production)
    def per36(stat_list, min_list):
        total_stat = sum(stat_list)
        total_min  = sum(min_list)
        if total_min == 0:
            return 0.0
        return (total_stat / total_min) * 36

    season_per36 = per36(stat_vals, min_vals)
    l5_per36     = per36(stat_vals[:n5], min_vals[:n5])
    per36_change = l5_per36 - season_per36

    # Minutes flag
    min_change_pct = (l5_min - season_min) / season_min if season_min > 0 else 0.0
    if min_change_pct > MIN_BUMP_PCT:
        minutes_flag = "elevated"
    elif min_change_pct < -MIN_DROP_PCT:
        minutes_flag = "reduced"
    else:
        minutes_flag = None

    result = {
        "player_id":           player_id,
        "season_avg":          round(season_avg, 2),
        "l10_avg":             round(l10_avg, 2),
        "l5_avg":              round(l5_avg, 2),
        "last_5":              stat_vals[:5],
        "games_played":        n,
        "rest_games_removed":  rest_games_removed,
        "season_min":          round(season_min, 1),
        "l5_min":              round(l5_min, 1),
        "min_change_pct":      round(min_change_pct, 3),
        "minutes_flag":        minutes_flag,
        "season_per36":        round(season_per36, 2),
        "l5_per36":            round(l5_per36, 2),
        "per36_change":        round(per36_change, 2),
    }
    # Save to cache
    _sc = _load_stats_cache()
    _sc[cache_key] = {"ts": time.time(), "data": result}
    _save_stats_cache(_sc)
    return result


def get_stats_bulk(
    players: list[tuple[str, str]],
    delay: float = 0.4,
) -> dict[tuple[str, str], dict]:
    """Fetch stats for multiple (player, stat_type) pairs."""
    _load_player_ids()  # prime cache once
    results = {}
    for player_name, stat_type in players:
        result = get_player_stats(player_name, stat_type)
        if result:
            results[(player_name, stat_type)] = result
        time.sleep(delay)
    return results
