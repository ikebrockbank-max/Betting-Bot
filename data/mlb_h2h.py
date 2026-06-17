"""
mlb_h2h.py — Head-to-head and Statcast advanced stats for MLB props.

Covers:
  - Batter vs specific pitcher (career H2H from MLB Stats API)
  - Batter vs opposing team  (season + career, MLB Stats API)
  - Pitcher vs opposing team (MLB Stats API)
  - Batter Statcast profile  (xBA, xSLG, barrel%, hard hit%, exit velo — Baseball Savant)

All free APIs. Results cached 24h for H2H (rarely changes intra-day),
6h for Statcast (leaderboard updates once daily).
"""

import csv
import io
import json
import time
import urllib.request
from pathlib import Path

_CACHE_DIR = Path("logs/h2h_cache")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_TTL = {
    "h2h":      86400,   # career matchup stats — 24h
    "vs_team":  86400,   # vs team — 24h
    "statcast": 21600,   # Baseball Savant leaderboard — 6h
}

def _cpath(key: str) -> Path:
    safe = key[:80].replace(" ", "_").replace("/", "_").replace(":", "_")
    return _CACHE_DIR / f"{safe}.json"

def _load(key: str, ttl_key: str = "h2h"):
    p = _cpath(key)
    ttl = _TTL.get(ttl_key, 86400)
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

def _get(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=12).read())

def _get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=12).read().decode("utf-8-sig", errors="replace")

def _safe_float(val) -> float | None:
    if val is None or val == "" or val == "null":
        return None
    try:
        return round(float(val), 3)
    except (TypeError, ValueError):
        return None


# ── Batter vs Pitcher (career H2H) ────────────────────────────────────────────

def get_batter_vs_pitcher(batter_id: int | str, pitcher_id: int | str) -> dict | None:
    """
    Career stats for this batter against this specific pitcher.
    Returns {ab, h, hr, k, bb, avg, ops} or None if < 5 AB (too small to matter).

    MLB API: /people/{batter_id}/stats?stats=vsPlayer&opposingPlayerId={pitcher_id}
    """
    key = f"h2h_{batter_id}_{pitcher_id}"
    cached = _load(key, "h2h")
    if cached is not None:
        return cached if cached else None

    try:
        url = (f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats"
               f"?stats=vsPlayer&group=hitting&opposingPlayerId={pitcher_id}")
        data   = _get(url)
        splits = data.get("stats", [{}])[0].get("splits", [])
        if not splits:
            _save(key, {})
            return None
        s  = splits[0]["stat"]
        ab = int(s.get("atBats", 0) or 0)
        if ab < 5:
            _save(key, {})
            return None
        result = {
            "ab":  ab,
            "h":   int(s.get("hits", 0) or 0),
            "hr":  int(s.get("homeRuns", 0) or 0),
            "k":   int(s.get("strikeOuts", 0) or 0),
            "bb":  int(s.get("baseOnBalls", 0) or 0),
            "avg": s.get("avg", ".---"),
            "slg": s.get("slg", ".---"),
            "ops": s.get("ops", ".---"),
        }
        _save(key, result)
        return result
    except Exception:
        _save(key, {})
        return None


# ── Batter vs Team (career) ────────────────────────────────────────────────────

def get_batter_vs_team(batter_id: int | str, team_id: int | str) -> dict | None:
    """
    Career stats for batter against this opposing team.
    Returns {ab, h, hr, k, avg, ops} or None if < 10 AB.

    MLB API: /people/{batter_id}/stats?stats=vsTeam&opposingTeamId={team_id}
    """
    key = f"bvt_{batter_id}_{team_id}"
    cached = _load(key, "vs_team")
    if cached is not None:
        return cached if cached else None

    try:
        url = (f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats"
               f"?stats=vsTeam&group=hitting&opposingTeamId={team_id}")
        data   = _get(url)
        splits = data.get("stats", [{}])[0].get("splits", [])
        if not splits:
            _save(key, {})
            return None
        s  = splits[0]["stat"]
        ab = int(s.get("atBats", 0) or 0)
        if ab < 10:
            _save(key, {})
            return None
        result = {
            "ab":  ab,
            "h":   int(s.get("hits", 0) or 0),
            "hr":  int(s.get("homeRuns", 0) or 0),
            "k":   int(s.get("strikeOuts", 0) or 0),
            "avg": s.get("avg", ".---"),
            "ops": s.get("ops", ".---"),
        }
        _save(key, result)
        return result
    except Exception:
        _save(key, {})
        return None


# ── Pitcher vs Team (career) ───────────────────────────────────────────────────

def get_pitcher_vs_team(pitcher_id: int | str, team_id: int | str) -> dict | None:
    """
    Career stats for pitcher against this opposing team.
    Returns {bf, k, k_pct, era, avg_allowed} or None if < 15 BF.

    MLB API: /people/{pitcher_id}/stats?stats=vsTeam&group=pitching&opposingTeamId={team_id}
    """
    key = f"pvt_{pitcher_id}_{team_id}"
    cached = _load(key, "vs_team")
    if cached is not None:
        return cached if cached else None

    try:
        url = (f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats"
               f"?stats=vsTeam&group=pitching&opposingTeamId={team_id}")
        data   = _get(url)
        splits = data.get("stats", [{}])[0].get("splits", [])
        if not splits:
            _save(key, {})
            return None
        s  = splits[0]["stat"]
        bf = int(s.get("battersFaced", 0) or 0)
        if bf < 15:
            _save(key, {})
            return None
        ip = float(s.get("inningsPitched", 0) or 0)
        er = int(s.get("earnedRuns", 0) or 0)
        ks = int(s.get("strikeOuts", 0) or 0)
        result = {
            "bf":    bf,
            "k":     ks,
            "k_pct": round(ks / bf, 3) if bf else None,
            "era":   round(er / ip * 9, 2) if ip > 0 else None,
            "avg":   s.get("avg", ".---"),
        }
        _save(key, result)
        return result
    except Exception:
        _save(key, {})
        return None


# ── Batter Statcast Profile (Baseball Savant) ──────────────────────────────────

_STATCAST_CACHE: dict[int, dict] = {}

def _load_statcast_batters(season: int = 2026) -> dict[int, dict]:
    """
    Batter Statcast from Baseball Savant — merges two leaderboards:
      1. expected_statistics → xBA, xSLG, xwOBA
      2. statcast leaderboard → avg exit velo, barrel%, hard-hit%
    Cached 6h.
    """
    global _STATCAST_CACHE
    if _STATCAST_CACHE:
        return _STATCAST_CACHE

    cached = _load("savant_batters_full", "statcast")
    if cached:
        _STATCAST_CACHE = {int(k): v for k, v in cached.items()}
        return _STATCAST_CACHE

    result: dict[int, dict] = {}

    # 1. Expected stats (xBA, xSLG, xwOBA)
    try:
        url  = (f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
                f"?type=batter&year={season}&position=&team=&min=50&csv=true")
        raw  = _get_text(url)
        rows = list(csv.DictReader(io.StringIO(raw)))
        for row in rows:
            try:
                pid = int(row.get("player_id") or 0)
                if not pid:
                    continue
                result[pid] = {
                    "xba":          _safe_float(row.get("est_ba")),
                    "xslg":         _safe_float(row.get("est_slg")),
                    "xwoba":        _safe_float(row.get("est_woba")),
                    "barrel_pct":   None,
                    "hard_hit_pct": None,
                    "exit_velo":    None,
                    "pa":           int(row.get("pa") or 0),
                }
            except Exception:
                pass
    except Exception as e:
        print(f"[mlb_h2h] xStats load failed: {e}")

    # 2. Statcast leaderboard (exit velo, barrel%, hard-hit%)
    try:
        url2 = (f"https://baseballsavant.mlb.com/leaderboard/statcast"
                f"?type=batter&year={season}&position=&team=&min=50&csv=true")
        raw2 = _get_text(url2)
        rows2 = list(csv.DictReader(io.StringIO(raw2)))
        for row in rows2:
            try:
                pid = int(row.get("player_id") or 0)
                if not pid:
                    continue
                ev  = _safe_float(row.get("avg_hit_speed"))   # avg exit velocity
                brl = _safe_float(row.get("brl_percent"))     # barrel % (of BIP)
                hh  = _safe_float(row.get("ev95percent"))     # hard-hit % (EV >= 95mph)
                if pid in result:
                    result[pid]["exit_velo"]    = ev
                    result[pid]["barrel_pct"]   = round(brl / 100, 3) if brl is not None else None
                    result[pid]["hard_hit_pct"] = round(hh / 100, 3) if hh is not None else None
                else:
                    result[pid] = {
                        "xba": None, "xslg": None, "xwoba": None, "pa": 0,
                        "exit_velo":    ev,
                        "barrel_pct":   round(brl / 100, 3) if brl is not None else None,
                        "hard_hit_pct": round(hh / 100, 3) if hh is not None else None,
                    }
            except Exception:
                pass
    except Exception as e:
        print(f"[mlb_h2h] Statcast EV load failed: {e}")

    _STATCAST_CACHE = result
    _save("savant_batters_full", {str(k): v for k, v in result.items()})
    return result


def get_batter_statcast(batter_id: int) -> dict:
    """Statcast metrics for a batter. Empty dict if not found."""
    return _load_statcast_batters().get(batter_id, {})


# ── Pitcher Statcast Profile (Baseball Savant) ─────────────────────────────────

_PITCHER_STATCAST_CACHE: dict[int, dict] = {}

def _load_statcast_pitchers(season: int = 2026) -> dict[int, dict]:
    """
    Pitcher Statcast from Baseball Savant — merges two leaderboards:
      1. expected_statistics?type=pitcher → xBA allowed, xwOBA allowed
      2. statcast?type=pitcher           → avg EV allowed, barrel% allowed, hard-hit% allowed
    """
    global _PITCHER_STATCAST_CACHE
    if _PITCHER_STATCAST_CACHE:
        return _PITCHER_STATCAST_CACHE

    cached = _load("savant_pitchers_full", "statcast")
    if cached:
        _PITCHER_STATCAST_CACHE = {int(k): v for k, v in cached.items()}
        return _PITCHER_STATCAST_CACHE

    result: dict[int, dict] = {}

    # 1. Expected stats
    try:
        url = (f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
               f"?type=pitcher&year={season}&position=&team=&min=50&csv=true")
        raw  = _get_text(url)
        rows = list(csv.DictReader(io.StringIO(raw)))
        for row in rows:
            try:
                pid = int(row.get("player_id") or 0)
                if not pid:
                    continue
                result[pid] = {
                    "xba_allowed":      _safe_float(row.get("est_ba")),
                    "xwoba_allowed":    _safe_float(row.get("est_woba")),
                    "barrel_pct":       None,
                    "hard_hit_allowed": None,
                    "ev_allowed":       None,
                    "pa":               int(row.get("pa") or 0),
                }
            except Exception:
                pass
    except Exception as e:
        print(f"[mlb_h2h] Pitcher xStats load failed: {e}")

    # 2. Statcast leaderboard (EV, barrel%, hard-hit% allowed)
    try:
        url2 = (f"https://baseballsavant.mlb.com/leaderboard/statcast"
                f"?type=pitcher&year={season}&position=&team=&min=50&csv=true")
        raw2 = _get_text(url2)
        rows2 = list(csv.DictReader(io.StringIO(raw2)))
        for row in rows2:
            try:
                pid = int(row.get("player_id") or 0)
                if not pid:
                    continue
                ev  = _safe_float(row.get("avg_hit_speed"))
                brl = _safe_float(row.get("brl_percent"))
                hh  = _safe_float(row.get("ev95percent"))
                if pid in result:
                    result[pid]["ev_allowed"]       = ev
                    result[pid]["barrel_pct"]       = round(brl / 100, 3) if brl is not None else None
                    result[pid]["hard_hit_allowed"] = round(hh / 100, 3) if hh is not None else None
                else:
                    result[pid] = {
                        "xba_allowed": None, "xwoba_allowed": None, "pa": 0,
                        "ev_allowed":       ev,
                        "barrel_pct":       round(brl / 100, 3) if brl is not None else None,
                        "hard_hit_allowed": round(hh / 100, 3) if hh is not None else None,
                    }
            except Exception:
                pass
    except Exception as e:
        print(f"[mlb_h2h] Pitcher Statcast EV load failed: {e}")

    _PITCHER_STATCAST_CACHE = result
    _save("savant_pitchers_full", {str(k): v for k, v in result.items()})
    return result


def get_pitcher_statcast(pitcher_id: int) -> dict:
    """Statcast metrics for a pitcher (as allowed stats). Empty dict if not found."""
    return _load_statcast_pitchers().get(pitcher_id, {})


# ── H2H confidence adjustment ──────────────────────────────────────────────────

def _parse_avg(avg_str: str) -> float | None:
    """Parse MLB batting average string like '.333' or '0.333' to float."""
    if not avg_str or avg_str == ".---":
        return None
    try:
        return float(avg_str)
    except (ValueError, TypeError):
        return None

def _h2h_weight(ab: int) -> float:
    """
    Scale H2H confidence adjustment by sample size.
    ChatGPT recommendation: tiny samples are mostly noise.
      0-9 AB  → ignore (0.0)
      10-24   → light  (0.35)
      25-49   → moderate (0.65)
      50+     → meaningful (1.0)
    """
    if ab < 10:
        return 0.0
    if ab < 25:
        return 0.35
    if ab < 50:
        return 0.65
    return 1.0

def h2h_confidence_adjustment(h2h: dict | None, vs_team: dict | None,
                               direction: str, stat_type: str) -> float:
    """
    Returns a confidence adjustment scaled by sample size (ChatGPT recommendation).
    Max magnitude: ±0.04 at 50+ AB, tapering to 0 below 10 AB.

    Batter hitting props:
      ≥.320 avg vs this pitcher/team → OVER boost
      ≤.200 avg                      → OVER penalty / UNDER boost
    """
    if not h2h and not vs_team:
        return 0.0

    data  = h2h or vs_team
    ab    = data.get("ab", 0)
    weight = _h2h_weight(ab)
    if weight == 0.0:
        return 0.0

    avg_val = _parse_avg(data.get("avg", ".---"))
    if avg_val is None:
        return 0.0

    MAX_ADJ = 0.04

    hitting_props = {"Hits", "Singles", "Total Bases", "Hitter Fantasy Score",
                     "Hits+Runs+RBIs", "Home Runs", "Runs", "RBI"}
    if stat_type not in hitting_props:
        return 0.0

    raw_adj = 0.0
    if direction == "OVER":
        if avg_val >= 0.320:
            raw_adj = MAX_ADJ
        elif avg_val >= 0.280:
            raw_adj = MAX_ADJ * 0.5
        elif avg_val <= 0.200:
            raw_adj = -MAX_ADJ
        elif avg_val <= 0.230:
            raw_adj = -MAX_ADJ * 0.5
    elif direction == "UNDER":
        if avg_val <= 0.200:
            raw_adj = MAX_ADJ
        elif avg_val <= 0.230:
            raw_adj = MAX_ADJ * 0.5
        elif avg_val >= 0.320:
            raw_adj = -MAX_ADJ
        elif avg_val >= 0.280:
            raw_adj = -MAX_ADJ * 0.5

    return round(raw_adj * weight, 4)


# ── Statcast quality-contact score ────────────────────────────────────────────

# 2026 MLB league averages (batters)
_BATTER_LEAGUE_AVG = {
    "xwoba":        0.315,
    "barrel_pct":   0.085,   # 8.5%
    "hard_hit_pct": 0.420,   # 42%
    "exit_velo":    88.5,
    "xba":          0.240,
}

def compute_statcast_quality_score(statcast: dict, is_pitcher: bool = False) -> dict:
    """
    Convert Statcast metrics into a quality_contact_score and diagnostic signals.

    For batters: high xwOBA + Barrel% + HardHit% = better batter = OVER boost
    For pitchers: these are "allowed" metrics, so HIGH values = pitcher is getting hit hard

    Returns:
      {quality_score, xba_delta, regression_flag, note}

    quality_score: 0.0-1.0 (0.5 = league average)
      >0.6 = elite contact quality (OVER-favorable for batters)
      <0.4 = poor contact quality (OVER-unfavorable for batters)
    xba_delta: xBA - actual BA (positive = due for positive regression)
    regression_flag: "regression_up" | "regression_down" | None
    """
    avg = _BATTER_LEAGUE_AVG

    score = 0.5
    notes = []
    xba_delta = None

    if not is_pitcher:
        xwoba    = statcast.get("xwoba")
        brl      = statcast.get("barrel_pct")
        hh       = statcast.get("hard_hit_pct")
        ev       = statcast.get("exit_velo")
        xba      = statcast.get("xba")

        # xwOBA: strongest predictor (40% weight)
        if xwoba is not None:
            z = (xwoba - avg["xwoba"]) / 0.040
            score += z * 0.40 * 0.10
            if xwoba >= 0.370:
                notes.append(f"xwOBA {xwoba:.3f} (elite)")
            elif xwoba >= 0.340:
                notes.append(f"xwOBA {xwoba:.3f} (above avg)")
            elif xwoba <= 0.270:
                notes.append(f"xwOBA {xwoba:.3f} (weak)")

        # Barrel% (30% weight)
        if brl is not None:
            z = (brl - avg["barrel_pct"]) / 0.040
            score += z * 0.30 * 0.10
            if brl >= 0.150:
                notes.append(f"Barrel {brl*100:.1f}% (elite)")
            elif brl >= 0.110:
                notes.append(f"Barrel {brl*100:.1f}% (above avg)")

        # Hard hit% (20% weight)
        if hh is not None:
            z = (hh - avg["hard_hit_pct"]) / 0.070
            score += z * 0.20 * 0.10

        # Exit velo (10% weight)
        if ev is not None:
            z = (ev - avg["exit_velo"]) / 2.5
            score += z * 0.10 * 0.10

        # xBA regression signal — key buy-low/sell-high indicator
        if xba is not None:
            # We don't have actual BA in statcast dict — xba delta vs xba itself
            # Flag elite xBA (likely outperforming) or poor xBA (due to bounce back)
            if xba >= 0.290:
                notes.append(f"xBA {xba:.3f} (buy signal)")
                xba_delta = round(xba - avg["xba"], 3)
            elif xba <= 0.200:
                notes.append(f"xBA {xba:.3f} (sell signal)")
                xba_delta = round(xba - avg["xba"], 3)

    else:
        # Pitcher — high allowed metrics = pitcher gets hit hard = UNDER-favorable
        xba_alwd = statcast.get("xba_allowed")
        brl_alwd = statcast.get("barrel_pct")
        hh_alwd  = statcast.get("hard_hit_allowed")

        # Invert: for pitchers, LOWER is better
        if xba_alwd is not None:
            z = (avg["xba"] - xba_alwd) / 0.030
            score += z * 0.50 * 0.10
            if xba_alwd <= 0.220:
                notes.append(f"xBA-alwd {xba_alwd:.3f} (suppresses contact)")
            elif xba_alwd >= 0.270:
                notes.append(f"xBA-alwd {xba_alwd:.3f} (gets hit hard)")

        if brl_alwd is not None:
            z = (avg["barrel_pct"] - brl_alwd) / 0.040
            score += z * 0.30 * 0.10

        if hh_alwd is not None:
            z = (avg["hard_hit_pct"] - hh_alwd) / 0.070
            score += z * 0.20 * 0.10

    quality_score = round(max(0.2, min(0.8, score)), 3)

    regression_flag = None
    if xba_delta and xba_delta > 0.040:
        regression_flag = "regression_up"   # xBA > avg → hitting better than results show
    elif xba_delta and xba_delta < -0.040:
        regression_flag = "regression_down"

    return {
        "quality_score":    quality_score,
        "xba_delta":        xba_delta,
        "regression_flag":  regression_flag,
        "note":             " · ".join(notes) if notes else "",
    }


# ── Formatted context line for notifications ───────────────────────────────────

def format_h2h_note(h2h: dict | None, vs_team: dict | None, statcast: dict,
                    opp_pitcher: str, opp_team: str, is_pitcher: bool = False) -> str:
    """
    Build a compact H2H + Statcast note for notifications.
    Returns empty string if no usable data.

    Examples:
      "H2H vs Gasser: 4/12 (.333) · Barrel 12.4% · EV 92.1"
      "vs Brewers career: 18/52 (.346)"
    """
    parts = []

    if h2h:
        pname = opp_pitcher.split()[-1] if opp_pitcher else "P"
        parts.append(f"H2H vs {pname}: {h2h['h']}/{h2h['ab']} ({h2h['avg']})")
    elif vs_team and not is_pitcher:
        tname = opp_team.split()[-1] if opp_team else "opp"
        parts.append(f"vs {tname} career: {vs_team['h']}/{vs_team['ab']} ({vs_team['avg']})")

    if statcast and not is_pitcher:
        sc_parts = []
        brl = statcast.get("barrel_pct")
        ev  = statcast.get("exit_velo")
        hh  = statcast.get("hard_hit_pct")
        if brl is not None:
            sc_parts.append(f"Brl {brl*100:.1f}%")
        if ev is not None:
            sc_parts.append(f"EV {ev:.1f}")
        elif hh is not None:
            sc_parts.append(f"HH {hh*100:.1f}%")
        if sc_parts:
            parts.append(" ".join(sc_parts))

    return " · ".join(parts)


def format_pitcher_h2h_note(pitcher_vs_team: dict | None, opp_team: str) -> str:
    """Note for pitcher vs today's opposing team."""
    if not pitcher_vs_team:
        return ""
    tname = opp_team.split()[-1] if opp_team else "opp"
    k_pct = pitcher_vs_team.get("k_pct")
    era   = pitcher_vs_team.get("era")
    bf    = pitcher_vs_team.get("bf", 0)
    parts = [f"vs {tname}: {pitcher_vs_team['k']}K/{bf}BF"]
    if k_pct is not None:
        parts.append(f"({k_pct*100:.0f}% K)")
    if era is not None:
        parts.append(f"ERA {era:.2f}")
    return " ".join(parts)
