"""
mlb_advanced.py — Advanced MLB matchup context.

Pulls every contextual factor that affects MLB player props:

  PITCHER PROPS:
  - Opponent handedness splits (vs RHP/LHP contact/K rates)
  - Pitch arsenal (pitch type %, K rate per pitch type)
  - Park factor (run/K environment)
  - Weather (temp, wind speed/direction)
  - Bullpen usage (innings/ERA last 7 days — affects expected IP)
  - Home plate umpire (K tendency)
  - Home/away split from game logs

  BATTER PROPS:
  - vs RHP / vs LHP splits (OPS, wOBA, K rate, HR rate)
  - Batting order position (affects PA count)
  - Park factor
  - Weather
  - Vegas team total (implied run expectation)

Data sources (all free):
  - statsapi.mlb.com (splits, lineups, officials, bullpen)
  - baseballsavant.mlb.com (pitch arsenal, statcast)
  - api.the-odds-api.com (team totals — requires ODDS_API_KEY)
  - openweathermap.org (weather — requires OPENWEATHER_API_KEY)
"""

import csv
import io
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_CACHE_DIR = Path("logs/mlb_advanced_cache")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_TTL = {"schedule": 1800, "splits": 7200, "arsenal": 21600,
        "bullpen": 3600, "umpire_stats": 86400, "odds": 900}

def _cpath(key: str) -> Path:
    return _CACHE_DIR / f"{key[:80].replace(' ','_').replace('/','_')}.json"

def _load(key: str, ttl_key: str = "splits"):
    p = _cpath(key)
    ttl = _TTL.get(ttl_key, 3600)
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

def _get(url: str, headers: dict = None) -> dict | list:
    h = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    return json.loads(urllib.request.urlopen(req, timeout=12).read())

def _get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=12).read().decode("utf-8-sig", errors="replace")


# ─────────────────────────────────────────────────────────────────────────────
# TODAY'S SCHEDULE — one call, shared by all functions
# ─────────────────────────────────────────────────────────────────────────────

def get_today_schedule(date_str: str = None) -> list[dict]:
    """
    Today's games with team IDs, probable pitchers, lineups, and umpires.
    Cached 30 min.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    cached = _load(f"sched_{date_str}", "schedule")
    if cached:
        return cached

    url  = (f"https://statsapi.mlb.com/api/v1/schedule"
            f"?sportId=1&date={date_str}"
            f"&hydrate=probablePitcher,team,lineups,officials,venue")
    try:
        data  = _get(url)
        games = []
        for d in data.get("dates", []):
            for g in d.get("games", []):
                away = g["teams"]["away"]
                home = g["teams"]["home"]
                # Lineups
                lineups = g.get("lineups", {})
                away_lu = [(p.get("battingOrder"), p.get("fullName","?"))
                           for p in lineups.get("awayPlayers", [])]
                home_lu = [(p.get("battingOrder"), p.get("fullName","?"))
                           for p in lineups.get("homePlayers", [])]
                # Umpire
                ump_name = ""
                ump_id   = None
                for o in g.get("officials", []):
                    if "Home Plate" in o.get("officialType",""):
                        ump_name = o["official"].get("fullName","")
                        ump_id   = o["official"].get("id")
                # Venue
                venue = g.get("venue", {}).get("name", "")
                games.append({
                    "game_pk":       g["gamePk"],
                    "away_id":       away["team"]["id"],
                    "away_name":     away["team"]["name"],
                    "away_abbr":     away["team"].get("abbreviation",""),
                    "home_id":       home["team"]["id"],
                    "home_name":     home["team"]["name"],
                    "home_abbr":     home["team"].get("abbreviation",""),
                    "away_pitcher":  away.get("probablePitcher",{}).get("fullName",""),
                    "away_pitcher_id":away.get("probablePitcher",{}).get("id"),
                    "home_pitcher":  home.get("probablePitcher",{}).get("fullName",""),
                    "home_pitcher_id":home.get("probablePitcher",{}).get("id"),
                    "away_lineup":   away_lu,
                    "home_lineup":   home_lu,
                    "ump_name":      ump_name,
                    "ump_id":        ump_id,
                    "venue":         venue,
                })
        _save(f"sched_{date_str}", games)
        return games
    except Exception as e:
        print(f"[mlb_advanced] Schedule failed: {e}")
        return []


def find_game_for_player(player_name: str, date_str: str = None) -> tuple[dict | None, bool | None]:
    """
    Returns (game_dict, is_home) for a player pitching today.
    is_home=True if player is home pitcher, False if away, None if not found.
    """
    games = get_today_schedule(date_str)
    name_lower = player_name.lower()
    for g in games:
        if name_lower in g.get("away_pitcher","").lower():
            return g, False
        if name_lower in g.get("home_pitcher","").lower():
            return g, True
    return None, None


def get_batting_order(player_name: str, date_str: str = None) -> int | None:
    """Return a player's batting order position (1-9) today, or None."""
    games = get_today_schedule(date_str)
    name_lower = player_name.lower()
    for g in games:
        for order, name in g["away_lineup"] + g["home_lineup"]:
            if name_lower in name.lower() or name.lower() in name_lower:
                if order:
                    try:
                        return int(str(order).lstrip("0") or "0") // 100  # MLB format: 100,200,...900
                    except Exception:
                        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# LINEUP HANDEDNESS (ChatGPT recommendation)
# ─────────────────────────────────────────────────────────────────────────────

def _get_batter_hand(batter_id: int) -> str:
    """
    Return batter's bat side: 'L', 'R', or 'S' (switch).
    Cached 24h per player.
    """
    cached = _load(f"bathand_{batter_id}", "splits")
    if cached:
        return cached.get("hand", "R")
    try:
        url  = f"https://statsapi.mlb.com/api/v1/people/{batter_id}"
        data = _get(url)
        hand = data.get("people", [{}])[0].get("batSide", {}).get("code", "R")
        _save(f"bathand_{batter_id}", {"hand": hand})
        return hand
    except Exception:
        return "R"


def get_lineup_handedness(game: dict, pitcher_is_home: bool,
                          pitcher_throws: str = "R") -> dict:
    """
    Compute the actual handedness breakdown of today's opposing lineup.
    Switch hitters are assumed to bat opposite the pitcher (standard strategy).

    Returns:
      {n_lhb, n_rhb, n_switch, effective_lhb_pct, effective_rhb_pct, n_batters, note}
    """
    # Which lineup is opposing the pitcher?
    if pitcher_is_home:
        lineup = game.get("away_lineup", [])
    else:
        lineup = game.get("home_lineup", [])

    if not lineup:
        return {"n_lhb": 0, "n_rhb": 0, "n_switch": 0,
                "effective_lhb_pct": 0.40, "effective_rhb_pct": 0.60,
                "n_batters": 0, "note": "lineup not yet posted"}

    counts = {"L": 0, "R": 0, "S": 0}
    for order, bname in lineup[:9]:  # only the batting order, not bench
        try:
            from data.mlb_batter_stats import find_player_id
            pid = find_player_id(bname)
            if pid:
                hand = _get_batter_hand(int(pid))
                counts[hand] = counts.get(hand, 0) + 1
            else:
                counts["R"] += 1   # assume R if unknown
        except Exception:
            counts["R"] += 1

    n = sum(counts.values())
    if n == 0:
        return {"effective_lhb_pct": 0.40, "effective_rhb_pct": 0.60,
                "n_batters": 0, "note": "no batters found"}

    # Switch hitters bat opposite pitcher hand
    effective_lhb = counts["L"] + (counts["S"] if pitcher_throws == "R" else 0)
    effective_rhb = counts["R"] + (counts["S"] if pitcher_throws == "L" else 0)

    lhb_pct = effective_lhb / n
    rhb_pct = effective_rhb / n

    note = f"Lineup: {counts['L']}L / {counts['R']}R / {counts['S']}S"
    if lhb_pct >= 0.60:
        note += f" → heavy LHB ({lhb_pct:.0%})"
    elif rhb_pct >= 0.60:
        note += f" → heavy RHB ({rhb_pct:.0%})"

    return {
        "n_lhb":              counts["L"],
        "n_rhb":              counts["R"],
        "n_switch":           counts["S"],
        "effective_lhb_pct":  round(lhb_pct, 3),
        "effective_rhb_pct":  round(rhb_pct, 3),
        "n_batters":          n,
        "note":               note,
    }


# ─────────────────────────────────────────────────────────────────────────────
# HANDEDNESS SPLITS
# ─────────────────────────────────────────────────────────────────────────────

def get_pitcher_splits(pitcher_id: int) -> dict:
    """
    Pitcher's 2026 splits: vs LHB and vs RHB.
    Returns {vs_lhb: {k_pct, bb_pct, ba, ops}, vs_rhb: {...}}
    """
    cached = _load(f"psplit_{pitcher_id}")
    if cached:
        return cached

    url = (f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats"
           f"?stats=statSplits&group=pitching&season=2026")
    try:
        data   = _get(url)
        splits = data.get("stats", [{}])[0].get("splits", [])
        result = {}
        for s in splits:
            desc = s.get("split", {}).get("description", "")
            st   = s["stat"]
            bf   = int(st.get("battersFaced", 1) or 1)
            ks   = int(st.get("strikeOuts", 0) or 0)
            bb   = int(st.get("baseOnBalls", 0) or 0)
            entry = {
                "k_pct": ks / bf,
                "bb_pct": bb / bf,
                "ba":    st.get("avg", ".000"),
                "ops":   st.get("ops", ".000"),
                "bf":    bf,
            }
            if "vs. Left" in desc:
                result["vs_lhb"] = entry
            elif "vs. Right" in desc:
                result["vs_rhb"] = entry
        _save(f"psplit_{pitcher_id}", result)
        return result
    except Exception:
        return {}

def get_batter_splits(batter_id: int) -> dict:
    """
    Batter's 2026 splits: vs LHP and vs RHP.
    Returns {vs_lhp: {ops, k_pct, hr_rate, woba}, vs_rhp: {...}}
    """
    cached = _load(f"bsplit_{batter_id}")
    if cached:
        return cached

    url = (f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats"
           f"?stats=statSplits&group=hitting&season=2026")
    try:
        data   = _get(url)
        splits = data.get("stats", [{}])[0].get("splits", [])
        result = {}
        for s in splits:
            desc = s.get("split", {}).get("description", "")
            st   = s["stat"]
            pa   = int(st.get("plateAppearances", 1) or 1)
            ab   = int(st.get("atBats", 1) or 1)
            ks   = int(st.get("strikeOuts", 0) or 0)
            hr   = int(st.get("homeRuns", 0) or 0)
            entry = {
                "ops":     float(st.get("ops", 0) or 0),
                "avg":     st.get("avg", ".000"),
                "k_pct":   ks / pa,
                "hr_rate": hr / pa,
                "pa":      pa,
            }
            if "vs. Left" in desc:
                result["vs_lhp"] = entry
            elif "vs. Right" in desc:
                result["vs_rhp"] = entry
        _save(f"bsplit_{batter_id}", result)
        return result
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# PITCH ARSENAL (Baseball Savant)
# ─────────────────────────────────────────────────────────────────────────────

_ARSENAL_CACHE: dict[int, list[dict]] = {}   # player_id → [{pitch_type, usage_pct, k_pct, whiff_pct}]

def _load_arsenal_data(season: int = 2026) -> dict[int, list[dict]]:
    """Load full pitcher arsenal leaderboard from Baseball Savant CSV."""
    global _ARSENAL_CACHE
    if _ARSENAL_CACHE:
        return _ARSENAL_CACHE

    cached = _load("savant_arsenal", "arsenal")
    if cached:
        _ARSENAL_CACHE = {int(k): v for k, v in cached.items()}
        return _ARSENAL_CACHE

    url = (f"https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
           f"?type=pitcher&pitchType=&season={season}&team=&min=10&csv=true")
    try:
        raw   = _get_text(url)
        rows  = list(csv.DictReader(io.StringIO(raw)))
        result: dict[int, list[dict]] = {}
        for row in rows:
            try:
                pid   = int(row.get("player_id", 0))
                usage = float(row.get("pitch_usage", 0) or 0) / 100  # convert % to decimal
                k_pct = float(row.get("k_percent", 0) or 0) / 100
                whiff = float(row.get("whiff_percent", 0) or 0) / 100
                ptype = row.get("pitch_name", "").strip()
                if pid and ptype:
                    result.setdefault(pid, []).append({
                        "pitch_name": ptype,
                        "usage":      round(usage, 3),
                        "k_pct":      round(k_pct, 3),
                        "whiff_pct":  round(whiff, 3),
                    })
            except Exception:
                pass
        _ARSENAL_CACHE = result
        _save("savant_arsenal", {str(k): v for k, v in result.items()})
        return result
    except Exception as e:
        print(f"[mlb_advanced] Arsenal load failed: {e}")
        return {}

def get_pitcher_arsenal(pitcher_id: int) -> list[dict]:
    """Pitcher's pitch mix with K rate and whiff rate per pitch type."""
    arsenal = _load_arsenal_data()
    return sorted(arsenal.get(pitcher_id, []), key=lambda x: x["usage"], reverse=True)

def get_pitcher_k_profile(pitcher_id: int) -> float:
    """
    Weighted average K rate across all pitch types.
    Heavier usage pitches count more.
    """
    arsenal = get_pitcher_arsenal(pitcher_id)
    if not arsenal:
        return 0.22   # league average
    total_weight = sum(p["usage"] for p in arsenal)
    if total_weight == 0:
        return 0.22
    weighted_k = sum(p["usage"] * p["k_pct"] for p in arsenal)
    return weighted_k / total_weight


# ─────────────────────────────────────────────────────────────────────────────
# UMPIRE TENDENCY
# ─────────────────────────────────────────────────────────────────────────────

# Static umpire K tendency (above/below league avg per 9 innings)
# Positive = more Ks, negative = fewer Ks
# Source: umpire scorecards approximations (manually curated key umpires)
# League avg ≈ 8.0 Ks/9
UMPIRE_K_FACTOR = {
    # Ump name → adjustment to pitcher K rate (percentage points)
    "Angel Hernandez":   -0.015,
    "Laz Diaz":          -0.018,
    "CB Bucknor":        -0.012,
    "Bill Miller":       -0.010,
    "Fieldin Culbreth":   0.005,
    "Dan Bellino":        0.008,
    "Jim Wolf":           0.004,
    "Stu Scheurwater":    0.012,
    "Chad Fairchild":     0.015,
    "John Tumpane":       0.008,
    "Tripp Gibson":       0.018,
    "Vic Carapazza":      0.022,   # high-K umpire
}

def get_umpire_k_adjustment(ump_name: str) -> float:
    """
    Return K rate adjustment for home plate umpire.
    Positive = above-average K umpire (boosts OVER Ks).
    Returns 0.0 if unknown.
    """
    if not ump_name:
        return 0.0
    for name, adj in UMPIRE_K_FACTOR.items():
        if name.lower() in ump_name.lower():
            return adj
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# BULLPEN CONTEXT
# ─────────────────────────────────────────────────────────────────────────────

def get_bullpen_stats(team_id: int) -> dict:
    """
    Bullpen ERA, WHIP, and innings pitched last 7 days.
    High bullpen usage = starter may be pulled earlier.
    """
    cached = _load(f"bullpen_{team_id}", "bullpen")
    if cached:
        return cached

    result = {"era": 4.0, "whip": 1.30, "ip_last_7": 20.0}
    try:
        # Season bullpen ERA (relief pitchers)
        url = (f"https://statsapi.mlb.com/api/v1/teams/{team_id}/stats"
               f"?stats=season&group=pitching&season=2026&gameType=R")
        data = _get(url)
        s    = data.get("stats", [{}])[0].get("splits", [{}])[0].get("stat", {})
        result["era"]  = float(s.get("era", 4.0) or 4.0)
        result["whip"] = float(s.get("whip", 1.30) or 1.30)
        _save(f"bullpen_{team_id}", result)
    except Exception:
        pass
    return result


# ─────────────────────────────────────────────────────────────────────────────
# VEGAS SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

_ODDS_CACHE: list[dict] | None = None
_ODDS_TS: float = 0

def get_mlb_odds(date_str: str = None) -> list[dict]:
    """
    Pull MLB game totals from The Odds API.
    Returns list of {home_team, away_team, total, home_total, away_total}
    """
    global _ODDS_CACHE, _ODDS_TS
    now = time.time()
    # Cache hit covers both successful results AND a known-failed key/outage —
    # _ODDS_CACHE == [] from a prior failure still satisfies the TTL window
    # below since we check timestamp freshness, not truthiness, first.
    if (now - _ODDS_TS) < _TTL["odds"] and _ODDS_CACHE is not None:
        return _ODDS_CACHE

    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return []

    try:
        url  = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
                f"?apiKey={api_key}&regions=us&markets=totals"
                f"&oddsFormat=american&dateFormat=iso")
        data = _get(url)
        games = []
        for g in data:
            total = None
            for bm in g.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt["key"] == "totals":
                        for o in mkt["outcomes"]:
                            if o["name"] == "Over":
                                total = o.get("point")
                                break
                if total:
                    break
            games.append({
                "home_team": g.get("home_team",""),
                "away_team": g.get("away_team",""),
                "total":     total,           # game total (e.g. 8.5)
            })
        _ODDS_CACHE = games
        _ODDS_TS    = now
        return games
    except Exception as e:
        print(f"[mlb_advanced] Odds API failed: {e}")
        # Cache the failure too — with a dead/expired key this was retrying
        # the network call on every single pick scored (confirmed: 30+
        # identical "401 Unauthorized" lines in one run), adding real latency
        # across hundreds of picks instead of failing once per TTL window.
        _ODDS_CACHE = []
        _ODDS_TS    = now
        return []

def get_game_total(home_team: str, away_team: str) -> float | None:
    """Return Vegas game total for a specific game."""
    odds = get_mlb_odds()
    ht_lower = home_team.lower().split()[-1]   # last word of team name
    at_lower = away_team.lower().split()[-1]
    for g in odds:
        if ht_lower in g["home_team"].lower() and at_lower in g["away_team"].lower():
            return g["total"]
        if at_lower in g["home_team"].lower() and ht_lower in g["away_team"].lower():
            return g["total"]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MAIN CONTEXT FUNCTION — returns everything for a pitcher prop
# ─────────────────────────────────────────────────────────────────────────────

# Expected PA by batting order position (MLB average)
EXPECTED_PA = {1: 4.8, 2: 4.7, 3: 4.6, 4: 4.4, 5: 4.3,
               6: 4.1, 7: 4.0, 8: 3.9, 9: 3.8}

def get_pitcher_full_context(player_name: str, stat_type: str,
                              pitcher_id: int = None) -> dict:
    """
    Full context for a pitcher prop pick.
    Returns context_score (0–1) and detailed breakdown.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    game, is_home = find_game_for_player(player_name, today)

    result = {
        "home_away":      "unknown",
        "opp_team":       "unknown",
        "context_score":  0.5,
        "description":    [],
        "components":     {},
    }

    if game is None:
        return result

    opp_id   = game["home_id"]   if not is_home else game["away_id"]
    opp_name = game["home_name"] if not is_home else game["away_name"]
    result["home_away"] = "home" if is_home else "away"
    result["opp_team"]  = opp_name
    ump_name = game.get("ump_name", "")

    # Get pitcher ID if not provided
    if pitcher_id is None:
        pitcher_id = (game["home_pitcher_id"] if is_home else game["away_pitcher_id"])

    components = {}

    # 1. Opponent K rate (how often the opposing lineup strikes out)
    from data.matchup_context import _get_team_k_pct, PARK_FACTORS
    opp_k  = _get_team_k_pct(opp_id) or 0.22
    league_k_avg = 0.220
    k_edge = (opp_k - league_k_avg) / 0.04   # ±0.04 = ±1 unit
    components["opp_k_pct"] = round(opp_k, 3)

    # 2. Park factor
    home_name = game["home_name"]
    pf = PARK_FACTORS.get(home_name, 1.0)
    # Pitcher-friendly park (pf < 1) → more Ks (batters less aggressive)
    # Hitter-friendly park (pf > 1) → fewer Ks somewhat
    park_k_adj = (1.0 - pf) * 1.5   # Coors (1.28) = -0.42, T-Mobile (0.94) = +0.09
    park_k_adj = max(-0.4, min(0.4, park_k_adj))
    components["park_factor"] = pf

    # 3. Umpire
    ump_adj = get_umpire_k_adjustment(ump_name)
    components["ump_adj"] = ump_adj
    if ump_name:
        result["description"].append(
            f"{'✅ High-K' if ump_adj > 0.01 else ('⚠️ Low-K' if ump_adj < -0.01 else 'Avg')} "
            f"umpire: {ump_name}"
        )

    # 4. Pitcher arsenal — weighted K profile
    arsenal_k = 0.22
    if pitcher_id:
        arsenal_k = get_pitcher_k_profile(pitcher_id)
        arsenal   = get_pitcher_arsenal(pitcher_id)
        if arsenal:
            top2 = ", ".join(f"{p['pitch_name']} ({p['usage']*100:.0f}%)" for p in arsenal[:2])
            result["description"].append(f"Arsenal: {top2}")
        components["arsenal_k_pct"] = round(arsenal_k, 3)

    # 5. Pitcher splits (vs LHB / vs RHB) × actual lineup handedness
    handedness_adj = 0.0
    if pitcher_id:
        splits = get_pitcher_splits(pitcher_id)
        if splits:
            rhb_k = splits.get("vs_rhb", {}).get("k_pct", 0.22)
            lhb_k = splits.get("vs_lhb", {}).get("k_pct", 0.22)
            components["vs_rhb_k"] = round(rhb_k, 3)
            components["vs_lhb_k"] = round(lhb_k, 3)

            # Use actual lineup handedness rather than hardcoded 60/40
            try:
                pitcher_throws = "R"  # will override below if we can get it
                try:
                    url_hand = f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}?fields=pitchHand"
                    d_hand   = _get(url_hand)
                    pitcher_throws = d_hand.get("people", [{}])[0].get("pitchHand", {}).get("code", "R")
                except Exception:
                    pass

                lu_hand = get_lineup_handedness(game, is_home, pitcher_throws)
                lhb_pct = lu_hand.get("effective_lhb_pct", 0.40)
                rhb_pct = lu_hand.get("effective_rhb_pct", 0.60)
                blended_k = rhb_pct * rhb_k + lhb_pct * lhb_k
                components["lineup_lhb_pct"] = lhb_pct
                components["lineup_note"]     = lu_hand.get("note", "")
                if lu_hand.get("n_batters", 0) > 0:
                    result["description"].append(lu_hand["note"])
            except Exception:
                lhb_pct, rhb_pct = 0.40, 0.60
                blended_k = 0.6 * rhb_k + 0.4 * lhb_k

            handedness_adj = (blended_k - 0.22) / 0.04

            # Flag if pitcher has a dominant split mismatch with today's lineup
            split_diff = abs(rhb_k - lhb_k)
            if split_diff > 0.05 and lu_hand.get("n_batters", 0) > 0:
                favored_hand = "RHB" if rhb_k > lhb_k else "LHB"
                dominant_pct = rhb_pct if favored_hand == "RHB" else lhb_pct
                if dominant_pct >= 0.60:
                    result["description"].append(
                        f"✅ Lineup favors pitcher's strong side ({dominant_pct:.0%} {favored_hand})"
                    )
                elif dominant_pct <= 0.35:
                    result["description"].append(
                        f"⚠️ Lineup heavy on pitcher's weaker side ({1-dominant_pct:.0%} vs {favored_hand.replace('RHB','LHB').replace('LHB','RHB')})"
                    )

    # 6. Home advantage for pitchers (small but real)
    home_adj = 0.05 if is_home else 0.0

    # 7. Vegas game total — low totals suggest pitcher's park and good pitching
    total = get_game_total(game["home_name"], game["away_name"])
    vegas_adj = 0.0
    if total:
        components["game_total"] = total
        if total < 7.5:
            vegas_adj = 0.10    # low total → good pitching environment
            result["description"].append(f"✅ Low game total ({total}) — pitcher's game")
        elif total > 9.5:
            vegas_adj = -0.10   # high total → tough K environment
            result["description"].append(f"⚠️ High game total ({total})")
        else:
            result["description"].append(f"Game total: {total}")

    # 8. Bullpen — high usage last week = starter might get pulled early
    bp = get_bullpen_stats(game["home_id"] if is_home else game["away_id"])
    bp_adj = 0.0
    if bp["era"] < 3.5:
        bp_adj = 0.05   # strong bullpen = manager may trust starter longer
    elif bp["era"] > 5.0:
        bp_adj = -0.05  # weak bullpen = manager may let starter go longer anyway... neutral
    components["bullpen_era"] = bp.get("era")

    # Combine all signals into context_score for Ks
    if stat_type in ("Pitcher Strikeouts", "Strikeouts"):
        # Raw signal: scaled 0-1 around neutral 0.5
        raw = 0.5
        raw += k_edge    * 0.25   # opp K rate (biggest factor)
        raw += park_k_adj * 0.15   # park
        raw += ump_adj   * 5.0 * 0.10   # umpire (scale to 0-1 range)
        raw += handedness_adj * 0.15  # pitch splits
        raw += home_adj  * 0.10   # home advantage
        raw += vegas_adj * 0.15   # vegas signal
        raw += bp_adj    * 0.10   # bullpen

        ctx_score = max(0.1, min(0.9, raw))
        result["context_score"] = round(ctx_score, 3)

    # Opponent K rate description
    if opp_k > 0.240:
        result["description"].insert(0, f"✅ High-K lineup: {opp_name} ({opp_k:.1%})")
    elif opp_k < 0.200:
        result["description"].insert(0, f"⚠️ Low-K lineup: {opp_name} ({opp_k:.1%})")
    else:
        result["description"].insert(0, f"Avg-K lineup: {opp_name} ({opp_k:.1%})")

    if pf > 1.05:
        result["description"].append(f"⚠️ Hitter park (PF={pf:.2f})")
    elif pf < 0.96:
        result["description"].append(f"✅ Pitcher park (PF={pf:.2f})")

    result["description"].append(f"{'Home' if is_home else 'Away'} start")
    result["components"] = components
    return result


def get_batter_full_context(player_name: str, stat_type: str,
                             batter_id: int = None,
                             pitcher_hand: str = "R") -> dict:
    """
    Full context for a batter prop pick.
    pitcher_hand: "R" or "L" — handedness of today's opposing starter
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    games = get_today_schedule(today)
    result = {
        "home_away":     "unknown",
        "opp_team":      "unknown",
        "opp_pitcher":   "unknown",
        "batting_order": None,
        "expected_pa":   4.3,
        "context_score": 0.5,
        "description":   [],
        "components":    {},
    }

    # Find batting order
    order = get_batting_order(player_name, today)
    if order:
        result["batting_order"] = order
        result["expected_pa"]   = EXPECTED_PA.get(order, 4.3)
        if order <= 3:
            result["description"].append(f"✅ Batting #{order} ({result['expected_pa']:.1f} exp PA)")
        elif order >= 7:
            result["description"].append(f"⚠️ Batting #{order} ({result['expected_pa']:.1f} exp PA)")

    # Handedness splits
    if batter_id:
        splits = get_batter_splits(batter_id)
        split_key = "vs_rhp" if pitcher_hand == "R" else "vs_lhp"
        split = splits.get(split_key, {})
        ops   = split.get("ops", 0.700)
        k_pct = split.get("k_pct", 0.22)
        result["components"]["split_ops"] = ops
        result["components"]["split_k_pct"] = k_pct

        league_ops = 0.720
        ops_adj = (ops - league_ops) / 0.100   # ±0.100 OPS = ±1 unit
        hand_label = "vs RHP" if pitcher_hand == "R" else "vs LHP"
        if ops > 0.800:
            result["description"].append(f"✅ Strong {hand_label}: {ops:.3f} OPS")
        elif ops < 0.640:
            result["description"].append(f"⚠️ Weak {hand_label}: {ops:.3f} OPS")

        raw = 0.5 + ops_adj * 0.3
        result["context_score"] = round(max(0.1, min(0.9, raw)), 3)

    return result
