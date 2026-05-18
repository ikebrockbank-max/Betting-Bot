"""
Confirmed starting lineups and probable pitchers.

MLB: statsapi.mlb.com /api/v1/schedule with lineups hydration (official, free)
NBA: stats.nba.com (starters from tonight's scoreboard)
WNBA: ESPN scoreboard with lineups

is_player_starting(player_name, sport, team=None) -> bool | None
  True = confirmed starter, False = confirmed not starting, None = unknown

get_mlb_probable_pitcher(home_team, away=False) -> str | None
  Returns probable pitcher name for today's game

get_mlb_batting_order(team_name) -> list[str] | None
  Returns confirmed batting order if available

get_all_lineups(sport) -> dict
  Convenience wrapper — fetches all lineups for the given sport.
  Returns dict usable internally by is_player_starting.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

CACHE_PATH = Path("logs/.lineups_cache.json")
CACHE_TTL  = 1800  # 30 minutes

NBA_HEADERS = {
    "User-Agent":          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":             "https://www.nba.com/",
    "Accept":              "application/json",
    "x-nba-stats-origin":  "stats",
    "x-nba-stats-token":   "true",
}

ESPN_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


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


# ── Name matching ──────────────────────────────────────────────────────────────

def _last_word(name: str) -> str:
    return name.strip().lower().split()[-1] if name.strip() else ""


def _name_match(query: str, candidate: str) -> bool:
    """Case-insensitive: full match, or last-name match."""
    q = query.strip().lower()
    c = candidate.strip().lower()
    if q == c:
        return True
    if q.split()[-1] == c.split()[-1]:
        return True
    if q in c or c in q:
        return True
    return False


# ── MLB Lineups ────────────────────────────────────────────────────────────────

def _fetch_mlb_lineups() -> dict:
    """
    Fetch today's MLB lineups and probable pitchers from statsapi.
    Returns:
      {
        "game_key": {
          home_team, away_team,
          home_lineup: [names], away_lineup: [names],
          home_pitcher: str, away_pitcher: str,
          lineups_posted: bool
        }
      }
    """
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache_key = f"mlb_lineups_{today}"
    cached   = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={
                "sportId": 1,
                "date":    today,
                "hydrate": "probablePitcher,lineups,teams",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    games = {}
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            gid = str(game.get("gamePk", ""))
            if not gid:
                continue

            home_data = game.get("teams", {}).get("home", {})
            away_data = game.get("teams", {}).get("away", {})

            home_team    = home_data.get("team", {}).get("name", "")
            away_team    = away_data.get("team", {}).get("name", "")
            home_pitcher = home_data.get("probablePitcher", {}).get("fullName", "")
            away_pitcher = away_data.get("probablePitcher", {}).get("fullName", "")

            # Parse lineups
            lineups_data = game.get("lineups", {})
            home_players = lineups_data.get("homePlayers", [])
            away_players = lineups_data.get("awayPlayers", [])

            def _extract_names(players: list) -> list[str]:
                names = []
                for p in players:
                    full = p.get("fullName", "") or p.get("name", {}).get("full", "")
                    if full:
                        names.append(full)
                return names

            home_lineup = _extract_names(home_players)
            away_lineup = _extract_names(away_players)

            game_key = f"{away_team} @ {home_team}"
            games[game_key] = {
                "home_team":     home_team,
                "away_team":     away_team,
                "home_lineup":   home_lineup,
                "away_lineup":   away_lineup,
                "home_pitcher":  home_pitcher,
                "away_pitcher":  away_pitcher,
                "lineups_posted": bool(home_lineup or away_lineup),
            }

    _cache_set(cache_key, games)
    return games


# ── NBA Lineups ────────────────────────────────────────────────────────────────

def _fetch_nba_lineups() -> dict:
    """
    Fetch tonight's NBA starters from stats.nba.com scoreboard.
    Returns {game_key: {home_team, away_team, home_starters: [names], away_starters: [names]}}
    """
    today     = datetime.now(timezone.utc).strftime("%m/%d/%Y")
    cache_key = f"nba_lineups_{today}"
    cached    = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            "https://stats.nba.com/stats/scoreboardv2",
            params={
                "DayOffset":  "0",
                "LeagueID":   "00",
                "gameDate":   today,
            },
            headers=NBA_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    games = {}

    # Extract game headers
    try:
        game_header_set = next(
            rs for rs in data.get("resultSets", [])
            if rs.get("name") == "GameHeader"
        )
        gh_headers = game_header_set["headers"]
        gh_rows    = game_header_set["rowSet"]
    except (StopIteration, KeyError):
        return {}

    for row in gh_rows:
        gd = dict(zip(gh_headers, row))
        gid = gd.get("GAME_ID", "")
        if not gid:
            continue

        home_team = gd.get("HOME_TEAM_ABBREVIATION", gd.get("HOME_TEAM_ID", ""))
        away_team = gd.get("VISITOR_TEAM_ABBREVIATION", gd.get("VISITOR_TEAM_ID", ""))
        game_key  = f"{away_team} @ {home_team}"

        games[game_key] = {
            "game_id":       gid,
            "home_team":     home_team,
            "away_team":     away_team,
            "home_starters": [],
            "away_starters": [],
        }

    # Try to get starters from LineScore or GameMatchups — these are often
    # not available pre-game via scoreboardv2. Return what we have.
    _cache_set(cache_key, games)
    return games


# ── WNBA Lineups ───────────────────────────────────────────────────────────────

def _fetch_wnba_lineups() -> dict:
    """
    Fetch tonight's WNBA lineups from ESPN scoreboard.
    Returns {game_key: {home_team, away_team, home_starters: [names], away_starters: [names]}}
    """
    cache_key = f"wnba_lineups_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    cached    = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
            headers=ESPN_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    games = {}
    for event in data.get("events", []):
        comps       = event.get("competitions", [{}])[0]
        competitors = comps.get("competitors", [])

        teams = {}
        for c in competitors:
            side = c.get("homeAway", "home")
            t    = c.get("team", {})
            name = t.get("displayName", t.get("shortDisplayName", ""))
            rosters = c.get("roster", {}).get("athletes", [])
            starters = [
                a.get("athlete", {}).get("displayName", "")
                for a in rosters
                if a.get("starter")
            ]
            teams[side] = {"name": name, "starters": starters}

        home = teams.get("home", {})
        away = teams.get("away", {})
        game_key = f"{away.get('name', '')} @ {home.get('name', '')}"

        games[game_key] = {
            "home_team":     home.get("name", ""),
            "away_team":     away.get("name", ""),
            "home_starters": home.get("starters", []),
            "away_starters": away.get("starters", []),
        }

    _cache_set(cache_key, games)
    return games


# ── Public API ─────────────────────────────────────────────────────────────────

def get_all_lineups(sport: str) -> dict:
    """
    Fetch all lineups for the given sport.
    Returns sport-specific dict of game data.

    Errors are caught — returns {} on failure.
    """
    try:
        if sport == "MLB":
            return _fetch_mlb_lineups()
        elif sport == "NBA":
            return _fetch_nba_lineups()
        elif sport == "WNBA":
            return _fetch_wnba_lineups()
    except Exception:
        pass
    return {}


def is_player_starting(
    player_name: str,
    sport: str,
    team: str = "",
) -> bool | None:
    """
    Check if a player is confirmed to be starting tonight.

    Returns:
      True  = confirmed starter
      False = confirmed NOT starting (lineup exists but player absent)
      None  = unknown (no lineup data yet, API error, etc.)

    IMPORTANT: Returns None (not False) when lineups haven't been posted yet.
    Never penalizes a player just because lineup data is unavailable.
    """
    try:
        if sport == "MLB":
            return _is_starting_mlb(player_name, team)
        elif sport == "NBA":
            return _is_starting_nba(player_name, team)
        elif sport == "WNBA":
            return _is_starting_wnba(player_name, team)
    except Exception:
        pass
    return None


def _is_starting_mlb(player_name: str, team: str) -> bool | None:
    lineups = _fetch_mlb_lineups()
    if not lineups:
        return None

    # Search all games for the player
    any_lineup_posted = False
    for game_key, game in lineups.items():
        home_lineup = game.get("home_lineup", [])
        away_lineup = game.get("away_lineup", [])
        all_names   = home_lineup + away_lineup

        if not all_names:
            continue  # lineups not posted for this game yet

        any_lineup_posted = True

        # If team is specified, narrow to that team's lineup
        if team:
            team_last = _last_word(team)
            home_last = _last_word(game.get("home_team", ""))
            away_last = _last_word(game.get("away_team", ""))

            if team_last == home_last:
                all_names = home_lineup
            elif team_last == away_last:
                all_names = away_lineup

        for name in all_names:
            if _name_match(player_name, name):
                return True

        # Only return False if lineups were posted but player not found
        # and the team matches (to avoid false negatives from wrong game)
        if team and any_lineup_posted:
            return False

    # Lineups not yet posted
    if not any_lineup_posted:
        return None

    # Player not found in any posted lineup
    return False


def _is_starting_nba(player_name: str, team: str) -> bool | None:
    lineups = _fetch_nba_lineups()
    if not lineups:
        return None

    for game_key, game in lineups.items():
        home_starters = game.get("home_starters", [])
        away_starters = game.get("away_starters", [])

        if not home_starters and not away_starters:
            # Starters not yet announced (pre-game)
            return None

        all_starters = home_starters + away_starters
        for name in all_starters:
            if _name_match(player_name, name):
                return True

    # If we have starters data but player not in any, return None
    # (could be a bench player or wrong game — don't penalize)
    return None


def _is_starting_wnba(player_name: str, team: str) -> bool | None:
    lineups = _fetch_wnba_lineups()
    if not lineups:
        return None

    any_starters = False
    for game_key, game in lineups.items():
        home_starters = game.get("home_starters", [])
        away_starters = game.get("away_starters", [])

        if home_starters or away_starters:
            any_starters = True

        for name in home_starters + away_starters:
            if _name_match(player_name, name):
                return True

    # If lineup data exists but player not found, return None (not False)
    # WNBA pre-game starters are often unreliable
    return None


# ── MLB-specific convenience functions ─────────────────────────────────────────

def get_mlb_probable_pitcher(home_team: str, away: bool = False) -> str | None:
    """
    Return the probable pitcher name for today's game involving home_team.
    away=True returns the away team's probable pitcher.
    """
    lineups = _fetch_mlb_lineups()
    if not lineups:
        return None

    home_last = _last_word(home_team)
    for game_key, game in lineups.items():
        if _last_word(game.get("home_team", "")) == home_last:
            if away:
                return game.get("away_pitcher") or None
            return game.get("home_pitcher") or None

    return None


def get_mlb_batting_order(team_name: str) -> list[str] | None:
    """
    Return the confirmed batting order for the given team.
    Returns None if lineups haven't been posted yet.
    Returns [] (empty list) if the lineup was posted but is empty (unusual).
    """
    lineups = _fetch_mlb_lineups()
    if not lineups:
        return None

    team_last = _last_word(team_name)
    for game_key, game in lineups.items():
        if _last_word(game.get("home_team", "")) == team_last:
            lineup = game.get("home_lineup")
            return lineup if lineup is not None else None
        if _last_word(game.get("away_team", "")) == team_last:
            lineup = game.get("away_lineup")
            return lineup if lineup is not None else None

    return None
