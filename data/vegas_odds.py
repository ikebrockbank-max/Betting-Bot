"""
OddsAPI client — game totals, spreads, and player prop lines.

Free-tier API: 500 requests/month. We cache aggressively (10 min) to preserve quota.
Sign up at https://the-odds-api.com to get a free key.
Set env var: ODDS_API_KEY

Provides:
  get_game_totals(sport)   → list of game total/implied-score dicts
  get_team_implied(sport, team_name) → float | None
  compare_pp_to_books(player, stat, line, sport) → dict | None
"""

import json
import os
import time
from datetime import datetime, timezone
from math import cos, radians
from pathlib import Path

import requests

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
CACHE_PATH   = Path("logs/.vegas_odds_cache.json")
CACHE_TTL    = 600  # 10 minutes

SPORT_KEY_MAP = {
    "NBA":  "basketball_nba",
    "WNBA": "basketball_wnba",
    "MLB":  "baseball_mlb",
}

PREFERRED_BOOKS = ["draftkings", "fanduel", "betmgm", "caesars"]

STAT_MARKET_MAP = {
    # NBA / WNBA
    "Points":          "player_points",
    "Rebounds":        "player_rebounds",
    "Assists":         "player_assists",
    "3-PT Made":       "player_threes",
    "Turnovers":       "player_turnovers",
    # MLB batter
    "Hits":            "batter_hits",
    "Home Runs":       "batter_home_runs",
    "RBIs":            "batter_rbis",
    "Total Bases":     "batter_total_bases",
    "Walks":           "batter_walks",
    # MLB pitcher
    "Pitcher Strikeouts": "pitcher_strikeouts",
}


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_cache(data: dict):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data))


def _cache_get(key: str):
    cache = _load_cache()
    entry = cache.get(key)
    if entry and (time.time() - entry.get("ts", 0)) < CACHE_TTL:
        return entry.get("data")
    return None


def _cache_set(key: str, data):
    cache = _load_cache()
    cache[key] = {"ts": time.time(), "data": data}
    _save_cache(cache)


# ── Internal fetch helpers ─────────────────────────────────────────────────────

def _pick_best_bookmaker(bookmakers: list, market_key: str):
    """Return the best bookmaker's outcomes for a given market key."""
    book_map = {b["key"]: b for b in bookmakers}
    for bk in PREFERRED_BOOKS:
        book = book_map.get(bk)
        if not book:
            continue
        for mkt in book.get("markets", []):
            if mkt["key"] == market_key:
                return mkt.get("outcomes", [])
    # Fallback: first available
    for book in bookmakers:
        for mkt in book.get("markets", []):
            if mkt["key"] == market_key:
                return mkt.get("outcomes", [])
    return []


# ── Public API ─────────────────────────────────────────────────────────────────

def get_game_totals(sport: str) -> list[dict]:
    """
    Fetch game totals and implied team scores for a sport.

    Returns list of dicts:
      {home_team, away_team, total, home_implied, away_implied,
       home_spread, sport, commence_time}
    Returns [] if ODDS_API_KEY is not set or any error occurs.
    """
    if not ODDS_API_KEY:
        return []

    sport_key = SPORT_KEY_MAP.get(sport)
    if not sport_key:
        return []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache_key = f"totals_{sport}_{today}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
            params={
                "apiKey":      ODDS_API_KEY,
                "regions":     "us",
                "markets":     "totals,spreads",
                "oddsFormat":  "american",
                "dateFormat":  "iso",
            },
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json()
    except Exception:
        return []

    results = []
    for event in events:
        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            continue

        home_team = event.get("home_team", "")
        away_team = event.get("away_team", "")
        commence  = event.get("commence_time", "")

        # Extract total
        total_outcomes = _pick_best_bookmaker(bookmakers, "totals")
        total = None
        for o in total_outcomes:
            if o.get("name", "").lower() == "over":
                try:
                    total = float(o["point"])
                except Exception:
                    pass
                break

        if total is None:
            continue

        # Extract home spread
        spread_outcomes = _pick_best_bookmaker(bookmakers, "spreads")
        home_spread = 0.0
        for o in spread_outcomes:
            if o.get("name", "") == home_team:
                try:
                    home_spread = float(o["point"])
                except Exception:
                    home_spread = 0.0
                break

        # Compute implied scores
        # If home_spread is negative, home team is favored
        # home_implied = (total - home_spread) / 2 when spread is from home's perspective
        home_implied = (total - home_spread) / 2.0
        away_implied = total - home_implied

        results.append({
            "home_team":     home_team,
            "away_team":     away_team,
            "total":         total,
            "home_implied":  round(home_implied, 1),
            "away_implied":  round(away_implied, 1),
            "home_spread":   home_spread,
            "sport":         sport,
            "commence_time": commence,
        })

    _cache_set(cache_key, results)
    return results


def get_team_implied(sport: str, team_name: str) -> float | None:
    """
    Return the implied score for a team in tonight's games.
    Fuzzy-matches on the last word of the team name (case-insensitive).
    Returns None if not found.
    """
    totals = get_game_totals(sport)
    if not totals:
        return None

    needle = team_name.strip().lower().split()[-1] if team_name.strip() else ""

    for game in totals:
        home_last = game["home_team"].strip().lower().split()[-1]
        away_last = game["away_team"].strip().lower().split()[-1]

        if home_last == needle:
            return game["home_implied"]
        if away_last == needle:
            return game["away_implied"]

    return None


def compare_pp_to_books(
    pp_player: str,
    pp_stat: str,
    pp_line: float,
    sport: str,
) -> dict | None:
    """
    Compare a PrizePicks line to the consensus book line.

    Returns:
      {pp_line, book_line, edge_pct, direction}
      edge_pct > 0 means PP line is lower than books (easier OVER)
      direction: "easier" | "harder"
    Returns None if key not set, stat not mapped, or player not found.
    """
    if not ODDS_API_KEY:
        return None

    market_key = STAT_MARKET_MAP.get(pp_stat)
    if not market_key:
        return None

    sport_key = SPORT_KEY_MAP.get(sport)
    if not sport_key:
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    props_cache_key = f"props_{sport}_{today}"
    cached_props = _cache_get(props_cache_key)

    if cached_props is None:
        # Fetch today's event list
        try:
            resp = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/events",
                params={"apiKey": ODDS_API_KEY, "dateFormat": "iso"},
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception:
            return None

        # Filter to today's events and limit to 8 to save quota
        today_events = []
        for ev in events:
            commence = ev.get("commence_time", "")
            if today in commence:
                today_events.append(ev)
            if len(today_events) >= 8:
                break

        # Fetch player props for each event
        all_props: list[dict] = []
        for ev in today_events:
            ev_id = ev.get("id", "")
            if not ev_id:
                continue
            try:
                r = requests.get(
                    f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{ev_id}/odds",
                    params={
                        "apiKey":     ODDS_API_KEY,
                        "regions":    "us",
                        "markets":    ",".join(set(STAT_MARKET_MAP.values())),
                        "oddsFormat": "american",
                    },
                    timeout=15,
                )
                r.raise_for_status()
                all_props.append(r.json())
                time.sleep(0.3)
            except Exception:
                continue

        _cache_set(props_cache_key, all_props)
        cached_props = all_props

    # Search for the player across all events
    player_lower = pp_player.lower()
    book_lines: list[float] = []

    for event_data in (cached_props or []):
        bookmakers = event_data.get("bookmakers", [])
        for book in bookmakers:
            if book.get("key") not in PREFERRED_BOOKS:
                continue
            for mkt in book.get("markets", []):
                if mkt.get("key") != market_key:
                    continue
                for outcome in mkt.get("outcomes", []):
                    desc = outcome.get("description", "").lower()
                    if player_lower in desc or player_lower.split()[-1] in desc:
                        try:
                            point = float(outcome["point"])
                            book_lines.append(point)
                        except Exception:
                            pass

    if not book_lines:
        return None

    book_line = round(sum(book_lines) / len(book_lines), 1)
    edge_pct  = (book_line - pp_line) / book_line * 100 if book_line else 0.0

    return {
        "pp_line":   pp_line,
        "book_line": book_line,
        "edge_pct":  round(edge_pct, 2),
        "direction": "easier" if edge_pct > 0 else "harder",
    }
