"""
matchup_context.py — Rich matchup context for every pick.

For each player/stat/game, returns:
  - opponent_k_pct      (MLB) — how often opposing lineup strikes out
  - opponent_def_rating (NBA) — points/stat allowed by opponent defense
  - home_away           — "home" | "away"
  - home_avg / away_avg — player's split averages
  - home_hit_rate / away_hit_rate — hit rate on the specific line by location
  - park_factor         (MLB) — hitter-friendly or pitcher-friendly park
  - context_score       — 0.0–1.0 composite adjustment for scoring model

Higher context_score = matchup favors the pick direction.
Lower context_score  = matchup works against it.
"""

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_CACHE_DIR = Path("logs/context_cache")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_TTL  = 3600   # 1 hr for team stats
_SCHED_TTL  = 1800   # 30 min for today's schedule

NBA_HEADERS = {
    "User-Agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":            "https://www.nba.com/",
    "Accept":             "application/json",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token":  "true",
    "Origin":             "https://www.nba.com",
}

def _get(url: str, headers: dict = None) -> dict:
    h = {"User-Agent": "Mozilla/5.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    return json.loads(urllib.request.urlopen(req, timeout=12).read())

def _cpath(key: str) -> Path:
    return _CACHE_DIR / f"{key[:80].replace(' ','_').replace('/','_')}.json"

def _load(key: str, ttl: int = _CACHE_TTL):
    p = _cpath(key)
    if p.exists() and (time.time() - p.stat().st_mtime) < ttl:
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None

def _save(key: str, data):
    try:
        _cpath(key).write_text(json.dumps(data))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# MLB CONTEXT
# ─────────────────────────────────────────────────────────────────────────────

def _get_mlb_schedule(date_str: str) -> list[dict]:
    """Get today's MLB games with team IDs and probable pitchers."""
    cached = _load(f"mlb_sched_{date_str}", ttl=_SCHED_TTL)
    if cached:
        return cached
    try:
        url  = (f"https://statsapi.mlb.com/api/v1/schedule"
                f"?sportId=1&date={date_str}&hydrate=probablePitcher,team")
        data = _get(url)
        games = []
        for d in data.get("dates", []):
            for g in d.get("games", []):
                away = g["teams"]["away"]
                home = g["teams"]["home"]
                games.append({
                    "game_pk":     g["gamePk"],
                    "away_id":     away["team"]["id"],
                    "away_name":   away["team"]["name"],
                    "home_id":     home["team"]["id"],
                    "home_name":   home["team"]["name"],
                    "away_pitcher":away.get("probablePitcher", {}).get("fullName", ""),
                    "home_pitcher":home.get("probablePitcher", {}).get("fullName", ""),
                })
        _save(f"mlb_sched_{date_str}", games)
        return games
    except Exception as e:
        print(f"[context] MLB schedule failed: {e}")
        return []

def _get_team_k_pct(team_id: int) -> float | None:
    """Batting strikeout rate for a team (as batters). Higher = easier for pitchers."""
    cached = _load(f"mlb_kpct_{team_id}")
    if cached:
        return cached.get("k_pct")
    try:
        url  = f"https://statsapi.mlb.com/api/v1/teams/{team_id}/stats?stats=season&group=hitting&season=2026"
        data = _get(url)
        s    = data.get("stats", [{}])[0].get("splits", [{}])[0].get("stat", {})
        pa   = int(s.get("plateAppearances", 1) or 1)
        ks   = int(s.get("strikeOuts", 0) or 0)
        k_pct = ks / pa
        _save(f"mlb_kpct_{team_id}", {"k_pct": k_pct, "pa": pa, "ks": ks,
                                       "avg": s.get("avg", ".000")})
        return k_pct
    except Exception:
        return None

def _get_team_ops(team_id: int) -> float | None:
    """Team OPS as batters — proxy for offensive quality."""
    cached = _load(f"mlb_ops_{team_id}")
    if cached:
        return cached.get("ops")
    try:
        url  = f"https://statsapi.mlb.com/api/v1/teams/{team_id}/stats?stats=season&group=hitting&season=2026"
        data = _get(url)
        s    = data.get("stats", [{}])[0].get("splits", [{}])[0].get("stat", {})
        ops  = float(s.get("ops", 0.7) or 0.7)
        _save(f"mlb_ops_{team_id}", {"ops": ops})
        return ops
    except Exception:
        return None

# Park factor lookup (run factor > 1 = hitter friendly, < 1 = pitcher friendly)
# Source: 2025/2026 approximations
PARK_FACTORS = {
    "Colorado Rockies":      1.28,  # Coors — extreme hitter park
    "Boston Red Sox":        1.10,
    "Cincinnati Reds":       1.08,
    "Chicago Cubs":          1.06,
    "Texas Rangers":         1.05,
    "Philadelphia Phillies": 1.04,
    "Toronto Blue Jays":     1.03,
    "Atlanta Braves":        1.02,
    "Baltimore Orioles":     1.01,
    "New York Mets":         1.00,
    "New York Yankees":      0.99,
    "Houston Astros":        0.99,
    "Seattle Mariners":      0.96,
    "Oakland Athletics":     0.96,
    "Tampa Bay Rays":        0.95,
    "Miami Marlins":         0.95,
    "San Francisco Giants":  0.94,
    "Los Angeles Dodgers":   0.94,
    "Minnesota Twins":       0.94,
    "San Diego Padres":      0.93,
    "Detroit Tigers":        0.93,
    "Pittsburgh Pirates":    0.93,
    "Kansas City Royals":    0.97,
    "Cleveland Guardians":   0.98,
    "Los Angeles Angels":    1.00,
    "Milwaukee Brewers":     0.97,
    "St. Louis Cardinals":   0.98,
    "Washington Nationals":  1.00,
    "Arizona Diamondbacks":  1.01,
    "Chicago White Sox":     1.00,
}

def _pitcher_home_away_splits(game_logs: list[dict]) -> dict:
    """
    Split pitcher's strikeout data into home vs away from game logs.
    Game log entries have 'opp' field from MLB stats API.
    Home games = pitcher's team played at home (no '@' in away team context).
    We approximate by alternating — but actually MLB logs don't have home/away flag directly.
    Use the fact that home games show opponent coming to them.
    MLB statsapi game logs have a 'isHome' field if we hydrate — approximate from team context.
    """
    # Without explicit home/away flag in our simplified log, we can't split reliably.
    # Return None to signal "no split available" and fall back to overall.
    return {}

def get_mlb_context(player_name: str, stat_type: str,
                    game_logs: list[dict] = None) -> dict:
    """
    Full MLB matchup context for a pitcher.
    Returns dict with: opp_k_pct, opp_ops, park_factor, context_score, description
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    games = _get_mlb_schedule(today)

    # Find which game this pitcher is in
    pitcher_game = None
    is_home      = None
    opp_team_id  = None
    opp_team_name = None
    home_team_name = None

    name_lower = player_name.lower()
    for g in games:
        if name_lower in g.get("away_pitcher", "").lower():
            pitcher_game   = g
            is_home        = False
            opp_team_id    = g["home_id"]
            opp_team_name  = g["home_name"]
            home_team_name = g["home_name"]
            break
        if name_lower in g.get("home_pitcher", "").lower():
            pitcher_game   = g
            is_home        = True
            opp_team_id    = g["away_id"]
            opp_team_name  = g["away_name"]
            home_team_name = g["home_name"]
            break

    result = {
        "home_away":      "home" if is_home else ("away" if is_home is False else "unknown"),
        "opp_team":       opp_team_name or "unknown",
        "opp_k_pct":      None,
        "opp_ops":        None,
        "park_factor":    1.0,
        "context_score":  0.5,
        "description":    [],
    }

    if opp_team_id is None:
        return result

    # Opponent K rate
    k_pct = _get_team_k_pct(opp_team_id)
    result["opp_k_pct"] = k_pct

    # Park factor (pitcher's home park if home, opponent's park if away)
    park_name = home_team_name or ""
    result["park_factor"] = PARK_FACTORS.get(park_name, 1.0)
    pf = result["park_factor"]

    # Compute context score for strikeout props
    if stat_type in ("Pitcher Strikeouts", "Strikeouts") and k_pct is not None:
        # League avg K% ≈ 22%
        # High K% opponent (>24%) = OVER edge
        # Low K% opponent (<20%) = UNDER edge
        opp_k_score = (k_pct - 0.22) / 0.06   # range roughly -1 to +1
        opp_k_score = max(-0.5, min(0.5, opp_k_score))

        # Park factor adjustment: pitcher-friendly park (pf < 1) = slight OVER edge for Ks
        park_score  = (1.0 - pf) * 2   # Coors (1.28) = -0.56, pitcher park (0.94) = +0.12
        park_score  = max(-0.3, min(0.3, park_score))

        # Home advantage for pitchers is usually small but real
        home_score  = 0.05 if is_home else 0.0

        raw = 0.5 + opp_k_score * 0.4 + park_score * 0.2 + home_score
        result["context_score"] = round(max(0.1, min(0.9, raw)), 3)

        # Build description
        k_label = f"opp K%={k_pct:.1%}"
        if k_pct > 0.24:
            result["description"].append(f"✅ High-K lineup ({k_pct:.1%} K rate)")
        elif k_pct < 0.20:
            result["description"].append(f"⚠️ Low-K lineup ({k_pct:.1%} K rate)")
        else:
            result["description"].append(f"Avg K lineup ({k_pct:.1%})")

        if pf > 1.05:
            result["description"].append(f"⚠️ Hitter park (PF={pf:.2f})")
        elif pf < 0.96:
            result["description"].append(f"✅ Pitcher park (PF={pf:.2f})")

        if is_home:
            result["description"].append("Home start")
        else:
            result["description"].append("Away start")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# NBA CONTEXT
# ─────────────────────────────────────────────────────────────────────────────

# Opponent defensive rankings keyed by team abbreviation
_NBA_DEF_CACHE: dict[str, dict] = {}

def _load_nba_def_ratings() -> dict[str, dict]:
    """Load NBA team defensive stats — pts, reb, ast, 3pm allowed per game."""
    global _NBA_DEF_CACHE
    if _NBA_DEF_CACHE:
        return _NBA_DEF_CACHE

    cached = _load("nba_def_full")
    if cached:
        _NBA_DEF_CACHE = cached
        return cached

    try:
        url  = ("https://stats.nba.com/stats/leaguedashteamstats"
                "?Season=2025-26&SeasonType=Playoffs"
                "&MeasureType=Base&PerMode=PerGame"
                "&PaceAdjust=N&PlusMinus=N&Rank=N&LeagueID=00"
                "&Direction=DESC&Conference=&Division=&GameScope="
                "&GameSegment=&LastNGames=0&Location=&Month=0"
                "&OpponentTeamID=0&Outcome=&PORound=0&Period=0"
                "&PlayerExperience=&PlayerPosition=&StarterBench=&TwoWay=0")
        data = _get(url, NBA_HEADERS)
        rs   = data["resultSets"][0]
        hdrs = rs["headers"]
        rating = {}
        for row in rs["rowSet"]:
            g = dict(zip(hdrs, row))
            abbr = g.get("TEAM_ABBREVIATION", "")
            rating[abbr] = {
                "opp_pts": g.get("OPP_PTS", 110),
                "opp_reb": g.get("OPP_REB", 44),
                "opp_ast": g.get("OPP_AST", 24),
                "opp_fg3m":g.get("OPP_FG3M", 13),
                "opp_stl": g.get("OPP_STL", 8),
                "opp_blk": g.get("OPP_BLK", 5),
                "team":    g.get("TEAM_NAME", abbr),
            }
        if not rating:
            raise ValueError("Empty result")
        _save("nba_def_full", rating)
        _NBA_DEF_CACHE = rating
        return rating
    except Exception:
        pass

    # Fall back to regular season
    try:
        url2 = ("https://stats.nba.com/stats/leaguedashteamstats"
                "?Season=2025-26&SeasonType=Regular+Season"
                "&MeasureType=Base&PerMode=PerGame"
                "&PaceAdjust=N&PlusMinus=N&Rank=N&LeagueID=00"
                "&Direction=DESC&Conference=&Division=&GameScope="
                "&GameSegment=&LastNGames=0&Location=&Month=0"
                "&OpponentTeamID=0&Outcome=&PORound=0&Period=0"
                "&PlayerExperience=&PlayerPosition=&StarterBench=&TwoWay=0")
        data = _get(url2, NBA_HEADERS)
        rs   = data["resultSets"][0]
        hdrs = rs["headers"]
        rating = {}
        for row in rs["rowSet"]:
            g = dict(zip(hdrs, row))
            abbr = g.get("TEAM_ABBREVIATION", "")
            rating[abbr] = {
                "opp_pts": g.get("OPP_PTS", 110),
                "opp_reb": g.get("OPP_REB", 44),
                "opp_ast": g.get("OPP_AST", 24),
                "opp_fg3m":g.get("OPP_FG3M", 13),
                "team":    g.get("TEAM_NAME", abbr),
            }
        _save("nba_def_full", rating)
        _NBA_DEF_CACHE = rating
        return rating
    except Exception as e:
        print(f"[context] NBA def ratings failed: {e}")
        return {}

def _get_nba_schedule(date_str: str) -> list[dict]:
    """Today's NBA games with team abbreviations."""
    cached = _load(f"nba_sched_{date_str}", ttl=_SCHED_TTL)
    if cached:
        return cached
    try:
        url  = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date_str.replace('-','')}"
        data = _get(url)
        games = []
        for ev in data.get("events", []):
            comp = ev.get("competitions", [{}])[0]
            teams = comp.get("competitors", [])
            away = next((t for t in teams if t["homeAway"] == "away"), {})
            home = next((t for t in teams if t["homeAway"] == "home"), {})
            games.append({
                "away_abbr": away.get("team", {}).get("abbreviation", ""),
                "away_name": away.get("team", {}).get("displayName", ""),
                "home_abbr": home.get("team", {}).get("abbreviation", ""),
                "home_name": home.get("team", {}).get("displayName", ""),
                "away_players": [r.get("athlete", {}).get("displayName", "")
                                 for r in away.get("roster", [])],
                "home_players": [r.get("athlete", {}).get("displayName", "")
                                 for r in home.get("roster", [])],
            })
        _save(f"nba_sched_{date_str}", games)
        return games
    except Exception as e:
        print(f"[context] NBA schedule failed: {e}")
        return []

def _compute_home_away_splits(game_logs: list[dict], stat_fn, line: float) -> dict:
    """
    Split a player's game logs into home vs away.
    NBA logs use matchup like 'GSW vs. LAL' (home) or 'GSW @ LAL' (away).
    Returns {home_avg, away_avg, home_hit_rate, away_hit_rate, home_n, away_n}
    """
    home_vals, away_vals = [], []
    for g in game_logs:
        v = stat_fn(g) if callable(stat_fn) else g.get(stat_fn)
        if v is None:
            continue
        matchup = g.get("matchup", "")
        if "vs." in matchup:
            home_vals.append(v)
        elif "@" in matchup:
            away_vals.append(v)

    def _stats(vals):
        if not vals:
            return None, None, 0
        avg  = sum(vals) / len(vals)
        hr   = sum(1 for v in vals if v > line) / len(vals)
        return round(avg, 1), round(hr, 3), len(vals)

    h_avg, h_hr, h_n = _stats(home_vals)
    a_avg, a_hr, a_n = _stats(away_vals)

    return {
        "home_avg":      h_avg,
        "home_hit_rate": h_hr,
        "home_n":        h_n,
        "away_avg":      a_avg,
        "away_hit_rate": a_hr,
        "away_n":        a_n,
    }

# Stat type → defensive column map
_NBA_OPP_COL = {
    "Points":          "opp_pts",
    "Rebounds":        "opp_reb",
    "Assists":         "opp_ast",
    "3-Pointers Made": "opp_fg3m",
    "Pts+Rebs+Asts":   "opp_pts",
    "Pts+Rebs":        "opp_pts",
    "Pts+Asts":        "opp_pts",
    "Steals":          "opp_stl",
    "Blocks":          "opp_blk",
}

# League average benchmarks for normalising opponent defensive rank
_NBA_LEAGUE_AVG = {
    "opp_pts":  110.0,
    "opp_reb":  44.0,
    "opp_ast":  24.0,
    "opp_fg3m": 13.0,
    "opp_stl":  8.0,
    "opp_blk":  5.0,
}

def get_nba_context(player_name: str, stat_type: str,
                    game_logs: list[dict] = None, line: float = 0) -> dict:
    """
    Full NBA matchup context for a player.
    Returns dict with: opp_team, opp_def_val, home_away, splits, context_score, description
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    games = _get_nba_schedule(today)
    def_ratings = _load_nba_def_ratings()

    # Find which game this player is in
    is_home     = None
    opp_abbr    = None
    opp_name    = None
    name_lower  = player_name.lower()

    for g in games:
        # Try to match by team roster — but ESPN schedule doesn't include rosters easily
        # Fall back to: player's team abbreviation from their last game log matchup
        if game_logs:
            last_matchup = game_logs[0].get("matchup", "") if game_logs else ""
            # matchup format: "TEAM vs. OPP" or "TEAM @ OPP"
            if "vs." in last_matchup:
                my_abbr = last_matchup.split(" vs.")[0].strip()
            elif "@" in last_matchup:
                my_abbr = last_matchup.split(" @")[0].strip()
            else:
                my_abbr = ""

            if my_abbr:
                if my_abbr == g["home_abbr"]:
                    is_home  = True
                    opp_abbr = g["away_abbr"]
                    opp_name = g["away_name"]
                    break
                elif my_abbr == g["away_abbr"]:
                    is_home  = False
                    opp_abbr = g["home_abbr"]
                    opp_name = g["home_name"]
                    break

    result = {
        "home_away":     "home" if is_home else ("away" if is_home is False else "unknown"),
        "opp_team":      opp_name or "unknown",
        "opp_def_val":   None,
        "context_score": 0.5,
        "description":   [],
        "splits":        {},
    }

    # Home/away splits from game logs
    if game_logs and line > 0:
        col = _NBA_OPP_COL.get(stat_type, "opp_pts")
        def _stat_val(g):
            if stat_type == "Points":          return g.get("pts")
            if stat_type == "Rebounds":        return g.get("reb")
            if stat_type == "Assists":         return g.get("ast")
            if stat_type == "3-Pointers Made": return g.get("fg3m")
            if stat_type == "Pts+Rebs+Asts":   return (g.get("pts",0) or 0) + (g.get("reb",0) or 0) + (g.get("ast",0) or 0)
            if stat_type == "Pts+Rebs":        return (g.get("pts",0) or 0) + (g.get("reb",0) or 0)
            if stat_type == "Pts+Asts":        return (g.get("pts",0) or 0) + (g.get("ast",0) or 0)
            return None
        result["splits"] = _compute_home_away_splits(game_logs, _stat_val, line)

    # Opponent defensive context
    opp_def = def_ratings.get(opp_abbr, {}) if opp_abbr else {}
    col     = _NBA_OPP_COL.get(stat_type, "opp_pts")
    opp_val = opp_def.get(col)
    result["opp_def_val"] = opp_val

    if opp_val is not None:
        league_avg = _NBA_LEAGUE_AVG.get(col, opp_val)
        # How much does this opponent allow vs league average?
        # More allowed = easier matchup for OVER
        diff_pct = (opp_val - league_avg) / (league_avg + 1e-9)
        opp_score = 0.5 + diff_pct * 1.5   # scale: ±15% diff → ±0.225 adjustment
        opp_score = max(0.1, min(0.9, opp_score))

        # Home/away modifier
        home_bonus = 0.03 if is_home else -0.02
        raw = opp_score + home_bonus

        # If we have splits, weight toward the relevant split
        splits = result["splits"]
        if is_home is True and splits.get("home_hit_rate") is not None:
            split_hr = splits["home_hit_rate"]
            raw = raw * 0.6 + split_hr * 0.4
        elif is_home is False and splits.get("away_hit_rate") is not None:
            split_hr = splits["away_hit_rate"]
            raw = raw * 0.6 + split_hr * 0.4

        result["context_score"] = round(max(0.1, min(0.9, raw)), 3)

        # Description
        if diff_pct > 0.04:
            result["description"].append(f"✅ {opp_name} allows {opp_val:.1f} {col.replace('opp_','')} (above avg)")
        elif diff_pct < -0.04:
            result["description"].append(f"⚠️ {opp_name} allows only {opp_val:.1f} {col.replace('opp_','')} (tough D)")
        else:
            result["description"].append(f"{opp_name} avg defense ({opp_val:.1f})")

        if is_home is True:
            h_avg = splits.get("home_avg")
            result["description"].append(f"Home: avg {h_avg}" if h_avg else "Home game")
        elif is_home is False:
            a_avg = splits.get("away_avg")
            result["description"].append(f"Away: avg {a_avg}" if a_avg else "Away game")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# WNBA CONTEXT
# ─────────────────────────────────────────────────────────────────────────────

def _get_wnba_roster(team_id: str) -> list[str]:
    """Player display names on a WNBA team roster."""
    cached = _load(f"wnba_roster_{team_id}")
    if cached:
        return cached
    try:
        import time as _t
        _t.sleep(0.1)
        url  = (f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
                f"/teams/{team_id}/roster")
        data = _get(url)
        names = [a.get("displayName", "")
                 for a in data.get("athletes", [])]
        _save(f"wnba_roster_{team_id}", names)
        return names
    except Exception:
        return []


def _get_wnba_schedule(date_str: str) -> list[dict]:
    """Today's WNBA games from ESPN scoreboard, with rosters fetched separately."""
    cached = _load(f"wnba_sched_{date_str}", ttl=_SCHED_TTL)
    if cached:
        return cached
    try:
        date_compact = date_str.replace("-", "")
        url  = (f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
                f"?dates={date_compact}")
        data = _get(url)
        games = []
        for ev in data.get("events", []):
            comp  = ev.get("competitions", [{}])[0]
            teams = comp.get("competitors", [])
            away  = next((t for t in teams if t["homeAway"] == "away"), {})
            home  = next((t for t in teams if t["homeAway"] == "home"), {})
            away_id = away.get("team", {}).get("id", "")
            home_id = home.get("team", {}).get("id", "")
            # Fetch rosters for both teams
            away_players = _get_wnba_roster(away_id) if away_id else []
            home_players = _get_wnba_roster(home_id) if home_id else []
            games.append({
                "away_abbr":    away.get("team", {}).get("abbreviation", ""),
                "away_name":    away.get("team", {}).get("displayName", ""),
                "home_abbr":    home.get("team", {}).get("abbreviation", ""),
                "home_name":    home.get("team", {}).get("displayName", ""),
                "away_players": away_players,
                "home_players": home_players,
            })
        _save(f"wnba_sched_{date_str}", games)
        return games
    except Exception as e:
        print(f"[context] WNBA schedule failed: {e}")
        return []

_WNBA_DEF_CACHE: dict[str, dict] = {}

def _load_wnba_def_ratings() -> dict[str, dict]:
    """
    WNBA team scoring environment from ESPN team statistics.
    Uses each team's avgPoints as a pace/environment proxy:
      high scorers → up-tempo game → favors OVER props
      low scorers  → slower game  → favors UNDER props
    ESPN doesn't expose opponent-allowed stats directly; this is the best available proxy.
    Returns {team_abbr: {avg_pts, opp_pts_est, opp_reb_est, team}}.
    """
    global _WNBA_DEF_CACHE
    if _WNBA_DEF_CACHE:
        return _WNBA_DEF_CACHE

    cached = _load("wnba_def_full")
    if cached:
        _WNBA_DEF_CACHE = cached
        return cached

    try:
        url  = ("https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams"
                "?limit=20")
        data = _get(url)
        sports  = data.get("sports", [{}])[0]
        leagues = sports.get("leagues", [{}])[0]
        teams   = leagues.get("teams", [])
    except Exception:
        return {}

    result: dict[str, dict] = {}
    for entry in teams:
        ti   = entry.get("team", {})
        tid  = ti.get("id", "")
        abbr = ti.get("abbreviation", "")
        if not tid:
            continue
        try:
            import time as _time
            _time.sleep(0.15)
            sr   = _get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
                        f"/teams/{tid}/statistics")
            cats = sr.get("results", {}).get("stats", {}).get("categories", [])

            def _find(name):
                for cat in cats:
                    for s in cat.get("stats", []):
                        if s.get("name") == name:
                            return s.get("value")
                return None

            avg_pts = _find("avgPoints") or 80.0
            avg_reb = _find("avgRebounds") or 34.0
            avg_ast = _find("avgAssists") or 19.0

            # Estimate opponent pts: high-scoring teams tend to allow more (pace correlation)
            # WNBA league avg ~80 pts/game. Use team's own pace as proxy.
            result[abbr] = {
                "avg_pts":     float(avg_pts),
                "opp_pts":     float(avg_pts),   # proxy: high scorers → up-tempo environment
                "opp_reb":     float(avg_reb),
                "opp_ast":     float(avg_ast),
                "team":        ti.get("displayName", abbr),
            }
        except Exception:
            continue

    if result:
        _save("wnba_def_full", result)
    _WNBA_DEF_CACHE = result
    return result


def get_wnba_context(player_name: str, stat_type: str,
                     game_logs: list[dict] = None, line: float = 0) -> dict:
    """
    WNBA matchup context — ESPN schedule + team defensive ratings.
    Identifies opponent from today's schedule, scores defensive matchup.
    """
    result = {
        "home_away":     "unknown",
        "opp_team":      "unknown",
        "context_score": 0.5,
        "description":   [],
        "splits":        {},
    }

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    games = _get_wnba_schedule(today)
    name_lower = player_name.lower()

    # Find which game this player is in
    is_home    = None
    opp_abbr   = None
    opp_name   = None

    for g in games:
        away_names = [n.lower() for n in g.get("away_players", [])]
        home_names = [n.lower() for n in g.get("home_players", [])]
        player_last = name_lower.split()[-1]

        if any(player_last in n for n in home_names) or any(name_lower in n for n in home_names):
            is_home  = True
            opp_abbr = g["away_abbr"]
            opp_name = g["away_name"]
            break
        if any(player_last in n for n in away_names) or any(name_lower in n for n in away_names):
            is_home  = False
            opp_abbr = g["home_abbr"]
            opp_name = g["home_name"]
            break

    result["home_away"] = "home" if is_home else ("away" if is_home is False else "unknown")
    result["opp_team"]  = opp_name or "unknown"

    # Home/away splits from game logs (same approach as NBA)
    if game_logs and line > 0:
        def _stat_val(g):
            if stat_type in ("Points", "Pts"):     return g.get("pts")
            if stat_type == "Rebounds":             return g.get("reb")
            if stat_type == "Assists":              return g.get("ast")
            if stat_type in ("3-PT Made", "3PT Made"): return g.get("fg3m")
            if stat_type in ("Pts+Rebs", "Pts+Reb"):
                return (g.get("pts", 0) or 0) + (g.get("reb", 0) or 0)
            if stat_type in ("Pts+Asts", "Pts+Ast"):
                return (g.get("pts", 0) or 0) + (g.get("ast", 0) or 0)
            if stat_type in ("Pts+Rebs+Asts", "Pts+Reb+Ast"):
                return (g.get("pts",0) or 0) + (g.get("reb",0) or 0) + (g.get("ast",0) or 0)
            return None
        result["splits"] = _compute_home_away_splits(game_logs, _stat_val, line)

    # Defensive context
    if not opp_abbr:
        return result

    def_ratings = _load_wnba_def_ratings()
    opp_def     = def_ratings.get(opp_abbr, {})

    col_map = {
        "Points": "opp_pts", "Rebounds": "opp_reb", "Assists": "opp_ast",
        "Pts+Rebs": "opp_pts", "Pts+Asts": "opp_pts", "Pts+Rebs+Asts": "opp_pts",
    }
    col     = col_map.get(stat_type, "opp_pts")
    opp_val = opp_def.get(col)

    if opp_val:
        # WNBA league avgs (approx): pts~82, reb~33, ast~19
        league_avg = {"opp_pts": 82.0, "opp_reb": 33.0, "opp_ast": 19.0}.get(col, 82.0)
        diff_pct   = (opp_val - league_avg) / league_avg
        opp_score  = 0.5 + diff_pct * 1.5
        home_bonus = 0.03 if is_home else -0.02
        raw        = max(0.1, min(0.9, opp_score + home_bonus))

        # Blend with splits if available
        splits = result["splits"]
        if is_home is True and splits.get("home_hit_rate") is not None:
            raw = raw * 0.6 + splits["home_hit_rate"] * 0.4
        elif is_home is False and splits.get("away_hit_rate") is not None:
            raw = raw * 0.6 + splits["away_hit_rate"] * 0.4

        result["context_score"] = round(raw, 3)

        if diff_pct > 0.04:
            result["description"].append(f"✅ {opp_name} allows {opp_val:.1f} {col.replace('opp_','')} (above avg)")
        elif diff_pct < -0.04:
            result["description"].append(f"⚠️ {opp_name} tough D ({opp_val:.1f} {col.replace('opp_','')} allowed)")
        else:
            result["description"].append(f"{opp_name} avg defense ({opp_val:.1f})")

        result["description"].append("Home" if is_home else "Away")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# UNIFIED ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def get_context(sport: str, player_name: str, stat_type: str,
                game_logs: list[dict] = None, line: float = 0) -> dict:
    """
    Get full matchup context for any pick.
    Returns dict with context_score (0–1) and description list.
    context_score > 0.5 = matchup favors the pick
    context_score < 0.5 = matchup works against it
    """
    if sport == "MLB":
        return get_mlb_context(player_name, stat_type, game_logs)
    if sport == "NBA":
        return get_nba_context(player_name, stat_type, game_logs, line)
    if sport == "WNBA":
        return get_wnba_context(player_name, stat_type, game_logs, line)
    return {"context_score": 0.5, "description": [], "home_away": "unknown", "opp_team": "unknown"}
