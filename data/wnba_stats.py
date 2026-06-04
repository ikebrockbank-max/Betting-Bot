"""
WNBA stats client using ESPN APIs (no API key required).

Provides per-player recent averages (L5, L10) and season averages.
Mirrors data/nba_stats.py for WNBA.

Data source: ESPN athlete gamelog API (stats.wnba.com is geo-blocked).
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ESPN_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
CACHE_PATH       = Path("logs/.wnba_player_cache.json")
STATS_CACHE_PATH = Path("logs/.wnba_stats_cache.json")
STATS_CACHE_TTL  = 4 * 3600  # 4 hours

ESPN_TEAMS_URL   = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams"
ESPN_ROSTER_URL  = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/roster"
ESPN_GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/wnba/athletes/{athlete_id}/gamelog"

# Stat label -> column in ESPN gamelog labels array
# Labels: MIN PTS REB AST STL BLK TO FG FG% 3PT 3P% FT FT% PF
STAT_COL = {
    "Points":   "PTS",
    "Rebounds": "REB",
    "Assists":  "AST",
    "3-PT Made": "3PT",   # format "made-att" — we parse the made part
}

COMBINED_STAT_COLS: dict[str, list[str]] = {
    "Pts+Rebs":      ["PTS", "REB"],
    "Pts+Asts":      ["PTS", "AST"],
    "Pts+Rebs+Asts": ["PTS", "REB", "AST"],
    "Rebs+Asts":     ["REB", "AST"],
}

MIN_BUMP_PCT = 0.15
MIN_DROP_PCT = 0.15


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


def _load_player_ids() -> dict[str, str]:
    """Return {full_name_lower: espn_athlete_id}. Cached daily."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text())
            if cached.get("date") == today:
                return cached["players"]
        except Exception:
            pass

    # Fetch all teams, then all rosters
    try:
        resp = requests.get(ESPN_TEAMS_URL, headers=ESPN_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        sports  = data.get("sports", [{}])[0]
        leagues = sports.get("leagues", [{}])[0]
        teams   = leagues.get("teams", [])
    except Exception:
        return {}

    players: dict[str, str] = {}
    for team_entry in teams:
        team_id = team_entry.get("team", {}).get("id")
        if not team_id:
            continue
        try:
            r = requests.get(
                ESPN_ROSTER_URL.format(team_id=team_id),
                headers=ESPN_HEADERS,
                timeout=10,
            )
            r.raise_for_status()
            for athlete in r.json().get("athletes", []):
                name = athlete.get("fullName", athlete.get("displayName", "")).strip()
                aid  = athlete.get("id", "")
                if name and aid:
                    players[name.lower()] = str(aid)
        except Exception:
            continue
        time.sleep(0.2)

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps({"date": today, "players": players}))
    return players


def _find_athlete_id(name: str, players: dict[str, str]) -> str | None:
    key = name.lower().strip()
    if key in players:
        return players[key]
    last = key.split()[-1]
    matches = {k: v for k, v in players.items() if k.split()[-1] == last}
    if len(matches) == 1:
        return list(matches.values())[0]
    return None


def _parse_stat_val(raw: str, col: str) -> float | None:
    """Parse a stat value string. Handles 'X-Y' format for FG/3PT/FT (returns made)."""
    if raw is None:
        return None
    try:
        if col in ("FG", "3PT", "FT") and "-" in str(raw):
            return float(str(raw).split("-")[0])
        return float(raw)
    except (ValueError, TypeError):
        return None


def _parse_minutes(raw: str) -> float:
    """Parse minutes string (e.g. '30' or '30:15') to float."""
    if raw is None:
        return 0.0
    try:
        if ":" in str(raw):
            parts = str(raw).split(":")
            return float(parts[0]) + float(parts[1]) / 60
        return float(raw)
    except (ValueError, IndexError):
        return 0.0


def _fetch_gamelog(athlete_id: str, season: str) -> tuple[list[float], list[float], list[str]]:
    """
    Fetch ESPN WNBA gamelog. Returns (all_vals_for_each_game, all_mins, labels).
    Values are returned most-recent first.
    """
    try:
        resp = requests.get(
            ESPN_GAMELOG_URL.format(athlete_id=athlete_id),
            headers=ESPN_HEADERS,
            params={"season": season},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return [], [], []

    labels = data.get("labels", [])
    season_types = data.get("seasonTypes", [])

    # Collect all events across all categories (months)
    events_flat: list[dict] = []
    for stype in season_types:
        for cat in stype.get("categories", []):
            events_flat.extend(cat.get("events", []))

    return events_flat, labels


def get_player_stats(
    player_name: str,
    stat_type: str,
    season: str = "2025",
) -> dict | None:
    """
    Fetch WNBA game log via ESPN and return stat context.
    Returns same shape as nba_stats.get_player_stats.
    Returns None if player not found or stat unsupported.
    """
    col  = STAT_COL.get(stat_type)
    cols = COMBINED_STAT_COLS.get(stat_type)
    if not col and not cols:
        return None
    fetch_cols = cols if cols else [col]

    # Check stats cache first (4h TTL)
    cache_key = f"{player_name.lower()}|{stat_type}|{season}"
    _sc = _load_stats_cache()
    _entry = _sc.get(cache_key)
    if _entry and (time.time() - _entry.get("ts", 0)) < STATS_CACHE_TTL:
        return _entry.get("data")

    try:
        players = _load_player_ids()
    except Exception:
        return None

    athlete_id = _find_athlete_id(player_name, players)
    if not athlete_id:
        return None

    events_flat, labels = _fetch_gamelog(athlete_id, season)
    if not events_flat or not labels:
        return None

    all_vals: list[float] = []
    all_mins: list[float] = []

    # Events come oldest-first — reverse for most-recent first
    for event in reversed(events_flat):
        raw_stats = event.get("stats", [])
        game = dict(zip(labels, raw_stats))

        mins = _parse_minutes(game.get("MIN"))
        if mins <= 0:
            continue

        try:
            val = sum(
                _parse_stat_val(game.get(c), c) or 0.0
                for c in fetch_cols
            )
        except Exception:
            continue

        all_vals.append(val)
        all_mins.append(mins)

    if not all_vals:
        return None

    n = len(all_vals)
    season_avg = sum(all_vals) / n
    season_min = sum(all_mins) / n

    rest_threshold = season_min * 0.60
    full_games = [(v, m) for v, m in zip(all_vals, all_mins) if m >= rest_threshold]

    fv = [v for v, _ in full_games]
    fm = [m for _, m in full_games]
    nf = len(fv)
    if nf == 0:
        fv, fm, nf = all_vals, all_mins, n

    n5  = min(5, nf)
    n10 = min(10, nf)

    l10_avg = sum(fv[:n10]) / n10
    l5_avg  = sum(fv[:n5])  / n5
    l5_min  = sum(fm[:n5])  / n5

    def per36(stat_list, min_list):
        total_stat = sum(stat_list)
        total_min  = sum(min_list)
        if total_min == 0:
            return 0.0
        return (total_stat / total_min) * 36

    season_per36 = per36(fv, fm)
    l5_per36     = per36(fv[:n5], fm[:n5])
    per36_change = l5_per36 - season_per36

    min_change_pct = (l5_min - season_min) / season_min if season_min > 0 else 0.0
    if min_change_pct > MIN_BUMP_PCT:
        minutes_flag = "elevated"
    elif min_change_pct < -MIN_DROP_PCT:
        minutes_flag = "reduced"
    else:
        minutes_flag = None

    result = {
        "player_id":          athlete_id,
        "season_avg":         round(season_avg, 2),
        "l10_avg":            round(l10_avg, 2),
        "l5_avg":             round(l5_avg, 2),
        "last_5":             fv[:5],
        "game_values":        fv,        # full filtered game log (most-recent first)
        "games_played":       n,
        "rest_games_removed": n - nf,
        "season_min":         round(season_min, 1),
        "l5_min":             round(l5_min, 1),
        "min_change_pct":     round(min_change_pct, 3),
        "minutes_flag":       minutes_flag,
        "season_per36":       round(season_per36, 2),
        "l5_per36":           round(l5_per36, 2),
        "per36_change":       round(per36_change, 2),
    }
    _sc = _load_stats_cache()
    _sc[cache_key] = {"ts": time.time(), "data": result}
    _save_stats_cache(_sc)
    return result
