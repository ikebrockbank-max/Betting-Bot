"""
scanner_power_parlay.py — Comprehensive multi-sport parlay optimizer.

Pulls ALL standard (non-goblin, non-demon) PrizePicks lines across:
  NBA · MLB · WNBA · TENNIS · SOCCER · NHL

Scores every pick on 5 dimensions:
  1. Hit rate          (40%) — how often player beats this exact line recently
  2. Edge size         (25%) — gap between player avg and PP line
  3. Trend             (15%) — L3 vs L8 form trajectory
  4. Opponent context  (10%) — defensive ranking, K-rate, surface
  5. Situational       (10%) — home/away, injury, rest, weather

Builds optimal parlays for PP payout tiers:
  2-pick: 3x  | 3-pick: 5x  | 4-pick: 10x  | 5-pick: 20x

Run: python3 scanner_power_parlay.py
Auto: every 4 hours via GitHub Actions (scan.yml)
"""

import json
import os
import sys
import time
import urllib.request
import itertools
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
LEAGUE_IDS = {"NBA": 7, "MLB": 2, "WNBA": 3, "TENNIS": 5, "SOCCER": 82, "NHL": 8}

PP_PAYOUTS   = {2: 3.0, 3: 6.0, 4: 10.0, 5: 20.0}
PP_BREAKEVEN = {n: 1 / p for n, p in PP_PAYOUTS.items()}  # fraction parlay must hit

MIN_CONF      = 0.70   # raised from 0.68 — 1000-pick data: 65-70% bucket hits 44% (n=57 bet picks)
                       #                    70-75% hits 54%, 75-80% hits 60% — real signal starts at 70%
MIN_EDGE_PCT  = 0.08   # minimum 8% gap between player avg and PP line
MIN_GAMES       = 6    # minimum game history required (MLB needs 6+ starts for reliability)
MIN_GAMES_NBA   = 8    # NBA needs more games for stability
MAX_PARLAY    = 5      # max legs to consider
DEDUP_HOURS   = 4      # don't re-alert same pick within this window

# Scoring weights — based on ChatGPT's recommended model, adapted to available data
# MLB pitcher: hit_rate + recent_form + handedness + arsenal + park/weather + lineup + bullpen + vegas + opp_def + edge
# NBA/WNBA: hit_rate + recent_form + opponent_defense + home_away_splits + edge + situational
WEIGHTS = {
    "hit_rate":    0.20,  # how often they've hit this exact line
    "recent_form": 0.15,  # L3 vs L8 trend
    "matchup":     0.25,  # handedness splits + arsenal + opp K rate + park + umpire (MLB)
                          # or opponent def rating + home/away splits (NBA)
    "environment": 0.15,  # park factor + weather + game total (vegas)
    "opportunity": 0.10,  # lineup position + bullpen + role/minutes
    "edge_size":   0.15,  # gap between avg and line
}

CACHE_DIR = Path("logs/parlay_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SENT_LOG  = Path("logs/parlay_sent.json")

# ── Helpers ────────────────────────────────────────────────────────────────────
def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[parlay {ts}] {msg}", flush=True)

def _get_json(url: str, extra_headers: dict = None, retries: int = 3) -> dict:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    import urllib.error
    for attempt in range(retries):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=10).read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 2 ** (attempt + 1)   # 2s, 4s, 8s
                _log(f"Rate limited (429) — retrying in {wait}s (attempt {attempt + 1}/{retries})")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("_get_json: exhausted retries")


# Process-level raw API response cache.
# fetch_standard_lines populates this; fetch_typed_lines reads from it
# to avoid duplicate API calls that trigger 429 rate limits.
# Key: (sport, today_str) → raw PrizePicks API response dict.
_pp_raw_cache: dict[tuple, dict] = {}

def _cache(key: str, ttl: int = 1800):
    """Decorator-style: return cached value if fresh, else None."""
    p = CACHE_DIR / f"{key[:80].replace('/','_').replace(' ','_')}.json"
    if p.exists() and (time.time() - p.stat().st_mtime) < ttl:
        try:
            return json.loads(p.read_text()), p
        except Exception:
            pass
    return None, p

def _save(p: Path, data):
    try:
        p.write_text(json.dumps(data))
    except Exception:
        pass

def _load_sent() -> dict:
    if SENT_LOG.exists():
        try:
            return json.loads(SENT_LOG.read_text())
        except Exception:
            pass
    return {}

def _save_sent(data: dict):
    try:
        SENT_LOG.write_text(json.dumps(data))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Pull standard PrizePicks lines
# ─────────────────────────────────────────────────────────────────────────────

def _parse_start_time(st: str) -> datetime | None:
    """Parse PP start_time string to UTC datetime.
    PP format: "2026-06-01T19:05:00.000-04:00"
    Uses fromisoformat() (Python 3.11+) which handles any UTC offset correctly,
    including positive offsets (+05:30) that the old regex mangled.
    """
    if not st:
        return None
    try:
        # Strip milliseconds — fromisoformat handles the rest
        import re as _re
        clean = _re.sub(r"\.\d+", "", st)   # "2026-06-01T19:05:00.000-04:00" → "2026-06-01T19:05:00-04:00"
        dt = datetime.fromisoformat(clean)
        return dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return None

def fetch_standard_lines(sports: list[str] = None, days_ahead: int = 1) -> list[dict]:
    """
    Pull all STANDARD (non-goblin, non-demon) pre-game single-stat projections
    for games starting within the next `days_ahead` days (default: today only).

    Cache key includes today's date so stale data never bleeds into the next day.
    """
    if sports is None:
        sports = list(LEAGUE_IDS.keys())

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Window: now → days_ahead days from now
    now_utc   = datetime.now(timezone.utc)
    cutoff    = now_utc + timedelta(days=days_ahead)

    lines = []
    for sport in sports:
        lid = LEAGUE_IDS.get(sport)
        if not lid:
            continue
        # Date-stamped cache key — expires with the day, never serves yesterday's games
        cached, cpath = _cache(f"pp_standard_{sport}_{today_str}", ttl=900)
        if cached is not None:
            lines.extend(cached)
            continue

        try:
            url = (f"https://api.prizepicks.com/projections"
                   f"?league_id={lid}&per_page=500&single_stat=true&state_code=AZ")
            data = _get_json(url, {"Referer": "https://app.prizepicks.com/"})
            # Cache raw response so fetch_typed_lines can reuse it without
            # making a second API call (which triggers 429 rate limits).
            _pp_raw_cache[(sport, today_str)] = data
            players = {
                p["id"]: p["attributes"].get("name", "?")
                for p in data.get("included", [])
                if p["type"] == "new_player"
            }
            sport_lines = []
            skipped_future = 0
            for proj in data.get("data", []):
                a = proj["attributes"]
                if a.get("odds_type") != "standard":
                    continue
                # Allow pre_game AND in_game lines (in_game = live / first few
                # innings props that appear on PrizePicks' Live tab mid-game).
                status = a.get("status", "")
                if status not in ("pre_game", "in_progress"):
                    continue
                if a.get("projection_type") != "Single Stat":
                    continue

                # ── DATE FILTER: only games starting within the window ──────────
                start_time_str = a.get("start_time", "")
                game_dt = _parse_start_time(start_time_str)
                if game_dt and status == "pre_game":
                    # Pre-game date filter: only upcoming games in window
                    if game_dt < now_utc:
                        continue   # game already started / past
                    if game_dt > cutoff:
                        skipped_future += 1
                        continue   # too far in the future
                # in_progress lines: no date filter — game is live now, always include

                pid  = proj["relationships"].get("new_player", {}).get("data", {}).get("id", "")
                name = players.get(pid, "?")
                if name == "?":
                    continue
                # Skip combo props — player name contains "+" or stat type has "(Combo)"
                if "+" in name or "(Combo)" in a.get("stat_type", ""):
                    continue
                # Skip 1st inning props — too small sample, too volatile
                if "1st Inning" in a.get("stat_type", ""):
                    continue
                sport_lines.append({
                    "player":     name,
                    "stat_type":  a.get("stat_type", "?"),
                    "line":       float(a.get("line_score", 0)),
                    "sport":      sport,
                    "game_id":    a.get("game_id", ""),
                    "start_time": start_time_str,
                    "pp_id":      proj["id"],
                })

            _save(cpath, sport_lines)
            lines.extend(sport_lines)
            _log(f"{sport}: {len(sport_lines)} standard lines today"
                 + (f" ({skipped_future} future games skipped)" if skipped_future else ""))
            # Snapshot lines for movement tracking
            try:
                from line_tracker import snapshot_lines as _snap
                _snap(sport_lines)
            except Exception:
                pass
            time.sleep(0.8)
        except Exception as e:
            _log(f"{sport}: PP fetch failed — {e}")

    return lines


def fetch_typed_lines(sports: list[str], odds_type: str) -> list[dict]:
    """
    Fetch PrizePicks projections for a specific odds_type: 'goblin' or 'demon'.
    Same filtering logic as fetch_standard_lines but parameterised by type.
    Each returned line is tagged with projection_kind = odds_type.
    """
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_utc   = datetime.now(timezone.utc)
    cutoff    = now_utc + timedelta(days=1)

    lines = []
    for sport in sports:
        lid = LEAGUE_IDS.get(sport)
        if not lid:
            continue
        cached, cpath = _cache(f"pp_{odds_type}_{sport}_{today_str}", ttl=900)
        if cached is not None:
            lines.extend(cached)
            continue

        try:
            # Reuse the raw response cached by fetch_standard_lines if available —
            # both functions call the same URL, so the second/third call is free.
            raw_cached = _pp_raw_cache.get((sport, today_str))
            if raw_cached is not None:
                data = raw_cached
            else:
                url = (f"https://api.prizepicks.com/projections"
                       f"?league_id={lid}&per_page=500&single_stat=true&state_code=AZ")
                data = _get_json(url, {"Referer": "https://app.prizepicks.com/"})
                _pp_raw_cache[(sport, today_str)] = data
            players = {
                p["id"]: p["attributes"].get("name", "?")
                for p in data.get("included", [])
                if p["type"] == "new_player"
            }
            sport_lines = []
            for proj in data.get("data", []):
                a = proj["attributes"]
                if a.get("odds_type") != odds_type:
                    continue
                status = a.get("status", "")
                if status not in ("pre_game", "in_progress"):
                    continue
                if a.get("projection_type") != "Single Stat":
                    continue
                start_time_str = a.get("start_time", "")
                game_dt = _parse_start_time(start_time_str)
                if game_dt and status == "pre_game":
                    if game_dt < now_utc:
                        continue
                    if game_dt > cutoff:
                        continue
                pid  = proj["relationships"].get("new_player", {}).get("data", {}).get("id", "")
                name = players.get(pid, "?")
                if name == "?" or "+" in name:
                    continue
                if "(Combo)" in a.get("stat_type", "") or "1st Inning" in a.get("stat_type", ""):
                    continue
                sport_lines.append({
                    "player":          name,
                    "stat_type":       a.get("stat_type", "?"),
                    "line":            float(a.get("line_score", 0)),
                    "sport":           sport,
                    "game_id":         a.get("game_id", ""),
                    "start_time":      start_time_str,
                    "pp_id":           proj["id"],
                    "projection_kind": odds_type,
                    # rank: lower = harder line = higher real multiplier.
                    # Used by parlay builder to pick the hardest viable line
                    # per player/stat rather than the easiest one.
                    "difficulty_rank": int(a.get("rank", 999)),
                })

            _save(cpath, sport_lines)
            lines.extend(sport_lines)
            _log(f"{sport}: {len(sport_lines)} {odds_type} lines today")
            time.sleep(0.8)
        except Exception as e:
            _log(f"{sport}: {odds_type} fetch failed — {e}")

    return lines


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Get historical stats per sport
# ─────────────────────────────────────────────────────────────────────────────

NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Referer": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Origin": "https://www.nba.com",
}

_NBA_STAT_MAP = {
    "Points":            "PTS",
    "Rebounds":          "REB",
    "Assists":           "AST",
    "3-Pointers Made":   "FG3M",
    "Steals":            "STL",
    "Blocks":            "BLK",
    "Turnovers":         "TOV",
    "Pts+Rebs+Asts":     None,  # computed
    "Pts+Rebs":          None,
    "Pts+Asts":          None,
}

def _get_nba_player_id(player_name: str) -> str | None:
    cached, cpath = _cache(f"nba_pid_{player_name}", ttl=86400)
    if cached:
        return cached.get("id")
    try:
        url  = "https://stats.nba.com/stats/commonallplayers?LeagueID=00&Season=2025-26&IsOnlyCurrentSeason=1"
        data = _get_json(url, NBA_HEADERS)
        rows = data["resultSets"][0]["rowSet"]
        hdrs = data["resultSets"][0]["headers"]
        name_idx = hdrs.index("DISPLAY_FIRST_LAST")
        id_idx   = hdrs.index("PERSON_ID")
        name_lower = player_name.lower()
        for row in rows:
            if row[name_idx].lower() == name_lower:
                pid = str(row[id_idx])
                _save(cpath, {"id": pid})
                return pid
        # Fuzzy: last name match
        last = player_name.split()[-1].lower()
        for row in rows:
            if last in row[name_idx].lower():
                pid = str(row[id_idx])
                _save(cpath, {"id": pid})
                return pid
    except Exception as e:
        _log(f"NBA player ID lookup failed for {player_name}: {e}")
    return None

def _nba_game_log(player_id: str, season: str = "2025-26") -> list[dict]:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cached, cpath = _cache(f"nba_log_{player_id}_{season}_{today_str}", ttl=3600)
    if cached:
        return cached
    try:
        url  = (f"https://stats.nba.com/stats/playergamelogs"
                f"?PlayerID={player_id}&Season={season}&SeasonType=Regular+Season&LeagueID=00")
        data = _get_json(url, NBA_HEADERS)
        rs   = data["resultSets"][0]
        hdrs = rs["headers"]
        rows = rs["rowSet"]
        games = []
        for row in rows:
            g = dict(zip(hdrs, row))
            games.append({
                "date":       g.get("GAME_DATE", ""),
                "matchup":    g.get("MATCHUP", ""),
                "pts":        g.get("PTS", 0) or 0,
                "reb":        g.get("REB", 0) or 0,
                "ast":        g.get("AST", 0) or 0,
                "fg3m":       g.get("FG3M", 0) or 0,
                "stl":        g.get("STL", 0) or 0,
                "blk":        g.get("BLK", 0) or 0,
                "tov":        g.get("TOV", 0) or 0,
                "min":        int(float(g.get("MIN", 0) or 0)),
            })
        # Also try playoffs — prepend so most-recent game is always games[0]
        try:
            url2 = url.replace("Regular+Season", "Playoffs")
            d2   = _get_json(url2, NBA_HEADERS)
            rs2  = d2["resultSets"][0]
            playoff_games = []
            for row in rs2["rowSet"]:
                g = dict(zip(rs2["headers"], row))
                playoff_games.append({
                    "date":    g.get("GAME_DATE", ""),
                    "matchup": g.get("MATCHUP", ""),
                    "pts":     g.get("PTS", 0) or 0,
                    "reb":     g.get("REB", 0) or 0,
                    "ast":     g.get("AST", 0) or 0,
                    "fg3m":    g.get("FG3M", 0) or 0,
                    "stl":     g.get("STL", 0) or 0,
                    "blk":     g.get("BLK", 0) or 0,
                    "tov":     g.get("TOV", 0) or 0,
                    "min":     int(float(g.get("MIN", 0) or 0)),
                    "playoff": True,
                })
            # Sort descending by date so most-recent playoff game is first
            playoff_games.sort(key=lambda x: x["date"], reverse=True)
            games = playoff_games + games
        except Exception:
            pass
        _save(cpath, games)
        return games
    except Exception as e:
        _log(f"NBA game log failed ({player_id}): {e}")
        return []

def _get_nba_stats(player_name: str, stat_type: str, line: float) -> dict | None:
    pid = _get_nba_player_id(player_name)
    if not pid:
        return None
    games = _nba_game_log(pid)
    if not games:
        return None

    def val(g):
        if stat_type == "Points":       return g["pts"]
        if stat_type == "Rebounds":     return g["reb"]
        if stat_type == "Assists":      return g["ast"]
        if stat_type == "3-Pointers Made": return g["fg3m"]
        if stat_type == "Steals":       return g["stl"]
        if stat_type == "Blocks":       return g["blk"]
        if stat_type == "Turnovers":    return g["tov"]
        if stat_type == "Pts+Rebs+Asts": return g["pts"] + g["reb"] + g["ast"]
        if stat_type == "Pts+Rebs":     return g["pts"] + g["reb"]
        if stat_type == "Pts+Asts":     return g["pts"] + g["ast"]
        return None

    # Prioritise playoff games (already at front if present)
    values = []
    for g in games:
        v = val(g)
        if v is not None and g.get("min", 0) >= 10:
            values.append(v)

    result = _compute_stats(player_name, stat_type, line, values, "NBA")
    if result:
        result["_raw_game_logs"] = games

        # Rest days: days between most recent game and today
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            last_date_raw = games[0].get("date", "") if games else ""
            if last_date_raw:
                # NBA dates come as ISO: "2026-04-19T00:00:00" or plain "2026-04-19"
                date_part = last_date_raw[:10]   # always take YYYY-MM-DD prefix
                last_date  = datetime.strptime(date_part, "%Y-%m-%d").date()
                today_date = datetime.strptime(today_str, "%Y-%m-%d").date()
                rest = max(0, (today_date - last_date).days - 1)
                result["rest_days"] = rest
        except Exception:
            pass

        # Minutes trend — use playoff minutes when available (more relevant than regular season)
        try:
            playoff_mins = [g.get("min", 0) for g in games if g.get("playoff") and g.get("min", 0) > 0]
            all_mins     = [g.get("min", 0) for g in games if g.get("min", 0) > 0]
            if len(all_mins) >= 5:
                if len(playoff_mins) >= 3:
                    # Enough playoff data: compare recent playoff games vs playoff avg
                    season_min = sum(playoff_mins) / len(playoff_mins)
                    l5_min     = sum(playoff_mins[:min(5, len(playoff_mins))]) / min(5, len(playoff_mins))
                else:
                    season_min = sum(all_mins) / len(all_mins)
                    l5_min     = sum(all_mins[:5]) / 5
                result["season_min"]   = round(season_min, 1)
                result["l5_min"]       = round(l5_min, 1)
                result["minutes_flag"] = (
                    "elevated" if l5_min > season_min * 1.15 else
                    "reduced"  if l5_min < season_min * 0.85 else None
                )
                if playoff_mins:
                    result["playoff_games"]   = len(playoff_mins)
                    result["playoff_min_avg"] = round(sum(playoff_mins) / len(playoff_mins), 1)
        except Exception:
            pass

    return result


def _normalize_name(name: str) -> str:
    """Strip accents/diacritics for MLB API search (Sánchez → Sanchez)."""
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFD", name)
        if unicodedata.category(c) != "Mn"
    )

def _get_mlb_pitcher_id(player_name: str) -> str | None:
    cached, cpath = _cache(f"mlb_pid_{player_name}", ttl=86400)
    if cached:
        return cached.get("id")
    # Try exact name first, then accent-stripped, then last-name-only fallback
    search_variants = [
        player_name,
        _normalize_name(player_name),
        player_name.split()[-1],  # last name only
    ]
    for variant in search_variants:
        try:
            enc  = variant.replace(" ", "+")
            url  = f"https://statsapi.mlb.com/api/v1/people/search?names={enc}&sportId=1"
            data = _get_json(url)
            for p in data.get("people", []):
                full = p.get("fullName", "")
                # Match: normalized versions of both names match
                if (_normalize_name(player_name).lower() in _normalize_name(full).lower() or
                        _normalize_name(full).lower() in _normalize_name(player_name).lower()):
                    pid = str(p["id"])
                    _save(cpath, {"id": pid, "matched_name": full})
                    return pid
        except Exception:
            pass
    return None

def _get_mlb_stats(player_name: str, stat_type: str, line: float) -> dict | None:
    """Route all MLB stat types through the comprehensive batter/pitcher stats module."""
    try:
        from data.mlb_batter_stats import get_player_stats as _mlb_full
        return _mlb_full(player_name, stat_type, line)
    except Exception as e:
        _log(f"MLB stats failed {player_name} {stat_type}: {e}")
        return None
    # Legacy pitcher-only path (kept as fallback)
    pid = _get_mlb_pitcher_id(player_name)
    if not pid:
        return None
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cached, cpath = _cache(f"mlb_log_{pid}_{today_str}", ttl=3600)
    if cached:
        games = cached
    else:
        try:
            url   = f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=pitching&season=2026"
            data  = _get_json(url)
            splits = data.get("stats", [{}])[0].get("splits", [])
            games  = []
            for s in splits:
                ip = s["stat"].get("inningsPitched", "0")
                try:
                    ip_f = float(ip)
                except Exception:
                    ip_f = 0
                if ip_f < 2.0:     # skip relief appearances (< 2 IP not a real start)
                    continue
                games.append({
                    "date":    s.get("date", ""),
                    "opp":     s.get("opponent", {}).get("name", "?"),
                    "ks":      s["stat"].get("strikeOuts", 0),
                    "hits":    s["stat"].get("hits", 0),
                    "walks":   s["stat"].get("baseOnBalls", 0),
                    "er":      s["stat"].get("earnedRuns", 0),
                    "ip":      ip_f,
                })
            games = sorted(games, key=lambda x: x["date"], reverse=True)
            _save(cpath, games)
        except Exception as e:
            _log(f"MLB log failed ({pid}): {e}")
            return None

    def val(g):
        if stat_type in ("Strikeouts", "Pitcher Strikeouts"): return g["ks"]
        if stat_type == "Hits Allowed":   return g["hits"]
        if stat_type == "Walks":          return g["walks"]
        if stat_type == "Earned Runs":    return g["er"]
        return None

    values = [val(g) for g in games if val(g) is not None]
    return _compute_stats(player_name, stat_type, line, values, "MLB")


def _get_wnba_stats(player_name: str, stat_type: str, line: float,
                    opp_team: str = "") -> dict | None:
    """
    Fetch WNBA stats with full matchup context:
    - Current (2026) + prior (2025) season game log
    - H2H vs today's opponent
    - Home/away splits from own game log
    - Opponent defensive context
    """
    try:
        from data.wnba_stats import get_player_stats as _wnba
        result = _wnba(player_name, stat_type, opp_team=opp_team)
        if not result:
            return None
        game_vals = result.get("game_values") or result.get("last_5", [])
        if not game_vals:
            return None
        computed = _compute_stats(player_name, stat_type, line, game_vals, "WNBA")
        if not computed:
            return None
        # Pass through all enriched context
        computed["minutes_flag"]       = result.get("minutes_flag")
        computed["season_min"]         = result.get("season_min")
        computed["l5_min"]             = result.get("l5_min")
        computed["projected_minutes"]  = result.get("projected_minutes")
        computed["min_std_dev"]        = result.get("min_std_dev")
        computed["role_stability"]     = result.get("role_stability", 0.5)
        computed["projected_stat"]     = result.get("projected_stat")
        computed["proj_low"]           = result.get("proj_low")
        computed["proj_high"]          = result.get("proj_high")
        computed["stat_per_min"]       = result.get("stat_per_min")
        computed["usage_fga_per_game"] = result.get("usage_fga_per_game")
        computed["usage_trend"]        = result.get("usage_trend")
        computed["usage_adj"]          = result.get("usage_adj", 1.0)
        computed["rest_days"]          = result.get("rest_days")
        computed["season_per36"]       = result.get("season_per36")
        computed["l5_per36"]           = result.get("l5_per36")
        computed["wnba_h2h"]           = result.get("h2h")
        computed["home_split"]         = result.get("home_split")
        computed["away_split"]         = result.get("away_split")
        computed["opp_def"]            = result.get("opp_def", {})
        computed["game_log"]           = result.get("game_log", [])
        computed["injury_impact"]      = result.get("injury_impact", {})
        computed["injury_note"]        = result.get("injury_note", "")

        # Override avg with projected_stat so edge_pct uses projection not raw avg
        proj = result.get("projected_stat")
        if proj is not None and proj > 0:
            computed["avg"] = proj   # projection replaces avg as the primary signal

        return computed
    except Exception:
        pass
    return None

def _get_tennis_stats(player_name: str, stat_type: str, line: float) -> dict | None:
    try:
        from data.tennis_stats import get_player_stats as _tn
        return _tn(player_name, stat_type, line)
    except Exception:
        return None

def _get_soccer_stats(player_name: str, stat_type: str, line: float) -> dict | None:
    try:
        from data.soccer_stats import get_player_stats as _sc
        return _sc(player_name, stat_type, line)
    except Exception:
        return None


def _compute_stats(player: str, stat_type: str, line: float,
                   values: list, sport: str) -> dict | None:
    """Core stat computation given a list of recent values."""
    if not values or len(values) < MIN_GAMES:
        return None

    n       = min(len(values), 10)
    recent  = values[:n]
    l3      = values[:3]
    l5      = values[:min(5, len(values))]

    avg_n  = sum(recent) / len(recent)
    avg_l3 = sum(l3) / len(l3)
    avg_l5 = sum(l5) / len(l5)

    over_hits  = sum(1 for v in recent if v > line)
    under_hits = sum(1 for v in recent if v < line)

    direction  = "OVER" if avg_n > line else "UNDER"
    hit_rate   = (over_hits / n) if direction == "OVER" else (under_hits / n)

    # Trend: positive means player is trending toward direction
    trend = (avg_l3 - avg_n) / (avg_n + 1e-9)
    if direction == "UNDER":
        trend = -trend  # invert: for UNDER, decreasing is good

    # stat_std_dev: actual game-to-game outcome variance from the player's log.
    # Critical distinction: this is NOT the projection CI (proj_low/proj_high),
    # which measures uncertainty about the mean estimate.
    # This measures how variable the outcome is game-to-game — the σ the
    # Gaussian probability model needs to be meaningful.
    # Without this, the model falls back to half the projection range (σ≈0.5),
    # which massively underestimates variance for high-variance stats like
    # turnovers, rebounds, etc. → inflated P(over/under) that doesn't match reality.
    import statistics as _stat_mod
    stat_std_dev = round(_stat_mod.stdev(recent), 3) if len(recent) >= 4 else None

    # Zero-inflation components: used by MLB pitcher logic and future
    # WNBA zero-inflated model (stats like turnovers and steals have heavy
    # zero-game rate that a pure Gaussian misses).
    zero_game_count = sum(1 for v in recent if v == 0)
    p_zero_game     = round(zero_game_count / len(recent), 3) if recent else 0.0
    _nonzero_vals   = [v for v in recent if v > 0]
    nonzero_mean    = round(sum(_nonzero_vals) / len(_nonzero_vals), 2) if _nonzero_vals else None
    nonzero_std     = round(_stat_mod.stdev(_nonzero_vals), 3) if len(_nonzero_vals) >= 3 else None

    return {
        "player":        player,
        "stat_type":     stat_type,
        "line":          line,
        "direction":     direction,
        "avg":           round(avg_n, 2),
        "avg_l3":        round(avg_l3, 2),
        "avg_l5":        round(avg_l5, 2),
        "hit_rate":      round(hit_rate, 3),
        "over_hits":     over_hits,
        "under_hits":    under_hits,
        "n_games":       n,
        "recent_values": recent[:8],
        "trend":         round(trend, 3),
        "sport":         sport,
        # Distribution shape fields — used by the probability engine
        "stat_std_dev":  stat_std_dev,   # outcome σ (not projection CI)
        "p_zero_game":   p_zero_game,    # fraction of zero-outcome games
        "nonzero_mean":  nonzero_mean,   # conditional mean on non-zero games
        "nonzero_std":   nonzero_std,    # conditional σ on non-zero games
        "median_val":    round(sorted(recent)[len(recent) // 2], 2),
    }


def get_stats_for_pick(pick: dict) -> dict | None:
    """Route to the right stat module based on sport."""
    sport  = pick["sport"]
    player = pick["player"]
    stat   = pick["stat_type"]
    line   = pick["line"]

    try:
        if sport == "WNBA":
            # Pass opponent for H2H and defensive context — resolve from matchup context first
            opp_team = pick.get("opp_team", "")
            if not opp_team:
                try:
                    from data.matchup_context import get_context
                    ctx = get_context(sport="WNBA", player_name=player,
                                     stat_type=stat, game_logs=None, line=line)
                    opp_team = ctx.get("opp_team", "")
                except Exception:
                    pass
            return _get_wnba_stats(player, stat, line, opp_team=opp_team)

        dispatchers = {
            "NBA":    _get_nba_stats,
            "MLB":    _get_mlb_stats,
            "TENNIS": _get_tennis_stats,
            "SOCCER": _get_soccer_stats,
        }
        fn = dispatchers.get(sport)
        if fn is None:
            return None
        return fn(player, stat, line)
    except Exception as e:
        _log(f"Stats failed {sport} {player} {stat}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Score each pick
# ─────────────────────────────────────────────────────────────────────────────

def _load_nba_def_ratings():
    """Pre-warm the NBA def ratings cache via matchup_context module."""
    try:
        from data.matchup_context import _load_nba_def_ratings as _warm
        _warm()
    except Exception as e:
        _log(f"NBA def ratings pre-warm failed: {e}")


_NBA_PACE_CACHE: dict[str, float] = {}

def _get_nba_team_pace(team_name: str) -> float | None:
    """
    Team pace (possessions/48 min) for 2025-26 playoffs/regular season.
    League avg ~100. Cached in-process.
    team_name is the full team name from context (e.g. "Boston Celtics").
    """
    global _NBA_PACE_CACHE
    if not _NBA_PACE_CACHE:
        # Always load Regular Season first (all 30 teams), then overlay Playoffs
        for season_type in ["Regular+Season", "Playoffs"]:
            try:
                url = (f"https://stats.nba.com/stats/leaguedashteamstats"
                       f"?Season=2025-26&SeasonType={season_type}"
                       f"&MeasureType=Advanced&PerMode=PerGame"
                       f"&PaceAdjust=N&PlusMinus=N&Rank=N&LeagueID=00"
                       f"&Direction=DESC&Conference=&Division=&GameScope="
                       f"&GameSegment=&LastNGames=0&Location=&Month=0"
                       f"&OpponentTeamID=0&Outcome=&PORound=0&Period=0"
                       f"&PlayerExperience=&PlayerPosition=&StarterBench=&TwoWay=0")
                data = _get_json(url, NBA_HEADERS)
                rs   = data["resultSets"][0]
                hdrs = rs["headers"]
                if "PACE" not in hdrs or "TEAM_NAME" not in hdrs:
                    continue
                pi = hdrs.index("PACE")
                ni = hdrs.index("TEAM_NAME")
                for row in rs["rowSet"]:
                    name = row[ni]
                    pace = row[pi]
                    if name and pace:
                        # Playoffs data overwrites Regular Season (more current)
                        _NBA_PACE_CACHE[name.lower()] = float(pace)
            except Exception:
                continue

    needle = team_name.lower()
    if needle in _NBA_PACE_CACHE:
        return _NBA_PACE_CACHE[needle]
    # Fuzzy: last word match
    last = needle.split()[-1] if needle else ""
    for k, v in _NBA_PACE_CACHE.items():
        if k.split()[-1] == last:
            return v
    return None


def _get_nba_vegas_total(home_team: str, away_team: str) -> float | None:
    """Pull NBA game total from The Odds API (same key as MLB)."""
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return None
    cached, cpath = _cache(f"nba_odds_{home_team}_{away_team}", ttl=900)
    if cached:
        return cached.get("total")
    try:
        url  = (f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
                f"?apiKey={api_key}&regions=us&markets=totals"
                f"&oddsFormat=american&dateFormat=iso")
        data = _get_json(url)
        ht_last = home_team.lower().split()[-1]
        at_last = away_team.lower().split()[-1]
        for g in data:
            gh = g.get("home_team", "").lower()
            ga = g.get("away_team", "").lower()
            if ht_last in gh and at_last in ga:
                for bm in g.get("bookmakers", []):
                    for mkt in bm.get("markets", []):
                        if mkt["key"] == "totals":
                            for o in mkt["outcomes"]:
                                if o["name"] == "Over":
                                    total = o.get("point")
                                    _save(cpath, {"total": total})
                                    return total
    except Exception:
        pass
    return None

_PITCHER_STAT_TYPES = {
    "Pitcher Strikeouts", "Strikeouts", "Pitcher Fantasy Score",
    "Pitching Outs", "Earned Runs Allowed", "Hits Allowed",
    "Walks Allowed", "Pitches Thrown",
}

# Elite rim protectors: player last name → team name keywords
# These players suppress opposing bigs' scoring/rebounding significantly
_ELITE_RIM_PROTECTORS = {
    "wembanyama": ["spurs", "san antonio"],
    "gobert":     ["timberwolves", "minnesota"],
    "kessler":    ["jazz", "utah"],
    "lopez":      ["bucks", "milwaukee"],
    "mobley":     ["cavaliers", "cleveland"],
    "sengun":     ["rockets", "houston"],
}

# Stat types where rim protection meaningfully reduces production
_RIM_AFFECTED_STATS = {
    "Points", "Rebounds", "Blocks",
    "Pts+Rebs", "Pts+Rebs+Asts", "Pts+Asts",
}

def _check_rim_protector(opp_team: str, stats: dict, stat_type: str) -> tuple[float, str]:
    """
    Returns (penalty, note). penalty < 1.0 when facing an elite rim protector.

    Penalty severity scales with how inside-oriented the player is:
      avg_reb >= 6  → big/center  → 0.83 penalty
      avg_reb >= 4  → PF/forward  → 0.90 penalty
      avg_reb < 4   → guard/wing  → 0.96 penalty (spacing still disrupted)
    """
    if not opp_team or opp_team == "unknown":
        return 1.0, ""
    if stat_type not in _RIM_AFFECTED_STATS:
        return 1.0, ""

    opp_lower = opp_team.lower()
    for protector, keywords in _ELITE_RIM_PROTECTORS.items():
        if any(kw in opp_lower for kw in keywords):
            # Estimate player's role from rebound average in recent game logs
            logs = stats.get("_raw_game_logs", [])
            if logs:
                reb_vals = [g.get("reb", 0) for g in logs[:10] if g.get("min", 0) >= 10]
                avg_reb  = sum(reb_vals) / len(reb_vals) if reb_vals else 0
            else:
                avg_reb = stats.get("avg", 0) if stat_type == "Rebounds" else 3.0

            if avg_reb >= 6:
                return 0.83, f"⚠️ vs {protector.title()} — elite rim protection (big matchup)"
            elif avg_reb >= 4:
                return 0.90, f"⚠️ vs {protector.title()} — rim protection (forward matchup)"
            else:
                return 0.96, f"⚠️ vs {protector.title()} — rim protection (spacing disrupted)"

    return 1.0, ""

def _get_full_context(stats: dict, pick: dict) -> dict:
    """
    Get real matchup context: opponent quality, home/away, park factors, splits,
    umpire tendency, pitch arsenal, lineup position, bullpen, Vegas totals.

    MLB pitchers → get_pitcher_full_context (K-weighted, arsenal, umpire, bullpen)
    MLB batters  → get_batter_full_context  (handedness splits, batting order, PA context)
    NBA/WNBA     → matchup_context.get_context (defensive ratings, home/away splits)
    """
    sport     = pick["sport"]
    stat_type = pick["stat_type"]

    try:
        if sport == "MLB":
            if stat_type in _PITCHER_STAT_TYPES:
                from data.mlb_advanced import get_pitcher_full_context
                return get_pitcher_full_context(
                    player_name = pick["player"],
                    stat_type   = stat_type,
                )
            else:
                # Batter prop — use batter-specific context
                from data.mlb_advanced import get_batter_full_context
                # Get pitcher hand from stats dict (already computed in mlb_batter_stats)
                pitcher_hand = stats.get("pitcher_hand", "R")
                batter_id    = None
                try:
                    from data.mlb_batter_stats import find_player_id
                    pid = find_player_id(pick["player"])
                    batter_id = int(pid) if pid else None
                except Exception:
                    pass
                return get_batter_full_context(
                    player_name  = pick["player"],
                    stat_type    = stat_type,
                    batter_id    = batter_id,
                    pitcher_hand = pitcher_hand,
                )
        else:
            from data.matchup_context import get_context
            game_logs = stats.get("_raw_game_logs")
            ctx = get_context(
                sport       = sport,
                player_name = pick["player"],
                stat_type   = stat_type,
                game_logs   = game_logs,
                line        = pick["line"],
            )
            # Attach NBA Vegas total if we can identify the matchup
            if sport == "NBA" and ctx.get("opp_team", "unknown") != "unknown":
                try:
                    game_logs_r = game_logs or []
                    last_mu     = game_logs_r[0].get("matchup", "") if game_logs_r else ""
                    # matchup like "BOS vs. LAL" → home=BOS, away=LAL (or "@")
                    is_home = "vs." in last_mu
                    my_abbr = last_mu.split(" vs.")[0].strip() if is_home else last_mu.split(" @")[0].strip()
                    opp_name = ctx["opp_team"]
                    home_t   = pick["player"] if is_home else opp_name  # approximate
                    away_t   = opp_name if is_home else pick["player"]
                    total    = _get_nba_vegas_total(home_t, away_t)
                    if total:
                        ctx.setdefault("components", {})["game_total"] = total
                        ctx["description"].append(f"Game total: {total}")
                except Exception:
                    pass
            return ctx
    except Exception as e:
        _log(f"Context failed {pick['player']}: {e}")
        return {"context_score": 0.5, "description": [], "home_away": "unknown", "opp_team": "unknown"}

def _injury_multiplier(player: str, sport: str) -> float:
    """1.0 = healthy, 0.0 = out. Try ESPN injury report."""
    try:
        from data.injuries import get_injury_report
        report = get_injury_report(sport.lower() if sport in ("NBA","WNBA") else "nba")
        for inj in report:
            if player.split()[-1].lower() in inj.get("player","").lower():
                status = inj.get("status", "").lower()
                if "out" in status:       return 0.0
                if "doubtful" in status:  return 0.2
                if "questionable" in status: return 0.7
    except Exception:
        pass
    return 1.0

def score_pick(stats: dict, pick: dict) -> dict:
    """
    Compute a composite 0–100 confidence score for a pick.

    5 factors:
      40% hit_rate   — how often they've hit this exact line historically
      25% edge_size  — gap between player avg and PP line
      15% trend      — L3 vs L8 trajectory
      10% opponent   — opponent K rate / defensive rating / park factor / home-away
      10% situational — injury status + data sample size
    """
    sport      = pick.get("sport", stats.get("sport", ""))
    stat_type  = pick.get("stat_type", "")
    hit_rate   = stats.get("hit_rate", 0)
    avg        = stats.get("avg", 0)
    line       = stats.get("line", 1)
    trend      = stats.get("trend", 0)

    # ── MLB batter: two structural adjustments before edge gate ──────────────
    # 1. Median over mean for zero-inflated / right-skewed distributions
    #    (Fantasy Score, Total Bases, Hits, etc.) — mean is inflated by outlier
    #    games; median is the more honest central tendency.
    # 2. Pitcher strength prior — scale expected output by opponent difficulty.
    #    An ace starter (Wheeler/Cole tier) should reduce expected batter output;
    #    a weak starter should increase it. Without this, the model treats all
    #    RHP identically except for thin career H2H samples.
    _MLB_SKEWED = {"Hitter Fantasy Score", "Total Bases", "Hits+Runs+RBIs",
                   "Singles", "Hits"}
    effective_avg   = avg
    pitcher_adj_note = ""

    if sport == "MLB" and stat_type not in _PITCHER_STAT_TYPES:
        # Step 1: prefer median for skewed distributions
        median_val = stats.get("median_val")
        if median_val is not None and stat_type in _MLB_SKEWED:
            effective_avg = median_val

        # Step 2: apply pitcher difficulty multiplier, dampened by H2H richness.
        #
        # The generic skill-score (ERA/K%/WHIP) and the career H2H batting avg
        # both measure the same thing: how hard is this pitcher for this batter?
        # When we have 10+ career AB vs the specific pitcher, H2H already prices
        # in his difficulty — applying the full skill-score multiplier on top
        # would double-count. Dampen proportionally:
        #   0 AB  → 100% of multiplier (no H2H — skill score is our only signal)
        #  10 AB  → 70%  of multiplier
        #  20+ AB → 40%  of multiplier (H2H is now the primary difficulty signal)
        diff_mult    = stats.get("difficulty_multiplier", 1.0)
        pitcher_tier = stats.get("pitcher_tier", "")
        if diff_mult != 1.0:
            h2h_ab         = int((stats.get("h2h") or {}).get("ab", 0) or 0)
            pitcher_weight = max(0.40, 1.0 - min(1.0, h2h_ab / 20.0) * 0.60)
            dampened_mult  = round(1.0 + (diff_mult - 1.0) * pitcher_weight, 3)
            effective_avg  = round(effective_avg * dampened_mult, 2)
            skill_score    = stats.get("pitcher_skill_score") or 0.0
            if h2h_ab >= 10:
                # H2H is the primary difficulty signal — note the dampening
                pitcher_adj_note = (
                    f"Pitcher adj ×{dampened_mult:.2f} "
                    f"(dampened from ×{diff_mult:.2f} — {h2h_ab} career AB vs pitcher)"
                )
            elif dampened_mult <= 0.82:
                pitcher_adj_note = (
                    f"⚠️ {pitcher_tier.replace('_',' ').title()} pitcher "
                    f"(×{dampened_mult:.2f} adj — skill {skill_score:.1f}/10)"
                )
            elif dampened_mult <= 0.92:
                pitcher_adj_note = (
                    f"Above-avg pitcher (×{dampened_mult:.2f} — skill {skill_score:.1f}/10)"
                )
            elif dampened_mult >= 1.12:
                pitcher_adj_note = (
                    f"✅ Weak pitcher (×{dampened_mult:.2f} — skill {skill_score:.1f}/10)"
                )

    # Hard gate: skip picks with less than 8% edge vs the line
    # Uses effective_avg so ace-pitcher matchups can correctly fail the gate
    edge_pct = abs(effective_avg - line) / (line + 1e-9)
    if edge_pct < MIN_EDGE_PCT:
        result = {**pick, **stats}
        result["confidence"] = 0.0
        result["conf_pct"]   = 0
        result["skip_reason"] = f"Edge too small ({edge_pct:.1%} < 8%)"
        return result

    # DNP/Activity gate: skip MLB batter OVER picks where the player has a high
    # rate of zero-production games — strong indicator of bench/platoon role or
    # injury absence. 3259-pick data: 33.4% MLB DNP rate (actual≤0), all auto-losses.
    # p_zero_game = fraction of recent games with 0 production in this stat.
    #
    # Threshold split by stat type:
    #   Binary stats (Hits, Total Bases, Singles): zero always means DNP or 0-fer.
    #     Use 0.20 — any player sitting out 20%+ is too risky for OVER.
    #   Cumulative stats (Runs, HFS, Walks): zero is normal game variance even for
    #     good starters. Use 0.30 — only filter true bench/platoon risk.
    _BINARY_STATS = {"Hits", "Total Bases", "Singles", "Stolen Bases"}
    direction = pick.get("direction", "OVER")
    p_zero = stats.get("p_zero_game", 0.0)
    p_zero_threshold = 0.20 if stat_type in _BINARY_STATS else 0.30
    if (sport == "MLB"
            and direction == "OVER"
            and stat_type not in _PITCHER_STAT_TYPES
            and p_zero > p_zero_threshold):
        result = {**pick, **stats}
        result["confidence"] = 0.0
        result["conf_pct"]   = 0
        result["skip_reason"] = (
            f"High DNP risk: {p_zero:.0%} zero-production games (threshold {p_zero_threshold:.0%})"
        )
        return result

    # Lineup confirmation gate (MLB batters only).
    # When lineups are posted (typically 2-4h before gametime), skip any OVER
    # pick for a batter who isn't in the confirmed batting order — they won't
    # play, making an OVER an auto-loss.
    # Returns None when lineup hasn't been posted yet → fall through normally.
    # Wrapped in try/except so a lineup API failure never blocks scoring.
    if sport == "MLB" and direction == "OVER" and stat_type not in _PITCHER_STAT_TYPES:
        try:
            from data.lineups import is_player_starting as _is_starting
            player_name = pick.get("player", "")
            player_team = stats.get("player_team", "")
            lineup_confirmed = _is_starting(player_name, "MLB", player_team)
            if lineup_confirmed is False:
                result = {**pick, **stats}
                result["confidence"] = 0.0
                result["conf_pct"]   = 0
                result["skip_reason"] = "Not in confirmed batting lineup"
                return result
        except Exception:
            pass  # never let lineup check block scoring

    # 1. Hit rate (20%)
    hit_score = hit_rate

    # 2. Recent form / trend (15%)
    # Positive trend (L3 better than L8) = good; weight L3 vs L8
    trend_score = max(0, min(1.0, 0.5 + trend))

    # 3. Full matchup context (25%) — umpire, arsenal, handedness, opp K rate, park, home/away
    ctx       = _get_full_context(stats, pick)
    matchup_score = ctx.get("context_score", 0.5)

    # 4. Environment (15%) — park factor + Vegas game total + weather + pace
    comps      = ctx.get("components", {})
    pf         = comps.get("park_factor", stats.get("park_factor", 1.0))
    game_total = comps.get("game_total")

    # Park factor
    park_env = 0.5 + (1.0 - pf) * 1.5
    park_env = max(0.1, min(0.9, park_env))

    # Vegas total
    vegas_env = 0.5
    if game_total:
        if sport == "MLB":
            vegas_env = 0.5 + (8.5 - game_total) * 0.04   # low total = pitcher park
        else:
            vegas_env = 0.5 + (game_total - 215.0) * 0.002  # NBA: high total = pace-up
        vegas_env = max(0.2, min(0.8, vegas_env))

    # MLB weather (Open-Meteo, no API key needed)
    weather_env = 0.5
    weather_note = None
    if sport == "MLB":
        try:
            from data.mlb_weather import get_park_weather
            home_name = stats.get("home_name") or ctx.get("home_name", "")
            if not home_name:
                # derive from context: if player is home, their team is home
                home_away = ctx.get("home_away", "")
                opp_team  = ctx.get("opp_team", "")
                # We don't always have home team name directly — use opp if away
                # mlb_batter_stats puts home_name in the game dict but not the stats return
                pass
            # Also try game home_name from stats (added by mlb_batter_stats)
            if not home_name:
                home_name = stats.get("opp_team", "") if stats.get("home_away") == "away" else ""
            w = get_park_weather(home_name) if home_name else None
            if w and not w.get("is_dome"):
                boost = w.get("over_boost", 0.0)
                weather_env = 0.5 + boost * 10   # ±0.05 boost → ±0.5 on 0-1 scale
                weather_env = max(0.2, min(0.8, weather_env))
                if abs(boost) >= 0.02:
                    weather_note = w.get("description", "")
        except Exception:
            pass

    # NBA/WNBA pace adjustment
    pace_env = 0.5
    if sport in ("NBA", "WNBA"):
        try:
            opp_team = ctx.get("opp_team", "")
            if opp_team and opp_team != "unknown":
                pace = _get_nba_team_pace(opp_team)
                if pace:
                    # League avg ~100. Each +1 pace ≈ +1% counting stat volume
                    pace_env = 0.5 + (pace - 100.0) * 0.01
                    pace_env = max(0.3, min(0.7, pace_env))
        except Exception:
            pass
        env_score = (park_env * 0.2 + vegas_env * 0.4 + pace_env * 0.4)
    elif sport == "MLB":
        env_score = (park_env * 0.35 + vegas_env * 0.35 + weather_env * 0.30)
    else:
        env_score = (park_env * 0.5 + vegas_env * 0.5)

    # 5. Opportunity (10%) — lineup position, role, rest, data sample
    inj_mult   = _injury_multiplier(pick["player"], pick["sport"])
    n_games    = stats.get("n_games", MIN_GAMES)
    data_conf  = min(1.0, n_games / 10)

    # WNBA: role stability + usage trend into opportunity score
    if sport == "WNBA":
        role_stab  = stats.get("role_stability", 0.5)
        usage_adj  = stats.get("usage_adj", 1.0)
        usage_trend = stats.get("usage_trend")

        # Role stability: volatile minutes = lower baseline confidence
        stability_base = 0.5 + (role_stab - 0.5) * 0.4   # scales 0.3–0.7

        # Usage trend: getting more shots recently = higher opportunity
        # usage_adj > 1.0 means player is more involved than season average
        usage_opp = min(0.15, max(-0.15, (usage_adj - 1.0) * 0.75))  # ±0.15 range

        opp_score_base = min(0.85, max(0.15, stability_base + usage_opp))
    else:
        opp_score_base = 1.0

    # NBA/WNBA: hard skip reduced-minutes players — not enough role to trust the line
    if sport in ("NBA", "WNBA") and stats.get("minutes_flag") == "reduced":
        result = {**pick, **stats}
        result["confidence"]  = 0.0
        result["conf_pct"]    = 0
        result["skip_reason"] = f"Minutes reduced in playoffs ({stats.get('playoff_min_avg', '?')}mpg) — line set on higher role"
        return result

    # NBA/WNBA rest days penalty (back-to-back = significant fatigue)
    rest_mult = 1.0
    if sport in ("NBA", "WNBA"):
        rest_days = stats.get("rest_days")
        if rest_days == 0:
            rest_mult = 0.88   # back-to-back: ~12% performance drop
        elif rest_days == 1:
            rest_mult = 0.96   # one day rest: minor fatigue

    opp_score = inj_mult * data_conf * rest_mult * opp_score_base

    # 6. Edge size (15%) — uses effective_avg (post median + pitcher adjustment)
    edge_score = min(1.0, edge_pct / 0.30)

    # Surface pitcher adjustment in context notes for MLB batter picks
    if pitcher_adj_note:
        ctx.setdefault("description", []).insert(0, pitcher_adj_note)

    # For walk props: surface Poisson correction + pitcher BB rate in context notes
    if sport == "MLB" and stat_type == "Walks" and stats.get("direction") == "UNDER":
        poisson_p  = stats.get("poisson_p_zero")
        pit_walk   = stats.get("pitcher_walk_adj")
        pit_bb_pct = stats.get("pitcher_bb_pct")
        if poisson_p is not None:
            ctx.setdefault("description", []).append(
                f"📊 Poisson P(0 walks/game): {poisson_p:.0%} — true rate, streak-adjusted"
            )
        if pit_bb_pct is not None and pit_walk is not None:
            risk = "⚠️ HIGH-WALK" if pit_bb_pct > 0.09 else ("⚠️ MODERATE" if pit_bb_pct > 0.07 else "✅ Low-walk")
            ctx.setdefault("description", []).append(
                f"{risk} pitcher — BB%: {pit_bb_pct:.1%}, P(0 walks today): {pit_walk:.0%}"
            )

    # 7. Line movement signal — store adjustment, apply after confidence is computed
    line_movement_note = ""
    _lm_adj = 0.0
    try:
        from line_tracker import line_movement_signal as _lms
        _lm_adj, line_movement_note = _lms(pick["player"], stat_type,
                                           stats.get("direction", "OVER"))
    except Exception:
        pass

    # 8. WNBA-specific matchup enrichments: H2H, home/away splits, opp defense
    if sport == "WNBA":
        direction = stats.get("direction", "OVER")
        line_val  = stats.get("line", 1)

        # H2H vs today's opponent — adjust matchup score
        wnba_h2h = stats.get("wnba_h2h")
        if wnba_h2h and wnba_h2h.get("n", 0) >= 2:
            h2h_avg = wnba_h2h["avg"]
            h2h_edge = (h2h_avg - line_val) / (line_val + 1e-9)
            if direction == "UNDER":
                h2h_edge = -h2h_edge
            h2h_adj = max(-0.06, min(0.06, h2h_edge * 0.25))
            matchup_score = max(0.1, min(0.9, matchup_score + h2h_adj))
            sign = "✅" if h2h_adj > 0 else "⚠️"
            ctx.setdefault("description", []).append(
                f"{sign} vs {ctx.get('opp_team','opp').split()[-1]} H2H: "
                f"avg {h2h_avg} over {wnba_h2h['n']} games"
            )

        # Home/away split from own game log
        ha         = ctx.get("home_away", "unknown")
        split_key  = "home_split" if ha == "home" else "away_split"
        split      = stats.get(split_key)
        if split and split.get("n", 0) >= 4:
            split_avg  = split["avg"]
            split_edge = (split_avg - line_val) / (line_val + 1e-9)
            if direction == "UNDER":
                split_edge = -split_edge
            split_adj = max(-0.04, min(0.04, split_edge * 0.15))
            matchup_score = max(0.1, min(0.9, matchup_score + split_adj))
            sign = "✅" if split_adj > 0 else "⚠️"
            ctx.setdefault("description", []).append(
                f"{sign} {ha.capitalize()} avg: {split_avg} ({split['n']} games)"
            )

        # Opponent defensive context
        opp_def = stats.get("opp_def", {})
        if opp_def.get("avg_allowed") is not None:
            is_fav    = opp_def.get("is_favorable", False)
            avg_alwd  = opp_def["avg_allowed"]
            league_av = opp_def.get("league_avg", avg_alwd)
            def_adj   = (avg_alwd - league_av) / (league_av + 1e-9) * 0.3
            if direction == "UNDER":
                def_adj = -def_adj
            def_adj = max(-0.05, min(0.05, def_adj))
            matchup_score = max(0.1, min(0.9, matchup_score + def_adj))
            opp_name = ctx.get("opp_team", "").split()[-1]
            sign = "✅" if def_adj > 0 else "⚠️"
            ctx.setdefault("description", []).append(
                f"{sign} {opp_name} allows {avg_alwd:.1f} (league avg {league_av:.1f})"
            )

    # 8. Elite rim protector penalty — NBA/WNBA only
    rim_note = ""
    if sport in ("NBA", "WNBA"):
        opp_team_ctx = ctx.get("opp_team", "unknown")
        rim_penalty, rim_note = _check_rim_protector(opp_team_ctx, stats, stat_type)
        if rim_penalty < 1.0:
            matchup_score = matchup_score * rim_penalty
            opp_score     = opp_score * rim_penalty   # fewer minutes/opportunity too
            ctx.setdefault("description", []).append(rim_note)

    # 8. Statcast quality_contact_score — MLB only, blended into matchup score
    statcast_adj  = 0.0
    statcast_note = ""
    if sport == "MLB":
        try:
            from data.mlb_h2h import compute_statcast_quality_score
            sc         = stats.get("statcast", {})
            is_pitcher = pick.get("stat_type", "") in _PITCHER_STAT_TYPES
            sc_result  = compute_statcast_quality_score(sc, is_pitcher=is_pitcher)
            sc_quality = sc_result.get("quality_score", 0.5)
            # Blend Statcast quality into matchup score (20% of matchup weight)
            matchup_score = matchup_score * 0.80 + sc_quality * 0.20
            statcast_adj  = round(sc_quality - 0.5, 3)
            statcast_note = sc_result.get("note", "")
        except Exception:
            pass

    # Weighted composite
    composite = (
        WEIGHTS["hit_rate"]    * hit_score     +
        WEIGHTS["recent_form"] * trend_score   +
        WEIGHTS["matchup"]     * matchup_score +
        WEIGHTS["environment"] * env_score     +
        WEIGHTS["opportunity"] * opp_score     +
        WEIGHTS["edge_size"]   * edge_score
    )

    confidence = 0.50 + composite * 0.45
    if inj_mult == 0:
        confidence = 0.0

    # Home/away splits — surface in output
    splits    = ctx.get("splits", {})
    home_away = ctx.get("home_away", "unknown")
    split_avg = splits.get("home_avg") if home_away == "home" else splits.get("away_avg")
    split_hr  = splits.get("home_hit_rate") if home_away == "home" else splits.get("away_hit_rate")

    # Apply line movement adjustment (computed above but deferred until confidence exists)
    if _lm_adj != 0.0:
        confidence = max(0.0, min(1.0, confidence + _lm_adj))

    # H2H confidence adjustment (MLB only — baked into stats dict)
    h2h_adj = 0.0
    if sport == "MLB":
        h2h_adj = stats.get("h2h_conf_adj", 0.0)
        if h2h_adj != 0.0:
            confidence = max(0.0, min(1.0, confidence + h2h_adj))

    # ── Build projection drivers (ChatGPT recommendation) ─────────────────────
    # Show which factors are pushing the score up or down and by how much.
    drivers = []
    neutral = 0.5

    def _driver(label, score_val, weight):
        lift = (score_val - neutral) * weight
        pct  = int(abs(lift) * 100)
        if pct < 2:
            return  # skip tiny contributions
        sign = "+" if lift > 0 else "−"
        drivers.append((lift, f"{sign}{pct}% {label}"))

    _driver(f"hit rate ({stats.get('over_hits',0) if stats.get('direction')=='OVER' else stats.get('under_hits',0)}/{stats.get('n_games',0)})",
            hit_score, WEIGHTS["hit_rate"])
    _driver(f"matchup ({ctx.get('opp_team','?').split()[-1]})",
            matchup_score, WEIGHTS["matchup"])
    # Show effective_avg in edge driver when pitcher adjustment changed it
    _edge_label = (f"edge (eff {effective_avg:.1f} vs line {stats.get('line',0)})"
                   if effective_avg != avg
                   else f"edge (avg {stats.get('avg',0):.1f} vs line {stats.get('line',0)})")
    _driver(_edge_label, edge_score, WEIGHTS["edge_size"])
    _driver("recent form", trend_score, WEIGHTS["recent_form"])
    _driver("environment", env_score, WEIGHTS["environment"])

    if h2h_adj != 0:
        sign = "+" if h2h_adj > 0 else "−"
        drivers.append((h2h_adj, f"{sign}{int(abs(h2h_adj)*100)}% H2H history"))

    if statcast_adj != 0 and abs(statcast_adj) >= 0.05:
        sign = "+" if statcast_adj > 0 else "−"
        label = statcast_note if statcast_note else "Statcast quality"
        drivers.append((statcast_adj, f"{sign}{int(abs(statcast_adj)*100)}% {label}"))

    drivers.sort(key=lambda x: -abs(x[0]))   # largest magnitude first
    drivers_pos = [d for _, d in drivers if d.startswith("+")][:3]
    drivers_neg = [d for _, d in drivers if d.startswith("−")][:2]

    # NBA: compute vs-opponent avg from existing game logs
    nba_vs_opp_avg = None
    if pick.get("sport") in ("NBA", "WNBA"):
        try:
            opp_team = ctx.get("opp_team", "")
            game_logs = stats.get("_raw_game_logs", [])
            if game_logs and opp_team and opp_team != "unknown":
                # Extract opponent abbreviation from matchup string (e.g. "BOS vs. LAL" → "LAL")
                def _vs_opp_avg(logs, opp_name, stat_fn):
                    opp_word = opp_name.split()[-1].upper()
                    opp_abbr = opp_name if len(opp_name) <= 4 else ""
                    matching = []
                    for g in logs:
                        mu = g.get("matchup", "")
                        if opp_word in mu.upper() or (opp_abbr and opp_abbr in mu):
                            v = stat_fn(g)
                            if v is not None:
                                matching.append(v)
                    if len(matching) >= 2:
                        return round(sum(matching) / len(matching), 1), len(matching)
                    return None, 0

                def _stat_v(g):
                    st = pick.get("stat_type", "")
                    if st == "Points":         return g.get("pts")
                    if st == "Rebounds":       return g.get("reb")
                    if st == "Assists":        return g.get("ast")
                    if st in ("3-Pointers Made", "3PT Made"): return g.get("fg3m")
                    if st == "Steals":         return g.get("stl")
                    if st == "Blocks":         return g.get("blk")
                    if st in ("Pts+Rebs+Asts", "Pts+Reb+Ast"):
                        return (g.get("pts", 0) or 0) + (g.get("reb", 0) or 0) + (g.get("ast", 0) or 0)
                    if st in ("Pts+Rebs", "Pts+Reb"):
                        return (g.get("pts", 0) or 0) + (g.get("reb", 0) or 0)
                    if st in ("Pts+Asts", "Pts+Ast"):
                        return (g.get("pts", 0) or 0) + (g.get("ast", 0) or 0)
                    return None

                nba_vs_opp_avg, nba_vs_opp_n = _vs_opp_avg(game_logs, opp_team, _stat_v)
        except Exception:
            pass

    # NBA Finals volatility discount — Finals defense/game-planning suppresses individual stats
    # more than any model can price in. Raise effective threshold by applying a confidence haircut
    # when a player has playoff games and the round is likely Finals (rest_days >= 7 between games).
    finals_discount = False
    if sport == "NBA":
        pg   = stats.get("playoff_games", 0) or 0
        rest = stats.get("rest_days", 0) or 0
        if pg >= 14 and rest >= 6:
            # Deep in playoffs with long rest between games → Finals cadence (2–4 day gaps)
            confidence    = round(confidence * 0.88, 3)   # ~12% haircut
            finals_discount = True

    # Run projection engine for the projected stat value and edge %
    try:
        from projection_engine import project_pick
        proj = project_pick(pick, stats, ctx)
        # If projection gives a stronger edge signal, blend into confidence
        if proj and not proj.get("skip") and proj.get("edge_pct") is not None:
            proj_edge   = abs(proj["edge_pct"])
            proj_conf   = proj.get("confidence", confidence * 100) / 100
            # Blend: 60% scoring model, 40% projection engine
            confidence  = round(confidence * 0.6 + proj_conf * 0.4, 3)
    except Exception as e:
        _log(f"Projection engine failed for {pick['player']}: {e}")
        proj = {}

    # ── Confidence calibration from historical results ────────────────────────
    # When we have enough resolved picks in a bucket, blend toward the historical
    # hit rate. This corrects systematic over/under-confidence.
    # Blend weight scales with sample size: 0% at 0, ~50% at 50, max 70% at 100+.
    cal_note = ""
    try:
        from calibration_tracker import (get_calibration_adjustments,
                                          BOOTSTRAP_MIN_N, BLEND_MAX, BLEND_FULL_N)
        cal = get_calibration_adjustments()   # uses BOOTSTRAP_MIN_N automatically
        if cal:
            conf_pct_now = int(confidence * 100)
            for label, cdata in cal.items():
                lo, hi = map(int, label.split("-"))
                if lo <= conf_pct_now < hi:
                    hist_rate  = cdata["hist_rate"]
                    n_bucket   = cdata["n"]
                    # Blend formula: ramp from 0% at BOOTSTRAP_MIN_N to BLEND_MAX at BLEND_FULL_N
                    # At n=5:  blend ≈  0% (barely anything, avoid pure noise)
                    # At n=10: blend ≈ 14% (modest early correction)
                    # At n=20: blend ≈ 30% (meaningful correction after ~3 days)
                    # At n=50: blend ≈ 70% (full weight after ~1 week)
                    blend = min(BLEND_MAX,
                                max(0.0, (n_bucket - BOOTSTRAP_MIN_N)
                                         / (BLEND_FULL_N - BOOTSTRAP_MIN_N)
                                         * BLEND_MAX))
                    if blend > 0.02:   # only adjust if blend is meaningful
                        calibrated = round(confidence * (1 - blend) + hist_rate * blend, 3)
                        delta_dir  = "↑" if calibrated > confidence else "↓"
                        cal_note   = (
                            f"Cal {delta_dir} {confidence:.0%}→{calibrated:.0%} "
                            f"(hist {hist_rate:.0%}, n={n_bucket}, blend={blend:.0%})"
                        )
                        confidence = calibrated
                    break
    except Exception:
        pass

    # ── Per-stat-type calibration correction ──────────────────────────────────
    # After enough data accumulates in Supabase, the model learns stat-specific
    # overconfidence. E.g. "Singles is +25% overconfident based on 80 resolved picks."
    # Apply a blend toward the historical real_hit_rate, weighted by sample size.
    #
    # This runs AFTER the bucket calibration above, so it's additive.
    # Only fires when stat_calibration table has ≥ 15 picks for this stat type.
    try:
        from calibration_tracker import (get_stat_calibration as _gsc,
                                          BOOTSTRAP_MIN_N, BLEND_MAX, BLEND_FULL_N)
        _sc = _gsc(sport, stat_type)
        if _sc and _sc.get("overconfidence") is not None:
            _overconf  = _sc["overconfidence"]   # positive = model too high
            _sc_n      = _sc.get("n", 0)
            # Same fast-ramp blend formula as bucket calibration
            _sc_blend  = min(BLEND_MAX,
                             max(0.0, (_sc_n - BOOTSTRAP_MIN_N)
                                      / (BLEND_FULL_N - BOOTSTRAP_MIN_N)
                                      * BLEND_MAX))
            if _sc_blend > 0.05 and abs(_overconf) > 0.03:
                _sc_target  = _sc["real_hit_rate"]
                _sc_cal     = round(confidence * (1 - _sc_blend) + _sc_target * _sc_blend, 3)
                _sc_dir     = "↓" if _sc_cal < confidence else "↑"
                cal_note    = (cal_note + " " if cal_note else "") + (
                    f"StatCal{_sc_dir} {confidence:.0%}→{_sc_cal:.0%} "
                    f"({stat_type} hist {_sc_target:.0%}, n={_sc_n})"
                )
                confidence = _sc_cal
    except Exception:
        pass

    # ── Sport-level calibration (empirical, from 902 resolved picks 2026-06-08) ──
    # Applied after all per-bucket and per-stat corrections.
    # Only fires for OVER picks — UNDER is banned from parlays anyway (29% hit rate).
    direction = stats.get("direction", pick.get("direction", "OVER"))
    if direction == "OVER":
        # WNBA OVER qualified hit rate: 62% vs MLB OVER: 55%
        # Adjust confidence to match observed sport reality.
        _sport_adj = {
            "WNBA": +0.03,   # outperforms — slight boost
            "MLB":  -0.03,   # underperforms — slight haircut
            "NBA":   0.00,   # insufficient data yet
        }
        _adj = _sport_adj.get(sport, 0.0)
        if _adj != 0.0:
            confidence = round(min(0.95, max(0.35, confidence + _adj)), 3)

        # ── Stat-type signal boosts (best performing categories from data) ─────
        # MLB Walks: 60% actual hit rate  (10 picks) — genuine edge
        # MLB Walks Allowed: 58% actual hit rate  (12 picks) — genuine edge
        # Both consistently outperform all other MLB categories.
        if sport == "MLB" and stat_type in ("Walks", "Walks Allowed"):
            confidence = round(min(0.95, confidence * 1.05), 3)

    # ── Probability distribution engine ──────────────────────────────────────────
    # Model the stat as normally distributed around the projection.
    # P(over line) = 1 − Φ((line − projection) / σ)
    #
    # σ sources (best → fallback):
    #   1. Calibration MAE → σ ≈ MAE / 0.798  (for normal: MAE = σ√(2/π))
    #   2. Player's own stat_std_dev from their game log
    #   3. Half the proj_low/proj_high range
    #
    # For WNBA (where we have a real projected_stat), blend the distribution
    # probability into confidence at 45% weight. Other sports keep their current
    # composite scoring model unchanged.
    p_over  = None
    p_under = None
    # WNBA: per-minute projection (most accurate — real minutes model).
    # MLB batter: fall back to effective_avg (pitcher-adjusted projection center).
    # MLB pitcher / NBA: no probability blending (insufficient σ signal).
    proj_stat_val = stats.get("projected_stat")
    if proj_stat_val is None and sport == "MLB" and stat_type not in _PITCHER_STAT_TYPES:
        proj_stat_val = effective_avg if (effective_avg and effective_avg > 0) else None
    if proj_stat_val is not None and proj_stat_val > 0 and line > 0:
        import math as _math
        _std = None
        # Source 1: calibration MAE
        try:
            from calibration_tracker import get_stat_mae as _gsmae
            _mae = _gsmae(sport, stat_type, min_n=8)
            if _mae and _mae > 0.3:
                _std = _mae / 0.798   # MAE = σ · √(2/π) ≈ 0.798σ  →  σ = MAE/0.798
        except Exception:
            pass
        # Source 2: player stat std dev
        if not _std or _std < 0.5:
            _std = stats.get("stat_std_dev") or 0.0
        # Source 3: projection range half-width
        if (_std or 0) < 0.5:
            _pl, _ph = stats.get("proj_low"), stats.get("proj_high")
            if _pl is not None and _ph is not None:
                _std = max(0.5, (_ph - _pl) / 2.0)

        # ── Sport-specific probability computation ────────────────────────────
        #
        # WNBA: Gaussian Normal(projected_stat, σ) — per-minute projection is
        #   the most direct estimate available; σ from calibration MAE.
        #
        # MLB batter: Zero-inflated mixture model — MLB outcomes are NOT Gaussian.
        #   Two components:
        #     1. Point mass at zero: P(zero game) — 0-for-4, 0 FS, 0 TB, etc.
        #     2. Conditional distribution on non-zero games: right-skewed
        #   P(stat > line) = P(non-zero) × P(non-zero stat > line | non-zero)
        #   P(stat ≤ line) = P(zero) + P(non-zero) × P(non-zero stat ≤ line)
        #
        #   Pitcher quality adjusts P(zero) via zero_inflation_factor — this
        #   simultaneously corrects both the mean AND variance without separate
        #   σ scaling (which would double-count the same pitcher signal).

        if sport == "WNBA" and _std and _std > 0.3:
            # Gaussian model — valid for WNBA per-minute projections
            _z    = (line - proj_stat_val) / _std
            _cdf  = 0.5 * (1 + _math.erf(_z / _math.sqrt(2)))
            p_over  = round(max(0.01, min(0.99, 1.0 - _cdf)), 3)
            p_under = round(max(0.01, min(0.99, _cdf)), 3)
            _p_model = p_over if direction == "OVER" else p_under
            if 0.05 < _p_model < 0.95:
                confidence = round(confidence * 0.55 + _p_model * 0.45, 3)

        elif sport == "MLB" and stat_type not in _PITCHER_STAT_TYPES:
            p_zero_base  = float(stats.get("p_zero_game") or 0.0)
            nonzero_mean = stats.get("nonzero_mean")
            nonzero_std  = stats.get("nonzero_std")

            # ── Binary threshold shortcut ────────────────────────────────────
            # For OVER/UNDER X.5 lines where the stat is a low-count integer
            # (Singles, Hits, HR, etc.), the question is simply "does he get
            # at least 1?" — a binary event.  The Gaussian model, especially
            # when using the median as center, badly overstates probability
            # when nonzero_std ≈ 0 (player always gets exactly 1 when he hits).
            #
            # Example: Crews Singles OVER 0.5 — median=1.0, σ=0.49 → 84%.
            #   But history says 8/12 = 67%.  The median reflects the typical
            #   non-zero game, not the threshold probability.
            #
            # Fix: when the line is half-integer (X.5) AND nonzero_std < 0.3
            # (near-zero variance in non-zero games), use hit_rate directly as
            # p_over/p_under.  That IS the empirical P(stat ≥ 1).
            _is_half_line = abs(line - round(line) - 0.5) < 0.01   # e.g. 0.5, 1.5, 2.5
            # nonzero_std=0.0 is falsy — must use explicit None check, not "or" fallback
            _nonzero_std_low = nonzero_std is not None and nonzero_std < 0.3

            # ── Poisson model for rare integer-count stats (X.5 lines) ──────────
            # Stats like Singles, Hits, HR, RBI follow a Poisson process per game.
            # P(at least 1) = 1 − e^(−λ)  where λ = avg per game.
            # The Gaussian/zero-inflated model systematically overstates these
            # because it's fooled by edge_size (avg >> 0.5) and recent streaks.
            #
            # Measured overconfidence (model − real hit rate):
            #   Singles:  +25%   HR: ~+15%   Hits: +4%
            #
            # Fix: for X.5 lines on Poisson-appropriate stat types, use the
            # Poisson probability directly and blend 70% Poisson / 30% hit_rate.
            # The blend respects that hot/cold streaks are real (not just noise)
            # while anchoring to the theoretically correct base rate.
            _POISSON_STAT_TYPES = {"Singles", "Home Runs", "Stolen Bases"}
            if (_is_half_line and stat_type in _POISSON_STAT_TYPES
                    and proj_stat_val and proj_stat_val > 0):
                _lam = float(proj_stat_val)   # Poisson λ = avg per game
                _p_pois_over  = 1.0 - _math.exp(-_lam)
                _p_pois_under = _math.exp(-_lam)
                _hr = float(stats.get("hit_rate", 0.5))
                # 70% Poisson (theoretically correct) + 30% empirical hit rate (form signal)
                _p_ov_blend = 0.70 * _p_pois_over  + 0.30 * _hr
                _p_un_blend = 0.70 * _p_pois_under + 0.30 * (1.0 - _hr)
                p_over  = round(max(0.01, min(0.99, _p_ov_blend)), 3)
                p_under = round(max(0.01, min(0.99, _p_un_blend)), 3)

            elif _is_half_line and _nonzero_std_low and stats.get("hit_rate") is not None:
                # Use empirical hit rate — it IS P(over X.5) for half-integer lines
                _hr = float(stats["hit_rate"])
                p_over  = round(max(0.01, min(0.99, _hr)), 3)
                p_under = round(max(0.01, min(0.99, 1.0 - _hr)), 3)

            elif (p_zero_base > 0 and nonzero_mean is not None
                    and nonzero_std and nonzero_std > 0.3):
                # Zero-inflated model for stats with real spread in non-zero games
                p_zero_adj = p_zero_base
                _ps = stats.get("pitcher_skill_score")
                if _ps is not None:
                    try:
                        from data.mlb_pitcher_strength import pitcher_zero_inflation_factor as _pzif
                        p_zero_adj = min(0.85, p_zero_base * _pzif(float(_ps)))
                    except Exception:
                        pass
                p_nonzero = 1.0 - p_zero_adj

                # P(stat > line | non-zero game): Gaussian on non-zero component
                _z_cond   = (line - nonzero_mean) / nonzero_std
                _cdf_cond = 0.5 * (1 + _math.erf(_z_cond / _math.sqrt(2)))
                _p_ov_nz  = max(0.005, min(0.995, 1.0 - _cdf_cond))
                _p_un_nz  = max(0.005, min(0.995, _cdf_cond))

                p_over_raw  = p_nonzero * _p_ov_nz
                p_under_raw = p_zero_adj + p_nonzero * _p_un_nz
                _total = p_over_raw + p_under_raw
                if _total > 0:
                    p_over  = round(max(0.01, min(0.99, p_over_raw  / _total)), 3)
                    p_under = round(max(0.01, min(0.99, p_under_raw / _total)), 3)

            elif _std and _std > 0.3:
                # Gaussian fallback for continuous-ish stats (HFS, Total Bases, etc.)
                _z    = (line - proj_stat_val) / _std
                _cdf  = 0.5 * (1 + _math.erf(_z / _math.sqrt(2)))
                p_over  = round(max(0.01, min(0.99, 1.0 - _cdf)), 3)
                p_under = round(max(0.01, min(0.99, _cdf)), 3)

            # Blend into confidence at 35%
            if p_over is not None and p_under is not None:
                _mlb_dir = stats.get("direction", "OVER")
                _p_model = p_over if _mlb_dir == "OVER" else p_under
                if 0.05 < _p_model < 0.95:
                    confidence = round(confidence * 0.65 + _p_model * 0.35, 3)

    # ── MLB game state vector (shared per-game signal for correlation) ───────
    # Computed once here, stored in result so build_parlays() can use it when
    # checking if two legs share the same game → correlation adjustment.
    game_state = {}
    if sport == "MLB":
        try:
            from data.mlb_game_state import compute_game_state as _cgs
            game_state = _cgs(
                pitcher_skill_score = stats.get("pitcher_skill_score"),
                park_factor         = stats.get("park_factor") or comps.get("park_factor", 1.0),
                game_total          = comps.get("game_total"),
                pitcher_k_pct       = stats.get("pitcher_k_pct"),
            )
        except Exception:
            pass

    result = {**pick, **stats}
    result["hit_score"]       = round(hit_score, 3)
    result["edge_pct"]        = round(edge_pct, 4)   # raw edge fraction (e.g. 0.25 = 25%)
    result["edge_score"]      = round(edge_score, 3)
    result["trend_score"]     = round(trend_score, 3)
    result["opp_score"]       = round(opp_score, 3)
    result["sit_score"]       = round(opp_score, 3)
    result["composite"]       = round(composite, 3)
    result["confidence"]      = round(confidence, 3)
    result["conf_pct"]        = int(confidence * 100)
    result["home_away"]       = home_away
    result["opp_team"]        = ctx.get("opp_team", "unknown")
    result["context_notes"]   = ctx.get("description", [])
    result["split_avg"]       = split_avg
    result["split_hit_rate"]  = split_hr
    result["projection"]      = proj
    result["nba_vs_opp_avg"]  = nba_vs_opp_avg
    result["weather_note"]    = weather_note
    result["rest_days"]       = stats.get("rest_days")
    result["minutes_flag"]    = stats.get("minutes_flag")
    result["drivers_pos"]     = drivers_pos
    result["drivers_neg"]     = drivers_neg
    result["statcast_note"]   = statcast_note
    result["rim_note"]          = rim_note
    result["playoff_games"]     = stats.get("playoff_games")
    result["playoff_min_avg"]   = stats.get("playoff_min_avg")
    result["finals_discount"]     = finals_discount
    result["line_movement_note"]  = line_movement_note
    # WNBA projection engine fields
    result["projected_stat"]    = stats.get("projected_stat")
    result["proj_low"]          = stats.get("proj_low")
    result["proj_high"]         = stats.get("proj_high")
    result["projected_minutes"] = stats.get("projected_minutes")
    result["min_std_dev"]       = stats.get("min_std_dev")
    result["role_stability"]    = stats.get("role_stability")
    result["usage_fga_per_game"]= stats.get("usage_fga_per_game")
    result["wnba_h2h"]          = stats.get("wnba_h2h")
    result["home_split"]        = stats.get("home_split")
    result["away_split"]        = stats.get("away_split")
    result["injury_note"]              = stats.get("injury_note", "")
    result["injury_impact"]            = stats.get("injury_impact", {})
    result["injury_adjustment_source"] = stats.get("injury_impact", {}).get("injury_adjustment_source", "")
    result["usage_trend"]              = stats.get("usage_trend")
    result["usage_adj"]                = stats.get("usage_adj", 1.0)
    result["usage_confidence"]         = stats.get("usage_confidence", 1.0)
    result["cal_note"]                 = cal_note
    result["p_over"]                   = p_over
    result["p_under"]                  = p_under
    # MLB pitcher strength prior fields
    result["pitcher_skill_score"]      = stats.get("pitcher_skill_score")
    result["pitcher_k_pct"]            = stats.get("pitcher_k_pct")
    result["pitcher_tier"]             = stats.get("pitcher_tier", "")
    result["difficulty_multiplier"]    = stats.get("difficulty_multiplier", 1.0)
    result["pitcher_skill_desc"]       = stats.get("pitcher_skill_desc", "")
    result["effective_avg"]            = effective_avg
    result["median_val"]               = stats.get("median_val")
    # Game-level latent state (shared by all players in the same MLB game)
    result["game_state"]               = game_state
    # Lineup context (for lineup correlation model)
    result["batting_order"]            = stats.get("batting_order")
    result["player_team"]              = stats.get("player_team", "")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Build optimal parlays
# ─────────────────────────────────────────────────────────────────────────────

MIN_EV = 0.03    # minimum 3% positive EV to include a parlay (+$0.03 per $1 wagered)
MIN_P_HIT = 0.68 # minimum per-leg probability (distribution model or confidence fallback)


def _get_p_hit(pick: dict) -> float:
    """
    Best available P(hit) estimate for a single pick leg.

    Priority:
      1. p_over / p_under from the distribution model (zero-inflated MLB or WNBA projection).
         These are actual probabilities from a calibrated statistical model.
      2. confidence score (composite heuristic) — less precise but widely available.

    Using the distribution probability here (rather than confidence) makes the
    parlay EV calculation honest: EV = P(parlay) × payout - 1 is only meaningful
    if P(parlay) is a real probability, not a heuristic score.
    """
    direction = pick.get("direction", "OVER")
    p_over    = pick.get("p_over")
    p_under   = pick.get("p_under")
    if direction == "OVER" and p_over and 0.05 < p_over < 0.99:
        return p_over
    if direction == "UNDER" and p_under and 0.05 < p_under < 0.99:
        return p_under
    return pick.get("confidence", 0.62)


def build_parlays(scored_picks: list[dict], max_legs: int = MAX_PARLAY) -> list[dict]:
    """
    Build optimal parlays from scored picks, ranked by true expected value.

    Uses per-leg P(hit) from the distribution model (p_over / p_under when
    available, otherwise confidence) — not the composite heuristic score.
    Applies correlation adjustment for same-game legs before computing EV.

    Filters:
    - confidence >= MIN_CONF (qualitative model agrees pick is good)
    - p_hit >= MIN_P_HIT (probability model agrees line is clearable)
    - Parlay EV >= MIN_EV (positive expected value, not just positive probability)
    """
    # Filter: qualitative model, empirical history, AND probability model must all agree.
    # Mirrors parlay_builder.py eligible filter so debug log matches what's actually sent.
    from parlay_builder import EXCLUDED_STAT_TYPES as _EXCL, PARLAY_OVERS_ONLY as _OO
    eligible = [
        p for p in scored_picks
        if p["confidence"] >= MIN_CONF
        and p.get("hit_rate", 0) >= 0.65
        and _get_p_hit(p) >= MIN_P_HIT
        and p.get("stat_type", "") not in _EXCL
        and (not _OO or p.get("direction") == "OVER")
    ]
    eligible.sort(key=lambda x: _get_p_hit(x), reverse=True)

    # Deduplicate by game: keep highest-confidence pick per game
    by_game: dict[str, dict] = {}
    for pick in eligible:
        gid = pick.get("game_id", pick["player"])
        if gid not in by_game or pick["confidence"] > by_game[gid]["confidence"]:
            by_game[gid] = pick
    pool = list(by_game.values())

    # Import unified correlation model once outside the loop.
    # joint_game_correlation_factor() combines pitcher suppression + lineup
    # cascade with overlap correction so they don't double-count shared variance.
    try:
        from data.mlb_game_state import joint_game_correlation_factor as _joint_corr_fn
    except Exception:
        _joint_corr_fn = None
    # Keep individual functions as fallback
    try:
        from data.mlb_game_state import correlation_factor_same_game as _corr_fn
    except Exception:
        _corr_fn = None
    try:
        from data.mlb_game_state import lineup_correlation_factor as _lineup_corr_fn
    except Exception:
        _lineup_corr_fn = None

    parlays = []
    for n_legs in range(2, min(max_legs + 1, len(pool) + 1)):
        payout = PP_PAYOUTS.get(n_legs, 20)
        be     = PP_BREAKEVEN.get(n_legs, 0.05)

        best_ev = -999
        best_combo   = None
        best_p_win   = 0.0
        best_p_win_raw = 0.0
        best_corr    = 1.0
        # Limit combos to top 20 picks to avoid explosion
        top_pool = pool[:20]
        for combo in itertools.combinations(top_pool, n_legs):
            # Use distribution-model probability per leg (p_over/p_under), not confidence
            p_win = 1.0
            for leg in combo:
                p_win *= _get_p_hit(leg)

            # ── Unified correlation adjustment (overlap-corrected) ────────────
            # Uses joint_game_correlation_factor() which combines pitcher
            # suppression + lineup cascade while correcting for their shared
            # variance (pitcher dominance already implies weaker RBI chains).
            #
            # Prevents the naive pitcher_factor × lineup_factor overcounting
            # that made cancellation scenarios look artificially neutral.
            corr_factor = 1.0
            if _joint_corr_fn is not None:
                try:
                    corr_factor = _joint_corr_fn(list(combo))
                except Exception:
                    # Fallback: multiply individual factors (old behavior)
                    if _corr_fn is not None:
                        try: corr_factor *= _corr_fn(list(combo))
                        except Exception: pass
                    if _lineup_corr_fn is not None:
                        try: corr_factor *= _lineup_corr_fn(list(combo))
                        except Exception: pass
            p_win_adj = p_win * corr_factor

            ev = p_win_adj * payout - 1.0   # expected return on $1

            # Diversity bonus: picks from different sports are truly independent
            sports_in = {leg["sport"] for leg in combo}
            if len(sports_in) > 1:
                ev *= 1.05   # 5% bonus for cross-sport

            if ev > best_ev:
                best_ev      = ev
                best_combo   = combo
                best_p_win   = p_win_adj
                best_p_win_raw = p_win
                best_corr    = corr_factor

        if best_combo and best_ev >= MIN_EV:
            corr_note = ""
            if best_corr < 0.93:
                corr_note = f"⚠️ Correlation penalty ×{best_corr:.2f} (same-game ace suppression)"
            elif best_corr < 0.97:
                corr_note = f"⚠️ Correlation penalty ×{best_corr:.2f}"
            elif best_corr > 1.06:
                corr_note = f"✅ Lineup cascade bonus ×{best_corr:.2f} (adjacent batting positions)"
            elif best_corr > 1.02:
                corr_note = f"✅ Correlation bonus ×{best_corr:.2f}"

            # Per-leg breakdown for output
            leg_breakdown = []
            for leg in best_combo:
                p_h = _get_p_hit(leg)
                p_src = "dist" if leg.get("p_over") or leg.get("p_under") else "conf"
                leg_breakdown.append({
                    "player":    leg["player"],
                    "direction": leg["direction"],
                    "line":      leg["line"],
                    "stat_type": leg["stat_type"],
                    "p_hit":     round(p_h, 3),
                    "p_hit_pct": int(p_h * 100),
                    "p_src":     p_src,          # "dist" = distribution model, "conf" = fallback
                    "conf_pct":  leg["conf_pct"],
                    "hit_rate":  round(leg.get("hit_rate", 0), 3),
                    "n_games":   leg.get("n_games", 0),
                    "over_hits": leg.get("over_hits", 0),
                    "under_hits":leg.get("under_hits", 0),
                    "sport":     leg["sport"],
                    "game_id":   leg.get("game_id", ""),
                })

            parlays.append({
                "legs":          list(best_combo),
                "leg_breakdown": leg_breakdown,
                "n_legs":        n_legs,
                "payout":        payout,
                "p_win":         round(best_p_win, 3),
                "p_win_raw":     round(best_p_win_raw, 3),
                "corr_factor":   best_corr,
                "corr_note":     corr_note,
                "ev":            round(best_ev, 3),
                "ev_pct":        int(best_ev * 100),
                "ev_rating":     ("HIGH" if best_ev >= 0.20 else
                                  "MED-HIGH" if best_ev >= 0.10 else
                                  "MED" if best_ev >= 0.05 else "LOW"),
                "sports":        sorted({leg["sport"] for leg in best_combo}),
            })

    parlays.sort(key=lambda x: x["ev"], reverse=True)
    return parlays


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Format and send notifications
# ─────────────────────────────────────────────────────────────────────────────

SPORT_EMOJI = {"NBA": "🏀", "MLB": "⚾", "WNBA": "🏀", "TENNIS": "🎾", "SOCCER": "⚽", "NHL": "🏒"}

def _why_string(p: dict) -> str:
    """One-line human reason for the pick."""
    hits      = p["over_hits"] if p["direction"] == "OVER" else p["under_hits"]
    n         = p["n_games"]
    avg       = p["avg"]
    line      = p["line"]
    direction = p["direction"]
    rec       = p.get("recent_values", [])[:5]
    gap       = abs(avg - line)
    gap_pct   = int(gap / (line + 1e-9) * 100)

    trend_note = ""
    if p.get("trend", 0) > 0.10:
        trend_note = " · trending UP"
    elif p.get("trend", 0) < -0.10:
        trend_note = " · trending DOWN"

    return (
        f"{hits}/{n} games hit · avg {avg} vs line {line} "
        f"({gap_pct}% gap) · L5: {rec}{trend_note}"
    )

# Stat types with confirmed positive edge from calibration data
_SIGNAL_STATS = {"Walks", "Walks Allowed", "Runs", "Hits", "Total Bases"}

def _format_push_body(top_picks: list[dict], top_parlay: dict | None) -> str:
    lines = []
    for p in top_picks[:5]:
        arrow = "📈" if p["direction"] == "OVER" else "📉"
        e     = SPORT_EMOJI.get(p["sport"], "🎯")
        why   = _why_string(p)
        # Star flag for our highest-signal stat types
        star  = " ★" if p.get("stat_type") in _SIGNAL_STATS else ""
        lines.append(
            f"{e}{arrow} {p['player']} {p['direction']} {p['line']} {p['stat_type']}{star} ({p['conf_pct']}%)\n"
            f"   {why}"
        )

    if top_parlay:
        legs = " + ".join(
            f"{l['player']} {l['direction']} {l['line']} {l['stat_type']}"
            for l in top_parlay["legs"]
        )
        lines.append(
            f"\n🎯 Best parlay ({top_parlay['n_legs']}-leg, {top_parlay['payout']}x):\n"
            f"   {legs}\n"
            f"   Win prob: {int(top_parlay['p_win']*100)}% | EV: +{top_parlay['ev_pct']}%"
        )

    return "\n".join(lines)

def _format_discord_embed(top_picks: list[dict], parlays: list[dict],
                          goblin_parlays: list[dict] = None,
                          demon_parlays:  list[dict] = None) -> dict:
    """Build a rich Discord embed with full player names and clear reasoning."""
    fields = []

    # Top individual picks — full name, full stat, full reason
    for p in top_picks[:8]:
        arrow    = "📈" if p["direction"] == "OVER" else "📉"
        e        = SPORT_EMOJI.get(p["sport"], "🎯")
        conf_bar = "🟢" if p["confidence"] >= 0.75 else "🟡"
        hits     = p["over_hits"] if p["direction"] == "OVER" else p["under_hits"]
        n        = p["n_games"]
        avg      = p["avg"]
        line     = p["line"]
        rec      = p.get("recent_values", [])[:5]
        gap      = abs(avg - line)
        gap_pct  = int(gap / (line + 1e-9) * 100)
        l3_avg   = p.get("avg_l3", avg)
        trend_arrow = "↗️" if p.get("trend", 0) > 0.10 else ("↘️" if p.get("trend", 0) < -0.10 else "➡️")

        # Matchup context line
        home_away   = p.get("home_away", "unknown")
        opp_team    = p.get("opp_team", "")
        ctx_notes   = p.get("context_notes", [])
        split_avg   = p.get("split_avg")
        split_hr    = p.get("split_hit_rate")
        loc_emoji   = "🏠" if home_away == "home" else ("✈️" if home_away == "away" else "")
        opp_str     = f" vs {opp_team}" if opp_team and opp_team != "unknown" else ""
        split_str   = ""
        if split_avg is not None and home_away != "unknown":
            split_str = f" · {home_away.capitalize()} avg: **{split_avg}**"
            if split_hr is not None:
                split_str += f" ({int(split_hr*100)}% hit rate)"
        ctx_str = " · ".join(ctx_notes[:2]) if ctx_notes else ""

        # Projection engine output
        proj       = p.get("projection", {})
        projected  = proj.get("projected")
        edge_pct   = proj.get("edge_pct_pct", gap_pct)
        proj_str   = f"**{projected}** projected" if projected else f"**{avg}** avg"
        factors_pos = proj.get("factors_pos", [])
        factors_neg = proj.get("factors_neg", [])
        factors_neu = proj.get("factors_neu", [])
        all_factors = factors_pos + factors_neg + factors_neu

        stat_flag = " ★" if p.get("stat_type") in _SIGNAL_STATS else ""
        fields.append({
            "name":  f"{e}{arrow} {p['player']} — {p['direction']} {p['line']} {p['stat_type']}{stat_flag}",
            "value": (
                f"{conf_bar} **{p['conf_pct']}% confidence** · {p['sport']}\n"
                f"📐 Line: **{line}** · {proj_str} · Edge: **{edge_pct}%**\n"
                f"📊 Hit rate: **{hits}/{n}** past games · Avg: **{avg}** · L5: `{rec}`\n"
                f"{loc_emoji} {home_away.capitalize()}{opp_str}{split_str}\n"
                + ("\n".join(f"  {f}" for f in all_factors[:4]) if all_factors else "")
            ).strip(),
            "inline": False,
        })

    # Parlay recommendations — full breakdown with per-leg P(hit), EV, and Kelly sizing
    if parlays:
        has_kelly = "bet_size" in parlays[0]
        for par in parlays[:4]:
            leg_lines = []
            # Prefer leg_summary (from parlay_builder) over leg_breakdown (old format)
            leg_data = par.get("leg_summary") or par.get("leg_breakdown") or []
            raw_legs = par.get("legs", [])
            for i, ls in enumerate(leg_data):
                p_hit_pct = ls.get("p_hit_pct") or ls.get("conf_pct", 0)
                hit_rate  = ls.get("hit_rate", 0)
                n         = ls.get("n_games", 0)
                avg       = ls.get("avg", "?")
                recent    = ls.get("recent_5") or ls.get("recent_values", [])[:5]
                p_src_tag = " 📊" if ls.get("p_src") in ("model", "dist") else ""
                # For old format, compute hit count from raw legs
                if raw_legs and i < len(raw_legs):
                    rl = raw_legs[i]
                    hist_str = f"{rl.get('over_hits',0) if rl.get('direction')=='OVER' else rl.get('under_hits',0)}/{n} hist"
                else:
                    hist_str = f"{int(hit_rate*100)}% HR"
                leg_lines.append(
                    f"• {ls['player']} {ls['direction']} **{ls['line']}** {ls['stat_type']}\n"
                    f"  P(hit): **{p_hit_pct}%**{p_src_tag} · {hist_str} · avg {avg} · L5:{recent}"
                )

            ev_rating  = par.get("ev_rating", "")
            corr       = par.get("corr", par.get("corr_factor", 1.0))
            p_win      = par["p_win"]
            p_win_raw  = par.get("p_win_raw", p_win)
            corr_note  = par.get("corr_note", "")

            # Kelly sizing line
            sizing_str = ""
            if has_kelly:
                bet = par.get("bet_size", 0)
                win = par.get("win_amount", 0)
                net = par.get("net_profit", 0)
                k_full = par.get("kelly_full_pct", 0)
                k_frac = par.get("kelly_frac_pct", 0)
                sizing_str = (
                    f"\n💵 **Bet: ${bet:.2f}** → Win: **${win:.2f}** (+${net:.2f})\n"
                    f"   Kelly: {k_full:.1f}% full → {k_frac:.1f}% fractional"
                )

            fields.append({
                "name":  f"🎯 {par['n_legs']}-Leg Parlay — {par['payout']:.0f}x | EV: +{par['ev_pct']}% [{ev_rating}]",
                "value": (
                    "\n".join(leg_lines) + "\n"
                    f"P(win): **{int(p_win*100)}%**\n"
                    f"**EV: +{par['ev_pct']}%**"
                    + (f" | Sports: {', '.join(par.get('sports', []))}" if par.get("sports") else "")
                    + sizing_str
                    + (f"\n{corr_note}" if corr_note else "")
                ),
                "inline": False,
            })

    # ── Goblin parlay section ────────────────────────────────────────────────────
    for gp in (goblin_parlays or [])[:1]:
        leg_lines = [
            f"🟢 {ls['player']} OVER **{ls['line']}** {ls['stat_type']} "
            f"({int(ls['hit_rate']*100)}% HR · avg {ls['avg']})"
            for ls in gp["leg_summary"]
        ]
        fields.append({
            "name":  f"🧌 GOBLIN — {gp['n_legs']}-Leg | ~{gp['payout']}x | P(win)={int(gp['p_win']*100)}%",
            "value": (
                "\n".join(leg_lines) + "\n"
                f"💵 **Bet: ${gp['bet_size']}** → ~**${gp['win_amount']}**\n"
                f"_{gp.get('note', 'Check app for exact multiplier')}_"
            ),
            "inline": False,
        })

    # ── Demon parlay section ─────────────────────────────────────────────────────
    for dp in (demon_parlays or [])[:1]:
        leg_lines = [
            f"🔴 {ls['player']} {ls['direction']} **{ls['line']}** {ls['stat_type']} "
            f"({int(ls['hit_rate']*100)}% HR · avg {ls['avg']})"
            for ls in dp["leg_summary"]
        ]
        fields.append({
            "name":  f"😈 DEMON — {dp['n_legs']}-Leg | ~{dp['payout']}x | P(win)={int(dp['p_win']*100)}%",
            "value": (
                "\n".join(leg_lines) + "\n"
                f"💵 **Bet: ${dp['bet_size']}** → ~**${dp['win_amount']}** 🚀\n"
                f"_{dp.get('note', 'Check app for exact multiplier')}_"
            ),
            "inline": False,
        })

    now_et = datetime.now(timezone.utc) - timedelta(hours=4)
    ts     = now_et.strftime("%B %d, %Y at %I:%M %p ET")

    return {
        "embeds": [{
            "title":       f"⚡ Power Parlay Report — {ts}",
            "description": (f"**{len(top_picks)} edges found** across "
                            f"{len({p['sport'] for p in top_picks})} sports. "
                            f"Standard + Goblin + Demon lineups below."),
            "color":       3066993,
            "fields":      fields[:25],
            "footer":      {
                "text": (f"SharpLines · Hit rates from last "
                         f"{max((p.get('n_games',5) for p in top_picks), default=5)} games · "
                         f"Standard lines only")
            },
        }]
    }

def _send_notifications(top_picks: list[dict], parlays: list[dict],
                        bankroll: float = 50.0,
                        goblin_parlays: list[dict] = None,
                        demon_parlays:  list[dict] = None):
    """Send push notification + Discord embeds."""
    from notify import send_push, send_discord

    # If parlays have Kelly sizing (from parlay_builder), use compact Kelly format
    has_kelly = parlays and "bet_size" in parlays[0]
    if has_kelly:
        try:
            from parlay_builder import format_parlay_ntfy as _fpn
            push_title, push_body = _fpn(parlays, bankroll,
                                         goblin_parlays=goblin_parlays,
                                         demon_parlays=demon_parlays,
                                         top_picks=top_picks)
        except Exception:
            has_kelly = False

    if not has_kelly:
        top_parlay = parlays[0] if parlays else None
        push_body  = _format_push_body(top_picks, top_parlay)
        push_title = (
            f"⚡ {len(top_picks)} edges | Best: {top_picks[0]['player']} "
            f"{top_picks[0]['direction']} {top_picks[0]['line']} ({top_picks[0]['conf_pct']}%)"
            if top_picks else "Power Parlay Scan"
        )

    send_push(push_body, title=push_title)
    _log("Push notification sent")

    embed = _format_discord_embed(top_picks, parlays,
                                  goblin_parlays=goblin_parlays or [],
                                  demon_parlays=demon_parlays or [])
    DISCORD_WEBHOOK_PREMIUM = os.getenv("DISCORD_WEBHOOK_PREMIUM", "")
    DISCORD_WEBHOOK_FREE    = os.getenv("DISCORD_WEBHOOK_FREE", "")

    if DISCORD_WEBHOOK_PREMIUM:
        try:
            resp = requests.post(DISCORD_WEBHOOK_PREMIUM, json=embed, timeout=10)
            resp.raise_for_status()
            _log("Discord premium sent")
        except Exception as e:
            _log(f"Discord premium failed: {e}")

    # Free channel: send immediately.
    # A 60-min daemon thread was previously used here but GH Actions kills daemon
    # threads when the main process exits, so the message was never actually sent.
    if DISCORD_WEBHOOK_FREE:
        try:
            requests.post(DISCORD_WEBHOOK_FREE, json=embed, timeout=10)
            _log("Discord free sent")
        except Exception as e:
            _log(f"Discord free failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Deduplication: don't re-send picks within DEDUP_HOURS
# ─────────────────────────────────────────────────────────────────────────────

def _dedup_picks(picks: list[dict]) -> list[dict]:
    """Filter out picks that were already sent in the last DEDUP_HOURS hours."""
    sent    = _load_sent()
    now_ts  = datetime.now(timezone.utc).timestamp()
    cutoff  = now_ts - (DEDUP_HOURS * 3600)
    new_sent = {k: v for k, v in sent.items() if v > cutoff}  # prune old

    fresh = []
    for p in picks:
        key = f"{p['player']}|{p['stat_type']}|{p['game_id']}"
        if new_sent.get(key, 0) < cutoff:
            fresh.append(p)
            new_sent[key] = now_ts

    _save_sent(new_sent)
    return fresh


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run(sports: list[str] = None, force: bool = False):
    """
    Full pipeline: fetch → score → parlay → notify.

    sports: list of sport keys to scan. None = all.
    force:  ignore dedup (re-send even if recently sent).
    """
    if sports is None:
        sports = ["NBA", "MLB", "WNBA", "TENNIS", "SOCCER"]

    _log(f"Starting power parlay scan: {', '.join(sports)}")

    # Pre-load context data
    _load_nba_def_ratings()

    # 1. Fetch standard, goblin, and demon lines separately
    all_lines    = fetch_standard_lines(sports)
    goblin_lines = fetch_typed_lines(sports, "goblin")
    demon_lines  = fetch_typed_lines(sports, "demon")
    _log(f"Total standard lines: {len(all_lines)} | goblin: {len(goblin_lines)} | demon: {len(demon_lines)}")

    if not all_lines:
        _log("No lines found — exiting")
        return

    # 2. Score every line
    # scored_all: every pick with enough game history, regardless of confidence.
    #             Logged to Supabase so we can validate whether model confidence
    #             actually predicts hit rate across the full spectrum (30%–90%).
    # scored:     only picks above MIN_CONF — used for parlay building / notifications.
    scored_all = []
    scored     = []
    for i, pick in enumerate(all_lines):
        stats = get_stats_for_pick(pick)
        if stats is None:
            continue
        if stats.get("n_games", 0) < MIN_GAMES:
            continue
        s = score_pick(stats, pick)
        # Tag whether this pick passed the betting threshold
        s["was_qualified"] = s["confidence"] >= MIN_CONF
        scored_all.append(s)
        if s["was_qualified"]:
            scored.append(s)
        # Progress log every 20 picks
        if (i + 1) % 20 == 0:
            _log(f"  Scored {i+1}/{len(all_lines)} lines, {len(scored)} qualified so far...")
        time.sleep(0.05)  # light rate-limit respect

    _log(f"All scored: {len(scored_all)} | Qualified (≥{int(MIN_CONF*100)}%): {len(scored)}")

    # Log ALL scored picks to Supabase NOW — before any early returns.
    # This ensures watched picks are always captured for calibration,
    # even on days with zero qualified picks or all-deduped runs.
    try:
        from calibration_tracker import log_pick as _log_pick_early
        today = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")
        _early_logged_q = 0
        _early_logged_w = 0
        for p in scored_all:
            try:
                _log_pick_early(p)
                if p.get("was_qualified"):
                    _early_logged_q += 1
                else:
                    _early_logged_w += 1
            except Exception:
                pass
        if _early_logged_q or _early_logged_w:
            _log(f"Logged {_early_logged_q} bet picks + {_early_logged_w} watched picks to Supabase.")
    except Exception:
        pass

    if not scored:
        _log("No qualified picks — nothing to send")
        return

    # 3. Sort by confidence
    scored.sort(key=lambda x: x["confidence"], reverse=True)

    # 4. Deduplicate (unless forced)
    if not force:
        scored = _dedup_picks(scored)
        if not scored:
            _log("All picks already sent recently — skipping notification")
            return

    # 5. Build parlays
    parlays = build_parlays(scored)
    _log(f"Parlays built: {len(parlays)}")

    # 6. Log top picks
    _log("=" * 60)
    _log("TOP PICKS:")
    for p in scored[:10]:
        rec = str(p.get("recent_values", [])[:5])
        _log(f"  {p['conf_pct']:3d}% | {p['sport']:6} | {p['player']:<24} "
             f"{p['direction']:<5} {p['line']:<6} {p['stat_type']:<20} "
             f"HR:{p['hit_rate']:.0%} Avg:{p['avg']} L5:{rec}")

    if parlays:
        _log("\nBEST PARLAYS:")
        for par in parlays[:3]:
            legs = " + ".join(f"{l['player']} {l['direction']} {l['line']}" for l in par["legs"])
            _log(f"  {par['n_legs']}-leg ({par['payout']}x) | p_win={int(par['p_win']*100)}% | EV=+{par['ev_pct']}% | {legs}")
    _log("=" * 60)

    # 5b. Build diversified parlay portfolio with Kelly sizing (standard)
    bankroll = float(os.getenv("BANKROLL", "") or "50")  # empty string or unset → default $50
    kelly_parlays = []
    try:
        from parlay_builder import build_diverse_parlays as _bdp, format_parlay_plan as _fpp
        kelly_parlays = _bdp(scored, bankroll=bankroll)
        if kelly_parlays:
            _log(f"\nKELLY PARLAY PORTFOLIO (${bankroll:.0f} bankroll):")
            for kp in kelly_parlays:
                legs_str = " + ".join(
                    f"{l['player'].split()[-1]} {l['direction']} {l['line']}"
                    for l in kp["leg_summary"]
                )
                _log(f"  ${kp['bet_size']:.2f}→${kp['win_amount']:.2f} | "
                     f"{kp['n_legs']}pk {kp['payout']:.0f}x | "
                     f"p={int(kp['p_win']*100)}% EV+{kp['ev_pct']}% | {legs_str}")
            plan_str = _fpp(kelly_parlays, bankroll)
            _log(f"\n{plan_str}")
    except Exception as _e:
        _log(f"Kelly parlay builder failed (using standard parlays): {_e}")
        kelly_parlays = []

    # 5c. Score goblin and demon lines, build separate parlays
    goblin_parlays = []
    demon_parlays  = []
    try:
        from parlay_builder import (build_goblin_parlays as _bgp,
                                    build_demon_parlays as _bdmp,
                                    EXCLUDED_STAT_TYPES as _EXCL)

        # Pre-filter: drop excluded stat types before scoring (most goblin/demon
        # lines are Fantasy Score / combo props that we'll never use — scoring them
        # all would add 20+ minutes to the run for zero benefit).
        # Cap at 150 per type to keep the run under ~3 minutes.
        _GOBLIN_GOOD = [
            p for p in goblin_lines
            if p.get("stat_type", "") not in _EXCL
            and "Pitches Thrown" not in p.get("stat_type", "")
        ][:150]
        _DEMON_GOOD = [
            p for p in demon_lines
            if p.get("stat_type", "") not in _EXCL
            and "Pitches Thrown" not in p.get("stat_type", "")
        ][:150]
        _log(f"Goblin lines to score (after pre-filter): {len(_GOBLIN_GOOD)}")
        _log(f"Demon  lines to score (after pre-filter): {len(_DEMON_GOOD)}")

        # Score goblin lines
        scored_goblin = []
        for pick in _GOBLIN_GOOD:
            stats = get_stats_for_pick(pick)
            if stats is None or stats.get("n_games", 0) < MIN_GAMES:
                continue
            s = score_pick(stats, pick)
            s["projection_kind"] = "goblin"
            scored_goblin.append(s)
            time.sleep(0.05)
        _log(f"Goblin lines scored: {len(scored_goblin)}")

        # Score demon lines
        scored_demon = []
        for pick in _DEMON_GOOD:
            stats = get_stats_for_pick(pick)
            if stats is None or stats.get("n_games", 0) < MIN_GAMES:
                continue
            s = score_pick(stats, pick)
            s["projection_kind"] = "demon"
            scored_demon.append(s)
            time.sleep(0.05)
        _log(f"Demon  lines scored: {len(scored_demon)}")

        goblin_parlays = _bgp(scored_goblin, bankroll=bankroll)
        demon_parlays  = _bdmp(scored_demon, bankroll=bankroll)

        if goblin_parlays:
            gp = goblin_parlays[0]
            legs_str = " + ".join(
                f"{l['player'].split()[-1]} {l['direction']} {l['line']}"
                for l in gp["leg_summary"]
            )
            _log(f"\nGOBLIN PARLAY: ${gp['bet_size']}→~${gp['win_amount']} | "
                 f"{gp['n_legs']}pk ~{gp['payout']}x | p={int(gp['p_win']*100)}% | {legs_str}")
        else:
            _log("No qualifying goblin parlay found today")

        if demon_parlays:
            dp = demon_parlays[0]
            legs_str = " + ".join(
                f"{l['player'].split()[-1]} {l['direction']} {l['line']}"
                for l in dp["leg_summary"]
            )
            _log(f"\nDEMON PARLAY: ${dp['bet_size']}→~${dp['win_amount']} | "
                 f"{dp['n_legs']}pk ~{dp['payout']}x | p={int(dp['p_win']*100)}% | {legs_str}")
        else:
            _log("No qualifying demon parlay found today")

    except Exception as _e:
        _log(f"Goblin/demon parlay builder failed: {_e}")

    # 7. Send notifications — use Kelly parlays if available, else standard parlays.
    # When no qualifying parlays exist, _send_notifications still fires with top picks
    # so you always get a notification even on low-confidence days.
    final_notification_parlays = kelly_parlays if kelly_parlays else parlays
    if not final_notification_parlays:
        _log("No qualifying parlays today — sending picks-only notification.")
    _send_notifications(scored[:8], final_notification_parlays, bankroll=bankroll,
                        goblin_parlays=goblin_parlays, demon_parlays=demon_parlays)

    # 8. Log parlays for P&L tracking
    # (Individual picks were already logged to Supabase above, before early returns)
    try:
        from calibration_tracker import log_parlay as _log_parlay
        today = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")
        final_parlays = kelly_parlays
        for i, kp in enumerate(final_parlays, 1):
            try:
                _log_parlay(kp, parlay_num=i, parlay_date=today)
            except Exception:
                pass
        if final_parlays:
            _log(f"Logged {len(final_parlays)} parlays to Supabase for P&L tracking.")
    except Exception:
        pass

    _log("Done.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--sports",  nargs="+", default=None, help="Sports to scan")
    parser.add_argument("--force",   action="store_true",     help="Skip dedup")
    parser.add_argument("--dry-run", action="store_true",     help="Don't send notifications")
    args = parser.parse_args()

    if args.dry_run:
        # Override notification functions
        import notify as _n
        _n.send_push = lambda *a, **kw: print("[DRY RUN] Push:", a)
    run(sports=args.sports, force=args.force)
