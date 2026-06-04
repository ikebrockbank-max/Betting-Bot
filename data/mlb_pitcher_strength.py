"""
mlb_pitcher_strength.py — Composite pitcher ability score for batter prop context.

This is the "opponent quality prior" layer ChatGPT identified as the largest
structural gap in the batter model. Right now the batter model treats Wheeler
and a AAA callup identically except for career H2H batting avg (thin sample).

This module computes a pitcher_skill_score (0–10, 5.0 = league average) and
a difficulty_multiplier applied to expected batter output:

  skill 7.0 (Wheeler / Cole tier) → multiplier 0.80 → 20% harder for batters
  skill 5.0 (average starter)     → multiplier 1.00 → no adjustment
  skill 3.5 (weak starter)        → multiplier 1.15 → 15% easier for batters

Score components:
  35%  ERA vs league average
  30%  K rate vs league average
  15%  K–BB% (xFIP proxy — controls for contact quality and walk tendencies)
  10%  WHIP vs league average
  10%  Recent form (L5 starts ERA + K%)

Scale reference:
  ≥7.0  ace          Wheeler, Cole, Verlander tier
  6–7   above_avg    solid no. 1 or 2 starter
  4.5–6 average      mid-rotation
  3.5–4.5 below_avg  no. 5 / spot starter
  <3.5  weak         struggling starter / AAA callup

Difficulty multiplier:
  Each 1.0 above average = −10% expected batter output (slope 0.10/unit)
  Each 1.0 below average = +10% expected batter output
  Hard caps: [0.65, 1.35]  (never more than ±35% adjustment)
"""

import json
import time
import urllib.request
from pathlib import Path

_CACHE_DIR = Path("logs/pitcher_strength_cache")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_TTL_SEASON = 7200   # 2 h for season stats
_TTL_RECENT = 3600   # 1 h for recent form (may update after today's start)

# ── League averages (MLB 2026 baseline) ───────────────────────────────────────
LEAGUE = {
    "era":   4.10,
    "k_pct": 0.220,   # K per BF
    "bb_pct": 0.082,  # BB per BF
    "kbb":   0.138,   # K% − BB% (K-BB differential)
    "whip":  1.250,
}

# Scale: how many units of each stat equal 1 point on the 1–9 scale
# Calibrated so Wheeler-tier pitchers land ≈7.0
SCALE = {
    "era":  0.65,    # 0.65 ERA below avg = +1 pt
    "k":    0.030,   # 3.0% K above avg  = +1 pt
    "kbb":  0.040,   # 4.0% K-BB above avg = +1 pt
    "whip": 0.100,   # 0.10 WHIP below avg = +1 pt
}

# Weights must sum to 0.90 (remaining 0.10 = recent form)
SEASON_WEIGHTS = {"era": 0.35, "k": 0.30, "kbb": 0.15, "whip": 0.10}

TIER_THRESHOLDS = [
    (7.0, "ace"),
    (6.0, "above_avg"),
    (4.5, "average"),
    (3.5, "below_avg"),
    (0.0, "weak"),
]


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _cpath(key: str) -> Path:
    safe = key[:80].replace(" ", "_").replace("/", "_").replace(":", "_")
    return _CACHE_DIR / f"{safe}.json"

def _load(key: str, ttl: int):
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


# ── Component scoring ──────────────────────────────────────────────────────────

def _score_component(value: float, league_avg: float, scale: float,
                     higher_is_better: bool = True) -> float:
    """
    Convert a raw stat to a 1–9 component score (5.0 = league average).
    higher_is_better: True for K%, False for ERA/WHIP.
    """
    if higher_is_better:
        deviation = value - league_avg
    else:
        deviation = league_avg - value   # lower value = positive deviation = better
    return max(1.0, min(9.0, 5.0 + deviation / scale))


def _skill_tier(score: float) -> str:
    for threshold, label in TIER_THRESHOLDS:
        if score >= threshold:
            return label
    return "weak"


# ── Season stats ───────────────────────────────────────────────────────────────

def get_pitcher_season_stats(pitcher_id: int | str) -> dict:
    """
    Fetch 2026 season pitching stats from MLB Stats API.
    Returns {era, k_pct, bb_pct, kbb, whip, ip, n_starts} or {} on failure.
    """
    key = f"pstrength_season_{pitcher_id}"
    cached = _load(key, _TTL_SEASON)
    if cached is not None:
        return cached

    try:
        url  = (f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats"
                f"?stats=season&group=pitching&season=2026&gameType=R")
        data = _get(url)
        splits = data.get("stats", [{}])[0].get("splits", [])
        if not splits:
            _save(key, {})
            return {}

        st = splits[0]["stat"]
        ip = float(st.get("inningsPitched", 0) or 0)
        bf = max(1, int(st.get("battersFaced", 1) or 1))
        ks = int(st.get("strikeOuts", 0) or 0)
        bb = int(st.get("baseOnBalls", 0) or 0)
        gs = int(st.get("gamesStarted", 0) or 0)

        try:
            era = float(st.get("era", LEAGUE["era"]) or LEAGUE["era"])
        except (TypeError, ValueError):
            era = LEAGUE["era"]

        try:
            whip = float(st.get("whip", LEAGUE["whip"]) or LEAGUE["whip"])
        except (TypeError, ValueError):
            whip = LEAGUE["whip"]

        k_pct  = round(ks / bf, 4)
        bb_pct = round(bb / bf, 4)
        result = {
            "era":      round(era, 2),
            "k_pct":    k_pct,
            "bb_pct":   bb_pct,
            "kbb":      round(k_pct - bb_pct, 4),
            "whip":     round(whip, 3),
            "ip":       round(ip, 1),
            "n_starts": gs,
            "bf":       bf,
        }
        _save(key, result)
        return result
    except Exception as e:
        print(f"[pitcher_strength] season stats failed ({pitcher_id}): {e}")
        _save(key, {})
        return {}


# ── Recent form (L5 starts) ───────────────────────────────────────────────────

def get_pitcher_recent_form(pitcher_id: int | str, n_starts: int = 5) -> dict:
    """
    Last N starts performance: ERA, WHIP, K%.
    Returns {era_l5, whip_l5, k_pct_l5, n_starts} or {} on failure.
    Blend into season: season × 0.70 + recent × 0.30.
    """
    key = f"pstrength_recent_{pitcher_id}_{n_starts}"
    cached = _load(key, _TTL_RECENT)
    if cached is not None:
        return cached

    try:
        url = (f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats"
               f"?stats=gameLog&group=pitching&season=2026")
        data   = _get(url)
        splits = data.get("stats", [{}])[0].get("splits", [])

        # Keep starts only (IP >= 2.0), most recent first
        starts = sorted(
            [s for s in splits if float(s["stat"].get("inningsPitched", 0) or 0) >= 2.0],
            key=lambda x: x.get("date", ""),
            reverse=True,
        )[:n_starts]

        if not starts:
            _save(key, {})
            return {}

        total_ip = sum(float(s["stat"].get("inningsPitched", 0) or 0) for s in starts)
        total_er = sum(int(s["stat"].get("earnedRuns", 0) or 0) for s in starts)
        total_k  = sum(int(s["stat"].get("strikeOuts", 0) or 0) for s in starts)
        total_bf = sum(max(1, int(s["stat"].get("battersFaced", 1) or 1)) for s in starts)
        total_h  = sum(int(s["stat"].get("hits", 0) or 0) for s in starts)
        total_bb = sum(int(s["stat"].get("baseOnBalls", 0) or 0) for s in starts)

        if total_ip < 1.0:
            _save(key, {})
            return {}

        result = {
            "era_l5":   round(total_er / total_ip * 9, 2),
            "whip_l5":  round((total_h + total_bb) / total_ip, 3),
            "k_pct_l5": round(total_k / total_bf, 4),
            "bb_pct_l5": round(total_bb / total_bf, 4),
            "n_starts": len(starts),
        }
        _save(key, result)
        return result
    except Exception as e:
        print(f"[pitcher_strength] recent form failed ({pitcher_id}): {e}")
        _save(key, {})
        return {}


# ── Main composite score ───────────────────────────────────────────────────────

def get_pitcher_skill_score(pitcher_id: int | str) -> dict:
    """
    Composite pitcher skill score on a 0–10 scale (5.0 = league average).

    Returns:
      skill_score:          float, 1–9
      era, k_pct, whip:     season stats
      recent_era:           L5 starts ERA (None if insufficient data)
      tier:                 "ace" | "above_avg" | "average" | "below_avg" | "weak"
      multiplier:           batter output adjustment (1.0 = no change)
      description:          human-readable summary
    """
    if not pitcher_id:
        return {"skill_score": 5.0, "multiplier": 1.0, "tier": "average", "description": ""}

    key = f"pskill_{pitcher_id}"
    cached = _load(key, _TTL_SEASON)
    if cached is not None:
        return cached

    season = get_pitcher_season_stats(pitcher_id)
    recent = get_pitcher_recent_form(pitcher_id)

    if not season:
        result = {"skill_score": 5.0, "multiplier": 1.0, "tier": "average", "description": ""}
        _save(key, result)
        return result

    era  = season.get("era",   LEAGUE["era"])
    k    = season.get("k_pct", LEAGUE["k_pct"])
    kbb  = season.get("kbb",   LEAGUE["kbb"])
    whip = season.get("whip",  LEAGUE["whip"])

    # Score each component (1–9 scale, 5.0 = avg)
    era_c  = _score_component(era,  LEAGUE["era"],   SCALE["era"],  higher_is_better=False)
    k_c    = _score_component(k,    LEAGUE["k_pct"], SCALE["k"],    higher_is_better=True)
    kbb_c  = _score_component(kbb,  LEAGUE["kbb"],   SCALE["kbb"],  higher_is_better=True)
    whip_c = _score_component(whip, LEAGUE["whip"],  SCALE["whip"], higher_is_better=False)

    # Weighted season score (normalized so avg pitcher = exactly 5.0)
    raw_season = (era_c  * SEASON_WEIGHTS["era"]  +
                  k_c    * SEASON_WEIGHTS["k"]    +
                  kbb_c  * SEASON_WEIGHTS["kbb"]  +
                  whip_c * SEASON_WEIGHTS["whip"])
    weight_sum = sum(SEASON_WEIGHTS.values())   # = 0.90
    season_score = raw_season / weight_sum       # normalize to 1.0 weight

    # Blend recent form (10% weight)
    recent_score = season_score   # default: recent = same as season
    if recent.get("era_l5") is not None:
        r_era = recent["era_l5"]
        r_k   = recent.get("k_pct_l5", k)
        r_kbb = r_k - recent.get("bb_pct_l5", LEAGUE["bb_pct"])
        r_era_c = _score_component(r_era, LEAGUE["era"],   SCALE["era"], higher_is_better=False)
        r_k_c   = _score_component(r_k,   LEAGUE["k_pct"], SCALE["k"],   higher_is_better=True)
        r_kbb_c = _score_component(r_kbb, LEAGUE["kbb"],   SCALE["kbb"], higher_is_better=True)
        recent_score = (r_era_c * 0.50 + r_k_c * 0.30 + r_kbb_c * 0.20)

    final_score = round(max(1.0, min(9.0,
        season_score * 0.90 + recent_score * 0.10
    )), 2)

    tier       = _skill_tier(final_score)
    multiplier = pitcher_difficulty_multiplier(final_score)

    # Description for notifications
    parts = [f"ERA {era:.2f}", f"K% {k:.1%}", f"WHIP {whip:.2f}"]
    if recent.get("era_l5") is not None and abs(recent["era_l5"] - era) > 0.5:
        trend = "↑ form" if recent["era_l5"] < era else "↓ form"
        parts.append(f"L5 ERA {recent['era_l5']:.2f} ({trend})")
    description = " | ".join(parts)

    result = {
        "skill_score":  final_score,
        "tier":         tier,
        "multiplier":   multiplier,
        "era":          era,
        "k_pct":        round(k, 4),
        "whip":         round(whip, 3),
        "kbb":          round(kbb, 4),
        "recent_era":   recent.get("era_l5"),
        "recent_k_pct": recent.get("k_pct_l5"),
        "n_starts":     season.get("n_starts", 0),
        "description":  description,
    }
    _save(key, result)
    return result


def pitcher_difficulty_multiplier(skill_score: float) -> float:
    """
    Batter output multiplier based on pitcher skill score.

    Slope: each 1.0 above average = −10% expected batter output.
    Caps: [0.65, 1.35] — never more than ±35% adjustment.

    Examples:
      7.0 (ace)         → 0.80  (batter expected 20% less than usual)
      6.0 (above avg)   → 0.90
      5.0 (average)     → 1.00  (no adjustment)
      4.0 (below avg)   → 1.10
      3.0 (weak)        → 1.20
    """
    deviation  = skill_score - 5.0
    raw        = 1.0 - deviation * 0.10
    return round(max(0.65, min(1.35, raw)), 3)


def pitcher_sigma_multiplier(skill_score: float) -> float:
    """
    Scale factor for batter stat standard deviation (σ) based on pitcher quality.

    Ace pitchers don't just lower expected output — they *also* compress the
    distribution. Wheeler gives up fewer multi-hit games AND fewer HR spikes;
    outcomes cluster closer to zero. A weak starter creates high-variance games:
    more 0-for-4 days (batter swings over his head) AND more 3-hit explosions.

    This makes σ conditional on matchup, not just batter history — the key
    difference between a static estimator and a generative probabilistic model.

    Slope: each 1.0 above average tightens σ by 6%.
    Caps: [0.70, 1.35] — never compress below 70% or inflate above 135%.

    Examples:
      7.0 (Wheeler / ace)  → σ × 0.87  (tighter — consistent suppression)
      6.0 (above avg)      → σ × 0.94
      5.0 (average)        → σ × 1.00  (no change)
      4.0 (below avg)      → σ × 1.06
      3.0 (weak / callup)  → σ × 1.12  (wider — more extreme outcomes)
    """
    deviation = skill_score - 5.0
    raw       = 1.0 - deviation * 0.06
    return round(max(0.70, min(1.35, raw)), 3)
