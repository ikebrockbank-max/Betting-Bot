"""
MLB Stats API client (statsapi.mlb.com — no API key required).

Provides per-player recent averages (L5, L10) and season averages
for both batters and pitchers.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

MLB_BASE         = "https://statsapi.mlb.com/api/v1"
CACHE_PATH       = Path("logs/.mlb_player_cache.json")
STATS_CACHE_PATH = Path("logs/.mlb_stats_cache.json")
STATS_CACHE_TTL  = 4 * 3600  # 4 hours

STAT_COL: dict[str, str | list] = {
    "Hits":                "hits",
    "Total Bases":         "totalBases",
    "Runs":                "runs",
    "RBIs":                "rbi",
    "Home Runs":           "homeRuns",
    "Stolen Bases":        "stolenBases",
    "Hitter Strikeouts":   "strikeOuts",
    "Pitcher Strikeouts":  "strikeOuts",
    "Walks":               "baseOnBalls",
    "Doubles":             "doubles",
    "Singles":             "_singles",       # computed
    "Hits+Runs+RBIs":      ["hits", "runs", "rbi"],
}

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


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


def _load_player_cache() -> dict:
    """Return cached player ID map. Structure: {date, players: {name_lower: id}}."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text())
            if cached.get("date") == today:
                return cached
        except Exception:
            pass
    return {"date": today, "players": {}}


def _save_player_cache(cache: dict):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache))


def _lookup_player_id(name: str, cache: dict) -> int | None:
    """Look up MLB player ID by name via /people/search. Falls back to last-name."""
    key = name.lower().strip()
    if key in cache["players"]:
        return cache["players"][key]

    last_name = name.split()[-1]
    for search_name in [name, last_name]:
        try:
            resp = requests.get(
                f"{MLB_BASE}/people/search",
                headers=HEADERS,
                params={"names": search_name, "sportId": 1},
                timeout=10,
            )
            resp.raise_for_status()
            people = resp.json().get("people", [])
            if people:
                # Prefer exact full name match, fall back to last-name match
                for p in people:
                    full = p.get("fullName", "").lower()
                    if full == key:
                        pid = p["id"]
                        cache["players"][key] = pid
                        return pid
                # Last-name fallback if only one match
                last_matches = [p for p in people if p.get("lastName", "").lower() == last_name.lower()]
                if len(last_matches) == 1:
                    pid = last_matches[0]["id"]
                    cache["players"][key] = pid
                    return pid
        except Exception:
            pass
        time.sleep(0.2)

    return None


def _fetch_game_log(player_id: int, season: str, group: str) -> list[dict]:
    """Fetch game log splits for hitting or pitching."""
    try:
        resp = requests.get(
            f"{MLB_BASE}/people/{player_id}/stats",
            headers=HEADERS,
            params={"stats": "gameLog", "season": season, "group": group},
            timeout=15,
        )
        resp.raise_for_status()
        stats = resp.json().get("stats", [])
        if stats:
            return stats[0].get("splits", [])
    except Exception:
        pass
    return []


def _compute_singles(stat: dict) -> float:
    """singles = hits - doubles - triples - homeRuns"""
    hits     = float(stat.get("hits",     0) or 0)
    doubles  = float(stat.get("doubles",  0) or 0)
    triples  = float(stat.get("triples",  0) or 0)
    homers   = float(stat.get("homeRuns", 0) or 0)
    return max(0.0, hits - doubles - triples - homers)


def _extract_val(stat: dict, col: str | list) -> float | None:
    """Extract a stat value from a game split stat dict."""
    if col == "_singles":
        return _compute_singles(stat)
    if isinstance(col, list):
        try:
            return sum(float(stat.get(c, 0) or 0) for c in col)
        except (TypeError, ValueError):
            return None
    val = stat.get(col)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def get_player_stats(
    player_name: str,
    stat_type: str,
    season: str = "2026",
) -> dict | None:
    """
    Fetch MLB game log and return stat context.
    Returns same shape as nba_stats.get_player_stats.
    Returns None if player not found or stat unsupported.
    """
    col = STAT_COL.get(stat_type)
    if col is None:
        return None

    # Check stats cache first (4h TTL)
    cache_key = f"{player_name.lower()}|{stat_type}|{season}"
    _sc = _load_stats_cache()
    _entry = _sc.get(cache_key)
    if _entry and (time.time() - _entry.get("ts", 0)) < STATS_CACHE_TTL:
        return _entry.get("data")

    cache = _load_player_cache()
    player_id = _lookup_player_id(player_name, cache)
    _save_player_cache(cache)

    if not player_id:
        return None

    # Try hitting first, then pitching
    splits = []
    group  = "hitting"
    if stat_type == "Pitcher Strikeouts":
        group = "pitching"
        splits = _fetch_game_log(player_id, season, "pitching")
    else:
        splits = _fetch_game_log(player_id, season, "hitting")
        if not splits:
            group  = "pitching"
            splits = _fetch_game_log(player_id, season, "pitching")

    if not splits:
        return None

    all_vals = []
    for split in splits:
        stat = split.get("stat", {})
        val  = _extract_val(stat, col)
        if val is not None:
            all_vals.append(val)

    if not all_vals:
        return None

    # MLB splits come oldest-first; reverse so index 0 = most recent
    all_vals = list(reversed(all_vals))

    n = len(all_vals)

    # Quality gate: skip platoon/backup players with too few appearances.
    # For batters we require at least 15 games; for pitchers at least 5 starts.
    min_games = 5 if group == "pitching" else 15
    if n < min_games:
        return None

    n5  = min(5, n)
    n10 = min(10, n)

    season_avg = sum(all_vals) / n
    l10_avg    = sum(all_vals[:n10]) / n10
    l5_avg     = sum(all_vals[:n5])  / n5

    result = {
        "player_id":          player_id,
        "season_avg":         round(season_avg, 2),
        "l10_avg":            round(l10_avg, 2),
        "l5_avg":             round(l5_avg, 2),
        "last_5":             all_vals[:5],
        "games_played":       n,
        "rest_games_removed": 0,
        "season_min":         0.0,
        "l5_min":             0.0,
        "min_change_pct":     0.0,
        "minutes_flag":       None,
        "season_per36":       0.0,
        "l5_per36":           0.0,
        "per36_change":       0.0,
    }
    _sc = _load_stats_cache()
    _sc[cache_key] = {"ts": time.time(), "data": result}
    _save_stats_cache(_sc)
    return result
