"""
WNBA stats client using ESPN APIs (no API key required).

Provides per-player recent game log with full metadata:
  - game_log: list of {value, minutes, date, opponent, home_away} most-recent first
  - game_values: flat list of stat values for _compute_stats
  - H2H vs today's opponent
  - Home/away splits from player's own game log
  - Opponent defensive stats (rebounds/assists/points allowed)

Data source: ESPN athlete gamelog API.
Fetches current season (2026) first, supplements with 2025 for sample size.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ESPN_HEADERS     = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
CACHE_PATH       = Path("logs/.wnba_player_cache.json")
STATS_CACHE_PATH = Path("logs/.wnba_stats_cache.json")
STATS_CACHE_TTL  = 3600  # 1 hour (shorter — current season games update frequently)

ESPN_TEAMS_URL   = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams"
ESPN_ROSTER_URL  = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/roster"
ESPN_GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/wnba/athletes/{athlete_id}/gamelog"
ESPN_TEAM_STATS  = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/statistics"

# Stat label -> ESPN column
STAT_COL: dict[str, str] = {
    "Points":    "PTS",
    "Rebounds":  "REB",
    "Assists":   "AST",
    "3-PT Made": "3PT",
    "Steals":    "STL",
    "Blocks":    "BLK",
    "Turnovers": "TO",
}

COMBINED_STAT_COLS: dict[str, list[str]] = {
    "Pts+Rebs":      ["PTS", "REB"],
    "Pts+Asts":      ["PTS", "AST"],
    "Pts+Rebs+Asts": ["PTS", "REB", "AST"],
    "Rebs+Asts":     ["REB", "AST"],
    "Stls+Blks":     ["STL", "BLK"],
}

# Which ESPN team stat name maps to each stat type (for opp defensive context)
OPP_DEF_STAT: dict[str, str] = {
    "Points":         "avgPointsAllowed",
    "Rebounds":       "avgReboundsAllowed",
    "Assists":        "avgAssistsAllowed",
    "3-PT Made":      "avg3PtMadeAllowed",
    "Pts+Rebs":       "avgPointsAllowed",
    "Pts+Asts":       "avgPointsAllowed",
    "Pts+Rebs+Asts":  "avgPointsAllowed",
    "Rebs+Asts":      "avgReboundsAllowed",
}

MIN_BUMP_PCT = 0.15
MIN_DROP_PCT = 0.15

# Current and prior WNBA seasons to fetch
CURRENT_SEASON = "2026"
PRIOR_SEASON   = "2025"


# ── Cache helpers ─────────────────────────────────────────────────────────────

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


# ── Player ID lookup ──────────────────────────────────────────────────────────

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

    try:
        resp = requests.get(ESPN_TEAMS_URL, headers=ESPN_HEADERS, timeout=10)
        resp.raise_for_status()
        data    = resp.json()
        teams   = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
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
                headers=ESPN_HEADERS, timeout=10,
            )
            r.raise_for_status()
            for athlete in r.json().get("athletes", []):
                name = athlete.get("fullName", athlete.get("displayName", "")).strip()
                aid  = athlete.get("id", "")
                if name and aid:
                    players[name.lower()] = str(aid)
        except Exception:
            continue
        time.sleep(0.15)

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


# ── Stat parsing ──────────────────────────────────────────────────────────────

def _parse_stat_val(raw, col: str) -> float | None:
    if raw is None:
        return None
    try:
        if col in ("FG", "3PT", "FT") and "-" in str(raw):
            return float(str(raw).split("-")[0])
        return float(raw)
    except (ValueError, TypeError):
        return None

def _parse_minutes(raw) -> float:
    if raw is None:
        return 0.0
    try:
        if ":" in str(raw):
            parts = str(raw).split(":")
            return float(parts[0]) + float(parts[1]) / 60
        return float(raw)
    except (ValueError, IndexError):
        return 0.0


# ── Gamelog fetch ─────────────────────────────────────────────────────────────

def _fetch_gamelog_raw(athlete_id: str, season: str) -> tuple[list[dict], list[str], dict]:
    """
    Returns (events_list, labels, events_meta_dict).
    events_list: [{eventId, stats}] oldest-first as returned by ESPN.
    events_meta_dict: {eventId: {opponent, home_away, date, ...}} from top-level events.
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
        return [], [], {}

    labels     = data.get("labels", [])
    events_raw = data.get("events", {})   # top-level dict: eventId -> metadata

    # Build metadata lookup
    events_meta: dict[str, dict] = {}
    for eid, emeta in (events_raw.items() if isinstance(events_raw, dict) else []):
        opp       = emeta.get("opponent", {})
        opp_name  = opp.get("displayName", "")
        at_vs     = emeta.get("atVs", "vs")    # "vs" = home, "@" = away
        game_date = emeta.get("gameDate", "")
        events_meta[str(eid)] = {
            "opponent":  opp_name,
            "home_away": "home" if at_vs == "vs" else "away",
            "date":      game_date[:10] if game_date else "",
        }

    # Flatten all game events across season types
    events_flat: list[dict] = []
    for stype in data.get("seasonTypes", []):
        for cat in stype.get("categories", []):
            for ev in cat.get("events", []):
                # Only include Regular Season + Playoffs (skip preseason)
                stype_name = stype.get("displayName", "")
                if "Preseason" in stype_name:
                    continue
                events_flat.append(ev)

    return events_flat, labels, events_meta


def _build_game_log(
    events_flat: list[dict],
    labels: list[str],
    events_meta: dict,
    fetch_cols: list[str],
) -> list[dict]:
    """
    Build a game log list (most-recent first) with stat value + metadata.
    Each entry: {value, minutes, date, opponent, home_away}
    """
    game_log: list[dict] = []

    for event in reversed(events_flat):   # reversed = most-recent first
        eid       = str(event.get("eventId", ""))
        raw_stats = event.get("stats", [])
        game      = dict(zip(labels, raw_stats))
        meta      = events_meta.get(eid, {})

        mins = _parse_minutes(game.get("MIN"))
        if mins <= 0:
            continue

        try:
            val = sum(_parse_stat_val(game.get(c), c) or 0.0 for c in fetch_cols)
        except Exception:
            continue

        # FGA as usage proxy (FG column = "made-att")
        fga = 0.0
        fg_raw = game.get("FG")
        if fg_raw and "-" in str(fg_raw):
            try:
                fga = float(str(fg_raw).split("-")[1])
            except Exception:
                pass

        game_log.append({
            "value":     val,
            "minutes":   mins,
            "fga":       fga,
            "date":      meta.get("date", ""),
            "opponent":  meta.get("opponent", ""),
            "home_away": meta.get("home_away", "unknown"),
        })

    return game_log


# ── Opponent defensive stats ──────────────────────────────────────────────────

_OPP_DEF_CACHE: dict[str, dict] = {}

def _get_opp_def_stats(opp_team_name: str, stat_type: str) -> dict:
    """
    Fetch opponent team's defensive averages from ESPN.
    Returns {avg_allowed, league_avg, is_favorable} for the given stat_type.
    """
    global _OPP_DEF_CACHE

    if not opp_team_name:
        return {}

    # Load team IDs once
    try:
        resp = requests.get(ESPN_TEAMS_URL, headers=ESPN_HEADERS, timeout=10)
        teams = resp.json().get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    except Exception:
        return {}

    # Find team ID
    team_id = None
    opp_lower = opp_team_name.lower()
    for t in teams:
        tdata = t.get("team", {})
        if (opp_lower in tdata.get("displayName", "").lower() or
                opp_lower in tdata.get("name", "").lower() or
                tdata.get("name", "").lower() in opp_lower):
            team_id = tdata.get("id")
            break

    if not team_id:
        return {}

    cache_key = f"def_{team_id}_{stat_type}"
    if cache_key in _OPP_DEF_CACHE:
        return _OPP_DEF_CACHE[cache_key]

    try:
        resp = requests.get(
            ESPN_TEAM_STATS.format(team_id=team_id),
            headers=ESPN_HEADERS, timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    # ESPN team stats: navigate results.stats.categories[].stats[]
    # We look for relevant defensive stats
    stat_map = {
        "Points":   ("Scoring", "avgPointsAllowed", 82.0),
        "Rebounds": ("Rebounding", "avgReboundsAllowed", 33.0),
        "Assists":  ("General", "avgAssistsAllowed", 20.0),
        "3-PT Made":("General", "avg3PMAllowed", 7.0),
        "Pts+Rebs": ("Scoring", "avgPointsAllowed", 82.0),
        "Pts+Asts": ("Scoring", "avgPointsAllowed", 82.0),
        "Pts+Rebs+Asts": ("Scoring", "avgPointsAllowed", 82.0),
        "Rebs+Asts": ("Rebounding", "avgReboundsAllowed", 33.0),
    }

    target = stat_map.get(stat_type, ("Scoring", "avgPointsAllowed", 82.0))
    cat_name, stat_name, league_avg = target

    # Find stat in categories
    categories = (data.get("results", {}).get("stats", {}).get("categories", [])
                  or data.get("stats", {}).get("categories", []))
    allowed_avg = None
    for cat in categories:
        for s in cat.get("stats", []):
            name = s.get("name", "")
            # Try to find points/rebounds allowed by looking at opponent-related stats
            if "allow" in name.lower() or "opponent" in name.lower() or "opp" in name.lower():
                if any(kw in name.lower() for kw in ["point", "pts", "score"]) and "Point" in cat_name:
                    allowed_avg = s.get("value") or s.get("displayValue")
                elif any(kw in name.lower() for kw in ["reb"]) and "Reb" in cat_name:
                    allowed_avg = s.get("value") or s.get("displayValue")
                elif any(kw in name.lower() for kw in ["ast", "assist"]) and "Assist" in cat_name:
                    allowed_avg = s.get("value") or s.get("displayValue")

    # Fallback: use team's own avgPoints as pace proxy (high-scoring teams = more volume for opponents)
    if allowed_avg is None:
        for cat in categories:
            for s in cat.get("stats", []):
                if s.get("name", "") in ("avgPoints", "pointsPerGame"):
                    allowed_avg = s.get("value") or s.get("displayValue")
                    break

    result = {}
    if allowed_avg is not None:
        try:
            avg = float(str(allowed_avg).replace(",", ""))
            result = {
                "avg_allowed":  avg,
                "league_avg":   league_avg,
                "is_favorable": avg > league_avg,  # opponent allows more = favorable for OVER
            }
        except (ValueError, TypeError):
            pass

    _OPP_DEF_CACHE[cache_key] = result
    return result


# ── Main entry point ──────────────────────────────────────────────────────────

def get_player_stats(
    player_name: str,
    stat_type: str,
    opp_team: str = "",
) -> dict | None:
    """
    Fetch WNBA game log via ESPN and return stat context with full matchup data.

    Fetches current season (2026) first, supplements with 2025 for sample size.
    Returns:
      game_log:       list of {value, minutes, date, opponent, home_away} most-recent first
      game_values:    flat list of values for _compute_stats
      h2h:            stats vs today's specific opponent (if opp_team provided)
      home_splits:    {avg, hit_rate, n} for home games
      away_splits:    {avg, hit_rate, n} for away games
      opp_def:        {avg_allowed, is_favorable} opponent defensive context
    """
    col  = STAT_COL.get(stat_type)
    cols = COMBINED_STAT_COLS.get(stat_type)
    if not col and not cols:
        return None
    fetch_cols = cols if cols else [col]

    # Cache key includes opp_team so H2H is cached per matchup
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache_key = f"{player_name.lower()}|{stat_type}|{opp_team.lower()}|{today_str}"
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

    # Fetch current season first, then prior season for more data
    game_log: list[dict] = []
    for season in [CURRENT_SEASON, PRIOR_SEASON]:
        events_flat, labels, events_meta = _fetch_gamelog_raw(athlete_id, season)
        if events_flat and labels:
            season_log = _build_game_log(events_flat, labels, events_meta, fetch_cols)
            game_log.extend(season_log)

    if not game_log:
        return None

    # ── Core stats from game log ───────────────────────────────────────────────
    all_vals = [g["value"]   for g in game_log]
    all_mins = [g["minutes"] for g in game_log]
    n        = len(all_vals)

    season_avg = sum(all_vals) / n
    season_min = sum(all_mins) / n

    # Filter out garbage-time / rest games (< 60% of avg minutes)
    rest_threshold = season_min * 0.60
    full_games     = [g for g in game_log if g["minutes"] >= rest_threshold]
    if not full_games:
        full_games = game_log

    fv  = [g["value"]   for g in full_games]
    fm  = [g["minutes"] for g in full_games]
    nf  = len(fv)
    n5  = min(5, nf)
    n10 = min(10, nf)

    l10_avg = sum(fv[:n10]) / n10
    l5_avg  = sum(fv[:n5])  / n5
    l5_min  = sum(fm[:n5])  / n5

    def per36(stat_list, min_list):
        total_min = sum(min_list)
        return (sum(stat_list) / total_min * 36) if total_min > 0 else 0.0

    season_per36 = per36(fv, fm)
    l5_per36     = per36(fv[:n5], fm[:n5])

    min_change_pct = (l5_min - season_min) / season_min if season_min > 0 else 0.0
    minutes_flag = (
        "elevated" if min_change_pct >  MIN_BUMP_PCT else
        "reduced"  if min_change_pct < -MIN_DROP_PCT else None
    )

    # ── H2H vs today's opponent ────────────────────────────────────────────────
    h2h = None
    if opp_team:
        opp_lower  = opp_team.lower()
        opp_games  = [g for g in full_games
                      if opp_lower in g.get("opponent", "").lower()
                      or g.get("opponent", "").lower() in opp_lower]
        if len(opp_games) >= 2:
            h2h_vals  = [g["value"] for g in opp_games]
            h2h_avg   = sum(h2h_vals) / len(h2h_vals)
            h2h = {
                "avg":    round(h2h_avg, 2),
                "n":      len(opp_games),
                "values": h2h_vals[:5],
            }

    # ── Home/away splits from own game log ────────────────────────────────────
    home_games = [g for g in full_games if g.get("home_away") == "home"]
    away_games = [g for g in full_games if g.get("home_away") == "away"]

    def _split(games, line=None):
        if not games:
            return None
        vals = [g["value"] for g in games]
        avg  = sum(vals) / len(vals)
        return {"avg": round(avg, 2), "n": len(vals)}

    home_split = _split(home_games)
    away_split = _split(away_games)

    # ── Opponent defensive context ─────────────────────────────────────────────
    opp_def = {}
    if opp_team:
        try:
            opp_def = _get_opp_def_stats(opp_team, stat_type)
        except Exception:
            pass

    # ── Minutes projection engine ──────────────────────────────────────────────
    import statistics as _stats

    # Weighted: 50% L3, 30% L5, 20% season — recent minutes matter most
    l3_min = sum(fm[:3]) / min(3, len(fm)) if fm else season_min
    projected_minutes = round(l3_min * 0.50 + l5_min * 0.30 + season_min * 0.20, 1)

    # Role stability: coefficient of variation on minutes
    min_std_dev = round(_stats.stdev(fm), 1) if len(fm) > 1 else 0.0
    role_cv     = round(min_std_dev / season_min, 3) if season_min > 0 else 1.0
    # cv < 0.15 = very stable role, 0.15–0.30 = moderate, > 0.30 = volatile
    role_stability = max(0.0, min(1.0, 1.0 - role_cv * 2))

    # Role change detection — compare recent 3 games to prior 3-8 games
    role_change = None
    if len(fm) >= 6:
        recent_3 = sum(fm[:3]) / 3
        prior_3  = sum(fm[3:6]) / 3
        delta    = recent_3 - prior_3
        if delta >= 6:
            role_change = "starter_spike"    # gained 6+ minutes recently
        elif delta >= 3:
            role_change = "minutes_up"
        elif delta <= -6:
            role_change = "minutes_down"
        elif delta <= -3:
            role_change = "minutes_reduced"

    # Stat volatility — separate from minutes volatility
    stat_std_dev   = round(_stats.stdev(fv), 2) if len(fv) > 1 else 0.0
    stat_cv        = round(stat_std_dev / (season_avg + 1e-9), 3)
    stat_stability = max(0.0, min(1.0, 1.0 - stat_cv))   # 1=very consistent, 0=boom/bust
    stat_median    = round(_stats.median(fv), 2) if fv else season_avg

    # Per-minute rate → projected stat (use L5 per-min rate for recency)
    stat_per_min   = (season_avg / season_min) if season_min > 0 else 0.0
    l5_per_min     = (l5_avg / l5_min) if l5_min > 0 else stat_per_min
    # Blend: 60% recent rate, 40% season rate
    blended_rate   = l5_per_min * 0.60 + stat_per_min * 0.40
    projected_stat = round(blended_rate * projected_minutes, 2)
    # Uncertainty range: ±1 stdev of minutes × rate
    proj_low  = round(blended_rate * max(0, projected_minutes - min_std_dev), 2)
    proj_high = round(blended_rate * (projected_minutes + min_std_dev), 2)

    # Usage proxy: avg FGA per game (field goal attempts = shot volume)
    fga_vals = [g.get("fga", 0) for g in full_games if g.get("fga", 0) > 0]
    usage_fga_per_game  = round(sum(fga_vals) / len(fga_vals), 1) if fga_vals else None
    usage_fga_per_min   = round(sum(fga_vals) / sum(fm[:len(fga_vals)]), 3) if fga_vals and fm else None

    # ── Rest days ──────────────────────────────────────────────────────────────
    rest_days = None
    if full_games and full_games[0].get("date"):
        try:
            last_date  = datetime.strptime(full_games[0]["date"], "%Y-%m-%d").date()
            today_date = datetime.now(timezone.utc).date()
            rest_days  = max(0, (today_date - last_date).days - 1)
        except Exception:
            pass

    result = {
        # Core averages
        "player_id":          athlete_id,
        "season_avg":         round(season_avg, 2),
        "l10_avg":            round(l10_avg, 2),
        "l5_avg":             round(l5_avg, 2),
        "last_5":             fv[:5],
        "game_values":        fv,
        "game_log":           full_games,
        "games_played":       n,
        # Minutes
        "season_min":         round(season_min, 1),
        "l5_min":             round(l5_min, 1),
        "l3_min":             round(l3_min, 1),
        "projected_minutes":  projected_minutes,
        "min_std_dev":        min_std_dev,
        "role_cv":            role_cv,
        "role_stability":     round(role_stability, 3),
        "role_change":        role_change,
        "min_change_pct":     round(min_change_pct, 3),
        "minutes_flag":       minutes_flag,
        # Stat volatility
        "stat_std_dev":       stat_std_dev,
        "stat_cv":            stat_cv,
        "stat_stability":     round(stat_stability, 3),
        "stat_median":        stat_median,
        # Projection engine
        "stat_per_min":       round(stat_per_min, 4),
        "blended_rate":       round(blended_rate, 4),
        "projected_stat":     projected_stat,
        "proj_low":           proj_low,
        "proj_high":          proj_high,
        # Usage
        "usage_fga_per_game": usage_fga_per_game,
        "usage_fga_per_min":  usage_fga_per_min,
        # Per-36
        "season_per36":       round(season_per36, 2),
        "l5_per36":           round(l5_per36, 2),
        "per36_change":       round(l5_per36 - season_per36, 2),
        # Rest
        "rest_days":          rest_days,
        # Matchup
        "h2h":                h2h,
        "home_split":         home_split,
        "away_split":         away_split,
        "opp_def":            opp_def,
    }

    _sc[cache_key] = {"ts": time.time(), "data": result}
    _save_stats_cache(_sc)
    return result
