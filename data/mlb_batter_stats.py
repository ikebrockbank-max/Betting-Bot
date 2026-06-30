"""
mlb_batter_stats.py — MLB batter and pitcher game log stats for all PrizePicks stat types.

Supports ALL current PP MLB stat types:

BATTER:
  Hitter Fantasy Score   — DK-style: 1B×3 + 2B×5 + 3B×8 + HR×10 + RBI×3.5 + R×3.2 + BB×3 + SB×6
  Hits+Runs+RBIs         — H + R + RBI per game
  Singles                — H - 2B - 3B - HR
  Total Bases            — already in API response (totalBases)
  Hits                   — H
  Runs                   — R
  RBI                    — RBI
  Hitter Strikeouts      — SO (as batter)
  Walks                  — BB

PITCHER (supplementing existing module):
  Pitcher Fantasy Score  — Outs×1 + K×3 - ER×3 + QS×4 (outs≥18 & ER≤3) + W×6
  Pitching Outs          — IP × 3
  Earned Runs Allowed    — ER
  Hits Allowed           — H allowed
  Walks Allowed          — BB allowed
  Pitches Thrown         — numberOfPitches (from game log)
  Pitcher Strikeouts     — K (already handled in scanner_power_parlay.py)

IMPORTANT NOTE on batter stats:
  Batter distributions are heavily skewed — lots of 0s, occasional big games.
  The hit rate for a specific line is the primary signal, not the average.
  Example: avg FS=7 with PP line=6.5, but only 35% of games clear 6.5.
"""

import json
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_CACHE_DIR = Path("logs/batter_cache")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _cpath(key: str) -> Path:
    return _CACHE_DIR / f"{key[:80].replace(' ','_').replace('/','_').replace(':','_')}.json"

def _load(key: str, ttl: int = 3600):
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

def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=12).read())

def _normalize(name: str) -> str:
    """Strip accents: Sánchez → Sanchez."""
    return "".join(
        c for c in unicodedata.normalize("NFD", name)
        if unicodedata.category(c) != "Mn"
    )


# ── PP Fantasy Score formulas ──────────────────────────────────────────────────

def compute_hitter_fs(s: dict) -> float:
    """
    DraftKings-style MLB Hitter Fantasy Score.
    Confirmed against PP lines (Trea Turner avg 7.0 vs line 6.5).
    """
    h   = int(s.get("hits", 0) or 0)
    d   = int(s.get("doubles", 0) or 0)
    t   = int(s.get("triples", 0) or 0)
    hr  = int(s.get("homeRuns", 0) or 0)
    r   = int(s.get("runs", 0) or 0)
    rbi = int(s.get("rbi", 0) or 0)
    bb  = int(s.get("baseOnBalls", 0) or 0)
    sb  = int(s.get("stolenBases", 0) or 0)
    sg  = max(0, h - d - t - hr)   # singles
    return sg*3 + d*5 + t*8 + hr*10 + rbi*3.5 + r*3.2 + bb*3 + sb*6

def compute_pitcher_fs(s: dict) -> float:
    """
    PrizePicks MLB Pitcher Fantasy Score — official formula, verified against
    Kyle Freeland's 2026-06-19 start (22 outs, 8K, 2ER, no decision):
      22×1 + 8×3 - 2×3 + 4 (quality start: outs>=18 and ER<=3) = 44, exact match.
    Three earlier versions of this formula existed across the codebase
    (this file, calibration_tracker.py, a stale docstring elsewhere) — all
    different, all wrong (wrong weights, missing the quality-start bonus,
    wrongly penalizing hits/walks/HBP which PrizePicks doesn't penalize at
    all). This is the only one now; calibration_tracker.py's resolver was
    fixed to match.

    Outs×1 + K×3 - ER×3 + QualityStart×4 + Win×6. No H/BB/HBP penalty.
    """
    outs = int(s.get("outs", 0) or 0)
    ks   = int(s.get("strikeOuts", 0) or 0)
    er   = int(s.get("earnedRuns", 0) or 0)
    win  = int(s.get("wins", 0) or 0) > 0
    qs   = outs >= 18 and er <= 3
    return outs * 1.0 + ks * 3.0 - er * 3.0 + (4.0 if qs else 0.0) + (6.0 if win else 0.0)


# ── Stat type → group + compute function ──────────────────────────────────────

PITCHER_STAT_TYPES = {
    "Pitcher Strikeouts", "Strikeouts", "Pitcher Fantasy Score",
    "Pitching Outs", "Earned Runs Allowed", "Hits Allowed",
    "Walks Allowed", "Pitches Thrown",
}

BATTER_STAT_TYPES = {
    "Hitter Fantasy Score", "Hits+Runs+RBIs", "Singles", "Total Bases",
    "Hits", "Runs", "RBI", "Hitter Strikeouts", "Walks",
    "Home Runs", "Stolen Bases",
}

def _stat_value(stat_type: str, s: dict) -> float | None:
    """Extract the relevant stat from a single game's stat dict."""
    st = stat_type
    if st in ("Pitcher Strikeouts", "Strikeouts"):
        return int(s.get("strikeOuts", 0) or 0)
    if st == "Pitcher Fantasy Score":
        return compute_pitcher_fs(s)
    if st == "Pitching Outs":
        ip = float(s.get("inningsPitched", 0) or 0)
        return int(ip * 3)   # convert IP to outs
    if st == "Earned Runs Allowed":
        return int(s.get("earnedRuns", 0) or 0)
    if st == "Hits Allowed":
        return int(s.get("hits", 0) or 0)
    if st == "Walks Allowed":
        return int(s.get("baseOnBalls", 0) or 0)
    if st == "Pitches Thrown":
        return int(s.get("numberOfPitches", 0) or 0)
    if st == "Hitter Fantasy Score":
        return compute_hitter_fs(s)
    if st == "Hits+Runs+RBIs":
        return (int(s.get("hits",0) or 0) + int(s.get("runs",0) or 0)
                + int(s.get("rbi",0) or 0))
    if st == "Singles":
        h  = int(s.get("hits", 0) or 0)
        d  = int(s.get("doubles", 0) or 0)
        t  = int(s.get("triples", 0) or 0)
        hr = int(s.get("homeRuns", 0) or 0)
        return max(0, h - d - t - hr)
    if st == "Total Bases":
        return int(s.get("totalBases", 0) or 0)
    if st == "Hits":
        return int(s.get("hits", 0) or 0)
    if st == "Runs":
        return int(s.get("runs", 0) or 0)
    if st == "RBI":
        return int(s.get("rbi", 0) or 0)
    if st == "Hitter Strikeouts":
        return int(s.get("strikeOuts", 0) or 0)
    if st == "Walks":
        return int(s.get("baseOnBalls", 0) or 0)
    if st == "Home Runs":
        return int(s.get("homeRuns", 0) or 0)
    if st == "Stolen Bases":
        return int(s.get("stolenBases", 0) or 0)
    return None


# ── Player ID lookup ───────────────────────────────────────────────────────────

def find_player_id(player_name: str) -> str | None:
    """Find MLB player ID — handles accents, fuzzy last-name fallback."""
    cached = _load(f"mlb_pid_{_normalize(player_name)}", ttl=86400)
    if cached:
        return cached.get("id")

    variants = [player_name, _normalize(player_name), player_name.split()[-1]]
    for variant in variants:
        try:
            enc = variant.replace(" ", "+")
            url = f"https://statsapi.mlb.com/api/v1/people/search?names={enc}&sportId=1"
            data = _get(url)
            for p in data.get("people", []):
                full = p.get("fullName", "")
                if (_normalize(player_name).lower() in _normalize(full).lower() or
                        _normalize(full).lower() in _normalize(player_name).lower()):
                    pid = str(p["id"])
                    _save(f"mlb_pid_{_normalize(player_name)}", {"id": pid, "name": full})
                    return pid
        except Exception:
            pass
    return None


# ── Game log pull ──────────────────────────────────────────────────────────────

def get_player_game_log(player_name: str, stat_type: str) -> list[dict]:
    """
    Pull 2026 game log for a player.
    Routes to pitching or hitting group based on stat_type.
    Returns list of per-game raw stat dicts, most recent first.
    """
    pid = find_player_id(player_name)
    if not pid:
        return []

    is_pitcher = stat_type in PITCHER_STAT_TYPES
    group      = "pitching" if is_pitcher else "hitting"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache_key = f"gamelog_{pid}_{group}_{today}"
    cached = _load(cache_key, ttl=3600)
    if cached:
        return cached

    try:
        url  = (f"https://statsapi.mlb.com/api/v1/people/{pid}/stats"
                f"?stats=gameLog&group={group}&season=2026")
        data = _get(url)
        splits = data.get("stats", [{}])[0].get("splits", [])

        games = []
        for s in splits:
            stat  = s["stat"]
            date  = s.get("date", "")
            opp   = s.get("opponent", {}).get("name", "?")
            ha    = "home" if s.get("isHome", False) else "away"

            # For pitchers, skip relief appearances
            if is_pitcher:
                ip = float(stat.get("inningsPitched", 0) or 0)
                if ip < 2.0:
                    continue

            # For batters, skip games with 0 plate appearances (off-days in log)
            if not is_pitcher:
                pa = int(stat.get("plateAppearances", 0) or 0)
                if pa == 0:
                    continue

            games.append({
                "date":     date,
                "opponent": opp,
                "home_away":ha,
                "stat":     stat,
            })

        games = sorted(games, key=lambda x: x["date"], reverse=True)
        _save(cache_key, games)
        return games
    except Exception as e:
        print(f"[mlb_batter_stats] game log failed {player_name}: {e}")
        return []


# ── Main stats function ────────────────────────────────────────────────────────

MIN_GAMES = {
    "Pitcher Strikeouts": 6,
    "Pitcher Fantasy Score": 6,
    "Pitching Outs": 6,
    "Earned Runs Allowed": 6,
    "Hitter Fantasy Score": 10,   # more games needed for stable FS average
    "Hits+Runs+RBIs": 10,
    "Singles": 10,
    "default": 10,  # raised from 8 — calibration shows 8-game samples too noisy
}

def _get_pitcher_hand(pitcher_name: str) -> str:
    """Get pitcher throwing hand — 'R' or 'L'. Defaults to 'R' if unknown."""
    cached = _load(f"phand_{_normalize(pitcher_name)}", ttl=86400)
    if cached:
        return cached.get("hand", "R")
    pid = find_player_id(pitcher_name)
    if not pid:
        return "R"
    try:
        url  = f"https://statsapi.mlb.com/api/v1/people/{pid}?fields=pitchHand"
        data = _get(url)
        hand = data.get("people", [{}])[0].get("pitchHand", {}).get("code", "R")
        _save(f"phand_{_normalize(pitcher_name)}", {"hand": hand})
        return hand
    except Exception:
        return "R"

def get_matchup_context(player_name: str, stat_type: str,
                        is_pitcher: bool, game: dict) -> dict:
    """
    Build matchup context dict for a pick — used by the scoring model.

    For pitchers: opponent team K rate, park factor, umpire
    For batters:  opposing pitcher, pitcher handedness, park factor, batting order
    """
    from data.matchup_context import PARK_FACTORS, _get_team_k_pct
    from data.mlb_advanced import get_today_schedule, UMPIRE_K_FACTOR

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    schedule = get_today_schedule(today)

    ctx = {
        "home_away":     "unknown",
        "opp_team":      "unknown",
        "opp_pitcher":   "unknown",
        "pitcher_hand":  "R",
        "park_factor":   1.0,
        "opp_k_pct":     None,
        "ump_name":      "",
        "game_total":    None,
        "context_notes": [],
    }

    if not game:
        return ctx

    ctx["home_away"] = "home" if game.get("is_home") else "away"
    ctx["opp_team"]  = game.get("opp_name", "unknown")
    ctx["ump_name"]  = game.get("ump_name", "")

    # Park factor — always based on home team
    home_name = game.get("home_name", "")
    ctx["park_factor"] = PARK_FACTORS.get(home_name, 1.0)

    # Opponent info
    opp_id = game.get("opp_id")
    if opp_id:
        ctx["opp_k_pct"] = _get_team_k_pct(opp_id)

    # For batters: get opposing pitcher and their handedness
    if not is_pitcher:
        opp_pitcher = game.get("opp_pitcher", "")
        ctx["opp_pitcher"] = opp_pitcher
        if opp_pitcher:
            ctx["pitcher_hand"] = _get_pitcher_hand(opp_pitcher)
            hand_label = "LHP" if ctx["pitcher_hand"] == "L" else "RHP"
            ctx["context_notes"].append(f"vs {opp_pitcher} ({hand_label})")

    # Park note
    pf = ctx["park_factor"]
    if pf > 1.06:
        ctx["context_notes"].append(f"⚠️ Hitter park (PF={pf:.2f})")
    elif pf < 0.96:
        ctx["context_notes"].append(f"✅ Pitcher park (PF={pf:.2f})")

    # Home/away note
    ctx["context_notes"].append(f"{'Home' if ctx['home_away']=='home' else 'Away'}")

    return ctx

def _find_player_game(player_name: str) -> dict | None:
    """Find today's game info for any player (pitcher or batter)."""
    from data.mlb_advanced import get_today_schedule
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    schedule = get_today_schedule(today)
    name_lower = _normalize(player_name).lower()

    for g in schedule:
        # Check pitchers
        if name_lower in _normalize(g.get("away_pitcher","")).lower():
            return {"opp_id": g["home_id"], "opp_name": g["home_name"],
                    "opp_pitcher": g["home_pitcher"],
                    "opp_pitcher_id": g.get("home_pitcher_id"),
                    "home_name": g["home_name"], "home_id": g["home_id"],
                    "ump_name": g.get("ump_name",""), "is_home": False}
        if name_lower in _normalize(g.get("home_pitcher","")).lower():
            return {"opp_id": g["away_id"], "opp_name": g["away_name"],
                    "opp_pitcher": g["away_pitcher"],
                    "opp_pitcher_id": g.get("away_pitcher_id"),
                    "home_name": g["home_name"], "home_id": g["home_id"],
                    "ump_name": g.get("ump_name",""), "is_home": True}
        # Check batters in lineup
        for order, bname in g.get("away_lineup", []):
            if name_lower in _normalize(bname).lower():
                return {"opp_id": g["home_id"], "opp_name": g["home_name"],
                        "opp_pitcher": g["home_pitcher"],
                        "opp_pitcher_id": g.get("home_pitcher_id"),
                        "home_name": g["home_name"], "home_id": g["home_id"],
                        "ump_name": g.get("ump_name",""), "is_home": False,
                        "batting_order": order,
                        "player_team": g.get("away_name", "")}
        for order, bname in g.get("home_lineup", []):
            if name_lower in _normalize(bname).lower():
                return {"opp_id": g["away_id"], "opp_name": g["away_name"],
                        "opp_pitcher": g["away_pitcher"],
                        "opp_pitcher_id": g.get("away_pitcher_id"),
                        "home_name": g["home_name"], "home_id": g["home_id"],
                        "ump_name": g.get("ump_name",""), "is_home": True,
                        "batting_order": order,
                        "player_team": g.get("home_name", "")}
    return None

def get_player_stats(player_name: str, stat_type: str, line: float,
                      forced_direction: str | None = None) -> dict | None:
    """
    Full stat analysis for any PP MLB stat type, with matchup context and H2H.
    Returns scoring dict or None if insufficient data.

    forced_direction: when set ("OVER"/"UNDER"), skips the avg-vs-line
    auto-inference below and uses this direction for hit_rate/adj_hit_rate.
    Needed for goblin/demon lines, which are direction-locked bets (always
    "More", just easier or harder) — auto-inference silently picks whichever
    side matches the player's average, which for any low-average counting
    stat (Home Runs, at minimum) is almost always the opposite of what's
    actually being offered. Confirmed live: every Home Run demon line today
    auto-inferred UNDER (since avg HR/game is well under the 0.5 line for
    everyone, including elite sluggers) while being scored as if it were the
    OVER bet it actually is — feeding a ~90% UNDER hit-rate into an OVER
    confidence calculation, backwards for every single pick. Standard lines
    (no forced_direction passed) keep the existing auto-inference, which is
    correct there — the system is meant to find whichever side has signal.
    """
    games = get_player_game_log(player_name, stat_type)
    if not games:
        return None

    # Compute per-game stat values
    values = []
    for g in games:
        v = _stat_value(stat_type, g["stat"])
        if v is not None:
            values.append((v, g["home_away"]))

    if not values:
        return None

    min_g = MIN_GAMES.get(stat_type, MIN_GAMES["default"])
    if len(values) < min_g:
        return None

    n      = min(len(values), 10)  # tightened from 15 — more recent = more predictive
    recent = [v for v, _ in values[:n]]
    l3     = recent[:3]
    l5     = recent[:5]

    avg_n   = sum(recent) / len(recent)
    avg_l3  = sum(l3) / len(l3)
    avg_l5  = sum(l5) / len(l5) if len(l5) >= 3 else avg_n

    over_hits  = sum(1 for v in recent if v > line)
    under_hits = sum(1 for v in recent if v < line)

    if forced_direction in ("OVER", "UNDER"):
        direction = forced_direction
    else:
        # Direction: use L5 avg when season avg is within 3% of line (borderline cases)
        edge_pct = abs(avg_n - line) / (line + 1e-9)
        if edge_pct < 0.03 and len(l5) >= 5:
            # Too close to call from season avg — let recent form decide
            direction = "OVER" if avg_l5 > line else "UNDER"
        else:
            direction = "OVER" if avg_n > line else "UNDER"
    hit_rate  = (over_hits / n) if direction == "OVER" else (under_hits / n)

    # Bayesian-adjusted hit rate: shrinks small-sample extremes toward 0.50.
    # Prior = 8 ghost games at 50% (4 hits, 4 misses). This prevents the model
    # from treating 9/10 (90%) the same as a true 90% performer.
    # 8/10 → adj 66.7% (barely passes 67% floor), 10/10 → adj 77.8%.
    _bayes_prior = 8
    _adj_hits = over_hits if direction == "OVER" else under_hits
    adj_hit_rate = round((_adj_hits + _bayes_prior * 0.5) / (n + _bayes_prior), 3)

    # Trend: L3 vs full window (for batters, this is noisier)
    trend = (avg_l3 - avg_n) / (avg_n + 1e-9)
    if direction == "UNDER":
        trend = -trend

    # Home/away splits
    home_vals = [v for v, ha in values[:n] if ha == "home"]
    away_vals = [v for v, ha in values[:n] if ha == "away"]
    home_avg  = round(sum(home_vals)/len(home_vals), 2) if home_vals else None
    away_avg  = round(sum(away_vals)/len(away_vals), 2) if away_vals else None

    # Hit rate specifically for current home/away location
    # Get today's game context
    is_pitcher = stat_type in PITCHER_STAT_TYPES
    try:
        game = _find_player_game(player_name)
        ctx  = get_matchup_context(player_name, stat_type, is_pitcher, game)
    except Exception:
        game, ctx = None, {}

    # Use home or away hit rate based on today's location
    home_away      = ctx.get("home_away", "unknown")
    location_vals  = home_vals if home_away == "home" else (away_vals if home_away == "away" else recent)
    loc_hits       = sum(1 for v in location_vals if (v > line if direction == "OVER" else v < line))
    loc_hit_rate   = (loc_hits / len(location_vals)) if location_vals else hit_rate
    loc_avg        = home_avg if home_away == "home" else (away_avg if home_away == "away" else avg_n)

    # ── Median for skewed distributions ──────────────────────────────────────
    # Batter stats like Fantasy Score and Total Bases are zero-inflated and
    # right-skewed (lots of 0s, occasional 30+ FS games). Mean is pulled up by
    # outliers. Median is a more robust central tendency for these.
    import statistics as _stat_mod
    _SKEWED_STATS = {"Hitter Fantasy Score", "Total Bases", "Hits+Runs+RBIs",
                     "Singles", "Hits"}
    median_val   = round(_stat_mod.median(recent), 2) if stat_type in _SKEWED_STATS else None
    # Standard deviation for probability distribution engine (Gaussian fallback)
    stat_std_dev = round(_stat_mod.stdev(recent), 2) if len(recent) >= 4 else None

    # ── Zero-inflated mixture model components ────────────────────────────────
    # MLB batter distributions are NOT Gaussian — they are zero-inflated and
    # right-skewed. Separating zero games from non-zero games lets us model the
    # two components independently:
    #   P(stat > line) = P(non-zero) × P(non-zero stat > line | non-zero game)
    # The pitcher's zero_inflation_factor then shifts P(zero) by pitcher quality.
    zero_game_count = sum(1 for v in recent if v == 0)
    p_zero_game     = round(zero_game_count / len(recent), 3) if recent else 0.0
    _nonzero_vals   = [v for v in recent if v > 0]
    nonzero_mean    = round(sum(_nonzero_vals) / len(_nonzero_vals), 2) if _nonzero_vals else None
    nonzero_std     = round(_stat_mod.stdev(_nonzero_vals), 2) if len(_nonzero_vals) >= 3 else None

    # ── Rare-event Poisson correction ────────────────────────────────────────
    # For low-frequency count stats (Walks, HR, etc.) the hit_rate from recent
    # games is dominated by lucky streaks.  A 7-game zero-walk streak doesn't
    # mean the player never walks — it means they had a hot streak.
    #
    # Walks are a Poisson process: P(0 walks per game) = e^(−λ), λ = avg/game.
    # Suzuki avg=0.25 → P(0 walks) = e^(−0.25) = 77.9%.
    # A 7-game streak would push the raw hit_rate to 87.5% — we cap it to 77.9%.
    #
    # Additionally, the opposing pitcher's BB% (walks per batter faced) adjusts
    # the expected walk probability for today's game:
    #   P(walk this game | pitcher) = 1 − (1 − pitcher_bb_pct)^PA
    # A high-walk pitcher can flip a 77% UNDER to a 50% — below our threshold.
    _RARE_COUNT_STATS = {"Walks", "Home Runs"}
    import math as _math
    poisson_p_zero      = None   # Poisson P(0 events) from season avg
    pitcher_bb_pct      = None   # pitcher walk rate (BB per batter faced)
    pitcher_walk_adj    = None   # adjusted P(0 walks today) given pitcher BB%

    if stat_type in _RARE_COUNT_STATS and avg_n > 0 and direction == "UNDER":
        # Poisson cap: season avg is the most honest λ estimate
        poisson_p_zero = round(_math.exp(-avg_n), 3)
        # Cap hit_rate to Poisson probability — streak can't inflate above this
        if hit_rate > poisson_p_zero:
            hit_rate = poisson_p_zero

    # ── Pitcher strength prior (opponent quality layer) ───────────────────────
    # Fetch a composite skill score for today's opposing pitcher so the batter
    # model can differentiate Wheeler from a AAA callup. The multiplier is
    # applied in score_pick to the effective_avg before the edge gate.
    pitcher_skill     = {}
    difficulty_mult   = 1.0
    pitcher_tier      = "unknown"
    pitcher_skill_str = ""

    if not is_pitcher:
        opp_pid = game.get("opp_pitcher_id") if game else None
        if opp_pid:
            try:
                from data.mlb_pitcher_strength import get_pitcher_skill_score, get_pitcher_season_stats
                pitcher_skill     = get_pitcher_skill_score(opp_pid)
                difficulty_mult   = pitcher_skill.get("multiplier", 1.0)
                pitcher_tier      = pitcher_skill.get("tier", "unknown")
                pitcher_skill_str = pitcher_skill.get("description", "")
                # For walk props: get pitcher's actual BB% to compute
                # today's walk probability (pitcher dominates batter tendency for walks)
                if stat_type == "Walks":
                    p_season = get_pitcher_season_stats(opp_pid)
                    pitcher_bb_pct = p_season.get("bb_pct")   # BB per batter faced
                    if pitcher_bb_pct is not None:
                        pa_per_game = 4.0   # typical batter PA per game
                        # P(batter walks at least once today) = 1 − (1−bb_pct)^PA
                        p_walk_game = 1.0 - ((1.0 - pitcher_bb_pct) ** pa_per_game)
                        pitcher_walk_adj = round(1.0 - p_walk_game, 3)  # P(0 walks today)
                        # Further cap hit_rate: take the LOWER of Poisson cap and
                        # pitcher-adjusted probability — both must be satisfied
                        if direction == "UNDER":
                            effective_cap = min(
                                poisson_p_zero if poisson_p_zero else 1.0,
                                pitcher_walk_adj
                            )
                            if hit_rate > effective_cap:
                                hit_rate = effective_cap
            except Exception:
                pass

    # ── H2H and Statcast advanced stats ──────────────────────────────────────
    # Skip H2H entirely if raw avg is within 8% of line — these picks won't
    # qualify anyway and H2H fetches are expensive (live API calls per player).
    _raw_edge = abs(avg_n - line) / (line + 1e-9) if line else 0
    h2h_data     = None
    vs_team_data = None
    statcast     = {}
    pitcher_vs_team_data = None
    h2h_conf_adj = 0.0

    if _raw_edge >= 0.08:
        try:
            from data.mlb_h2h import (get_batter_vs_pitcher, get_batter_vs_team,
                                       get_pitcher_vs_team, get_batter_statcast,
                                       get_pitcher_statcast, h2h_confidence_adjustment)

            player_pid = find_player_id(player_name)
            if player_pid:
                pid_int = int(player_pid)
                opp_pitcher_id = game.get("opp_pitcher_id") if game else None

                if not is_pitcher:
                    # Batter: get H2H vs today's pitcher + vs team + Statcast
                    if opp_pitcher_id:
                        h2h_data = get_batter_vs_pitcher(pid_int, opp_pitcher_id)
                    opp_id_for_h2h = game.get("opp_id") if game else None
                    if opp_id_for_h2h and not h2h_data:
                        vs_team_data = get_batter_vs_team(pid_int, opp_id_for_h2h)
                    statcast = get_batter_statcast(pid_int)
                    h2h_conf_adj = h2h_confidence_adjustment(h2h_data, vs_team_data,
                                                              direction, stat_type)
                else:
                    # Pitcher: get pitcher vs today's team + pitcher Statcast
                    opp_id_for_h2h = game.get("opp_id") if game else None
                    if opp_id_for_h2h:
                        pitcher_vs_team_data = get_pitcher_vs_team(pid_int, opp_id_for_h2h)
                    statcast = get_pitcher_statcast(pid_int)
        except Exception:
            pass

    return {
        "player":          player_name,
        "stat_type":       stat_type,
        "line":            line,
        "direction":       direction,
        "avg":             round(avg_n, 2),
        "avg_l3":          round(avg_l3, 2),
        "avg_l5":          round(avg_l5, 2),
        "hit_rate":        round(hit_rate, 3),
        "adj_hit_rate":    adj_hit_rate,   # Bayesian-shrunk hit rate (used by model cap)
        "hit_rate_over":   round(over_hits / n, 3),
        "hit_rate_under":  round(under_hits / n, 3),
        "location_hit_rate": round(loc_hit_rate, 3),
        "location_avg":    loc_avg,
        "over_hits":       over_hits,
        "under_hits":      under_hits,
        "n_games":         n,
        "recent_values":   recent[:8],
        "trend":           round(trend, 3),
        "home_avg":        home_avg,
        "away_avg":        away_avg,
        "home_away":       home_away,
        "opp_team":        ctx.get("opp_team", "unknown"),
        "opp_pitcher":     ctx.get("opp_pitcher", ""),
        "pitcher_hand":    ctx.get("pitcher_hand", "R"),
        "park_factor":     ctx.get("park_factor", 1.0),
        "context_notes":   ctx.get("context_notes", []),
        "sport":           "MLB",
        # Lineup context (for correlation model)
        "batting_order":   game.get("batting_order") if game else None,
        "player_team":     game.get("player_team", "") if game else "",
        # Skewed-stat median + std dev for probability engine (Gaussian fallback)
        "median_val":      median_val,
        "stat_std_dev":    stat_std_dev,
        # Zero-inflated mixture model components
        "p_zero_game":     p_zero_game,
        "nonzero_mean":    nonzero_mean,
        "nonzero_std":     nonzero_std,
        # Pitcher strength prior
        "pitcher_skill_score":   pitcher_skill.get("skill_score"),
        "pitcher_k_pct":         pitcher_skill.get("k_pct"),
        "pitcher_tier":          pitcher_tier,
        "difficulty_multiplier": difficulty_mult,
        "pitcher_skill_desc":    pitcher_skill_str,
        "pitcher_recent_era":    pitcher_skill.get("recent_era"),
        # Rare-event Poisson correction (Walks, HR)
        "poisson_p_zero":        poisson_p_zero,      # P(0 events) from season avg
        "pitcher_bb_pct":        pitcher_bb_pct,       # pitcher walk rate (BB/BF)
        "pitcher_walk_adj":      pitcher_walk_adj,     # P(0 walks today) given pitcher BB%
        # Advanced H2H + Statcast
        "h2h":                  h2h_data,
        "vs_team":              vs_team_data,
        "pitcher_vs_team":      pitcher_vs_team_data,
        "statcast":             statcast,
        "h2h_conf_adj":         round(h2h_conf_adj, 3),
    }
