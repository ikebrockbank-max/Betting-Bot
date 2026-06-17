"""
projection_engine.py — Stat projection engine.

Instead of scoring picks, this computes a projected stat value for each player/game,
then calculates edge vs the PrizePicks line.

Architecture:
  projected_stat = base_avg × (chain of dampened multipliers)
  edge_pct       = (projected - line) / line
  direction      = OVER if edge > 0, UNDER if edge < 0
  confidence     = f(|edge_pct|, sample_size, variance, consistency)

Outputs per pick:
  projected      — expected stat value
  line           — PP line
  edge_pct       — % gap (positive = OVER edge, negative = UNDER edge)
  confidence     — 0–100 how certain we are in the projection
  factors        — list of what's pushing the projection up or down
  direction      — OVER or UNDER

Usage:
  from projection_engine import project_pick
  result = project_pick(pick, stats, context)
"""

import math
from typing import Any

# ── League averages (used to normalise multipliers) ───────────────────────────
MLB_LEAGUE_AVG = {
    "Pitcher Strikeouts": 5.5,   # avg Ks per start for qualified starters
    "Strikeouts":         5.5,
    "Hits Allowed":       7.0,
    "Walks":              2.5,
    "Earned Runs":        2.5,
}

NBA_LEAGUE_AVG = {
    "Points":           20.0,
    "Rebounds":         7.0,
    "Assists":          5.0,
    "3-Pointers Made":  2.0,
    "Steals":           1.2,
    "Blocks":           0.8,
    "Pts+Rebs+Asts":    30.0,
    "Pts+Rebs":         26.0,
    "Pts+Asts":         24.0,
}

# Multiplier dampening — prevents single signals from dominating
# 1.0 = full signal, 0.5 = half signal, 0.0 = ignore
DAMP = {
    "opp_factor":     0.70,   # opponent quality (dampened — opponent isn't everything)
    "park_factor":    0.50,   # park/environment
    "home_away":      0.40,   # home/away split
    "trend":          0.30,   # recent form vs season
    "umpire":         0.25,   # umpire tendency (small effect)
    "lineup_pos":     0.20,   # batting order position
    "vegas":          0.35,   # Vegas total signal
}

# Minimum edge to consider a pick
MIN_EDGE_PCT  = 0.08   # 8%
MIN_GAMES     = 5


def _variance_score(values: list[float]) -> float:
    """
    Coefficient of variation — lower = more consistent = higher confidence.
    Returns 0–1 where 1 = perfectly consistent, 0 = wildly variable.
    """
    if len(values) < 2:
        return 0.5
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.5
    var  = sum((v - mean) ** 2 for v in values) / len(values)
    cv   = math.sqrt(var) / mean   # coefficient of variation
    # cv of 0.3 or less = consistent, cv of 1.0+ = very inconsistent
    return max(0, min(1.0, 1.0 - cv))


def _confidence_from_edge_and_sample(edge_pct: float, n_games: int,
                                      consistency: float,
                                      hit_rate: float) -> float:
    """
    Confidence = f(edge size, sample size, consistency, historical hit rate).

    - Large edge + large sample + consistent + high hit rate = high confidence
    - Small edge + small sample = low confidence
    """
    # Edge component (0–1): bigger edge = more confident
    edge_comp = min(1.0, abs(edge_pct) / 0.30)   # 30% edge = full credit

    # Sample size component (0–1): more data = more reliable
    sample_comp = min(1.0, n_games / 12)

    # Consistency component (0–1): how variable is the player?
    consistency_comp = consistency

    # Historical hit rate on this specific line (0–1)
    hit_comp = hit_rate

    # Weighted average
    raw = (edge_comp * 0.30 +
           sample_comp * 0.20 +
           consistency_comp * 0.20 +
           hit_comp * 0.30)

    # Map to 50–95% range (never below 50% since we've already confirmed direction)
    return round(50 + raw * 45, 1)


# ─────────────────────────────────────────────────────────────────────────────
# MLB PITCHER PROJECTION
# ─────────────────────────────────────────────────────────────────────────────

def project_mlb_pitcher(stats: dict, context: dict, stat_type: str) -> dict:
    """
    Project expected Ks for a pitcher using ADDITIVE adjustments to avoid
    multiplier compounding inflation.

    Formula:
      projected = base_avg + sum(signed_adjustments)
      adjustment_cap: projected can't move more than ±20% from base_avg

    The key fix vs the previous version: pitcher's base avg already bakes in
    their arsenal and stuff. We only adjust based on how TODAY'S specific
    context differs from their typical start.
    """
    factors    = []
    adjustments = []   # list of (label, value) tuples — additive Ks

    avg         = stats.get("avg", 0)
    values      = stats.get("recent_values", [])
    n           = stats.get("n_games", 0)
    hit_rate    = stats.get("hit_rate", 0.5)
    consistency = _variance_score(values)
    comps       = context.get("components", {})

    if avg <= 0 or n < 6:
        return {"projected": None, "edge_pct": None, "confidence": None,
                "factors": ["Insufficient data (need 6+ starts)"]}

    league_k_avg = 0.220

    # 1. Opponent K rate adjustment
    # League avg team faces ~22% K rate. If opponent is 25%, that's +13.6% more Ks.
    opp_k = comps.get("opp_k_pct")
    if opp_k:
        diff_pct = (opp_k - league_k_avg) / league_k_avg  # e.g. +0.136
        adj_ks   = avg * diff_pct * 0.60   # 60% of the expected lift (dampened)
        adjustments.append(("opp_k", adj_ks))
        diff_label = f"{diff_pct*100:+.0f}%"
        if diff_pct > 0.05:
            factors.append(f"✅ High-K lineup: {opp_k:.1%} ({diff_label} vs avg) → +{adj_ks:.1f} Ks")
        elif diff_pct < -0.05:
            factors.append(f"⚠️ Low-K lineup: {opp_k:.1%} ({diff_label} vs avg) → {adj_ks:.1f} Ks")

    # 2. Park factor
    # Park factor only affects about 5-8% of Ks — minor adjustment
    pf = comps.get("park_factor", 1.0)
    if abs(pf - 1.0) > 0.02:
        adj_ks = avg * (1.0 - pf) * 0.15   # very small effect on Ks
        adjustments.append(("park", adj_ks))
        if pf < 0.96:
            factors.append(f"✅ Pitcher park (PF={pf:.2f})")
        elif pf > 1.06:
            factors.append(f"⚠️ Hitter park (PF={pf:.2f})")

    # 3. Umpire — well-documented, but small effect (~0.5-1 K per game for extreme umps)
    ump_adj = comps.get("ump_adj", 0.0)
    if abs(ump_adj) > 0.005:
        adj_ks = ump_adj * 45   # convert K-rate adjustment to raw Ks (approx 45 BF/start)
        adj_ks = max(-1.2, min(1.2, adj_ks))   # cap at ±1.2 Ks
        adjustments.append(("ump", adj_ks))
        ump_notes = [n for n in context.get("description",[]) if "umpire" in n.lower()]
        if ump_notes:
            factors.append(f"{ump_notes[0]} ({adj_ks:+.1f} Ks)")

    # 4. Home/away — modest effect for pitchers (~0.3-0.5 Ks)
    home_away = context.get("home_away", "unknown")
    if home_away == "home":
        adjustments.append(("home", 0.3))
        factors.append("Home start (+0.3 Ks)")
    elif home_away == "away":
        adjustments.append(("away", -0.2))
        factors.append("Away start (-0.2 Ks)")

    # 5. Vegas game total — strong signal for overall run environment
    total = comps.get("game_total")
    if total:
        league_avg_total = 8.5
        # Low total = pitcher dominant. Each run below avg ≈ +0.3 Ks
        adj_ks = (league_avg_total - total) * 0.25
        adj_ks = max(-1.0, min(1.0, adj_ks))
        adjustments.append(("vegas", adj_ks))
        if total < 7.5:
            factors.append(f"✅ Low O/U ({total}) → {adj_ks:+.1f} Ks")
        elif total > 9.5:
            factors.append(f"⚠️ High O/U ({total}) → {adj_ks:+.1f} Ks")
        else:
            factors.append(f"O/U: {total}")

    # 6. Recent form — L3 vs season avg
    if len(values) >= 3:
        l3_avg     = sum(values[:3]) / 3
        form_diff  = l3_avg - avg
        # Weight recent form conservatively (30% weight)
        adj_ks     = form_diff * 0.30
        adj_ks     = max(-1.5, min(1.5, adj_ks))
        if abs(form_diff) > 1.0:
            adjustments.append(("form", adj_ks))
            if form_diff > 1.0:
                factors.append(f"✅ Hot: L3={l3_avg:.1f} vs avg={avg:.1f} ({adj_ks:+.1f})")
            else:
                factors.append(f"⚠️ Cold: L3={l3_avg:.1f} vs avg={avg:.1f} ({adj_ks:+.1f})")

    # NOTE: We do NOT add an arsenal multiplier — the player's avg already
    # reflects their arsenal. Arsenal data is shown for context only.
    arsenal = comps.get("arsenal_k_pct")
    if arsenal and comps.get("opp_k_pct"):
        # Arsenal context note only (not a multiplier)
        factors.append(f"Arsenal K profile: {arsenal:.1%}/pitch")

    # Sum adjustments and apply cap
    total_adj = sum(v for _, v in adjustments)
    projected = avg + total_adj
    # Hard cap: projection can't exceed ±20% from base avg
    max_proj = avg * 1.20
    min_proj = avg * 0.80
    projected = round(max(min_proj, min(max_proj, projected)), 1)

    return projected, factors, consistency, hit_rate


# ─────────────────────────────────────────────────────────────────────────────
# NBA PLAYER PROJECTION
# ─────────────────────────────────────────────────────────────────────────────

def project_nba_player(stats: dict, context: dict, stat_type: str) -> tuple:
    """Project expected NBA stat using opponent defense, home/away, and trend."""
    factors    = []
    multipliers = []

    avg        = stats.get("avg", 0)
    values     = stats.get("recent_values", [])
    n          = stats.get("n_games", 0)
    hit_rate   = stats.get("hit_rate", 0.5)
    consistency = _variance_score(values)
    comps      = context.get("components", {})

    if avg <= 0 or n < MIN_GAMES:
        return None, ["Insufficient data"], 0.5, 0.5

    base = avg

    # 1. Opponent defense
    opp_def = context.get("opp_def_val")
    if opp_def:
        league_avg_def = NBA_LEAGUE_AVG.get(stat_type, avg)
        def_ratio  = opp_def / league_avg_def
        dampened   = 1.0 + (def_ratio - 1.0) * DAMP["opp_factor"]
        multipliers.append(dampened)
        opp_team = context.get("opp_team", "")
        if def_ratio > 1.04:
            factors.append(f"✅ {opp_team} weak defense (allows {opp_def:.1f})")
        elif def_ratio < 0.96:
            factors.append(f"⚠️ {opp_team} elite defense (allows {opp_def:.1f})")

    # 2. Home/away using player's own splits
    home_away = context.get("home_away", "unknown")
    splits    = context.get("splits", {})
    if home_away == "home" and splits.get("home_avg"):
        h_avg    = splits["home_avg"]
        ratio    = h_avg / avg if avg else 1.0
        dampened = 1.0 + (ratio - 1.0) * DAMP["home_away"]
        multipliers.append(dampened)
        if ratio > 1.08:
            factors.append(f"✅ Strong at home (avg {h_avg})")
        elif ratio < 0.92:
            factors.append(f"⚠️ Struggles at home (avg {h_avg})")
        factors.append(f"Home game")
    elif home_away == "away" and splits.get("away_avg"):
        a_avg    = splits["away_avg"]
        ratio    = a_avg / avg if avg else 1.0
        dampened = 1.0 + (ratio - 1.0) * DAMP["home_away"]
        multipliers.append(dampened)
        if ratio > 1.08:
            factors.append(f"✅ Strong on road (avg {a_avg})")
        elif ratio < 0.92:
            factors.append(f"⚠️ Struggles on road (avg {a_avg})")
        factors.append(f"Away game")

    # 3. Recent form
    if len(values) >= 3:
        l3_avg = sum(values[:3]) / 3
        if avg > 0:
            form_ratio = l3_avg / avg
            dampened   = 1.0 + (form_ratio - 1.0) * DAMP["trend"]
            multipliers.append(dampened)
            if form_ratio > 1.12:
                factors.append(f"✅ Hot: L3 avg {l3_avg:.1f} vs {avg:.1f}")
            elif form_ratio < 0.88:
                factors.append(f"⚠️ Cold: L3 avg {l3_avg:.1f} vs {avg:.1f}")

    combined  = 1.0
    for m in multipliers:
        combined *= m

    projected = round(base * combined, 2)
    return projected, factors, consistency, hit_rate


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def project_pick(pick: dict, stats: dict, context: dict) -> dict:
    """
    Full projection for any pick.

    Returns:
      projected   — expected stat value
      line        — PP line
      edge_pct    — (projected - line) / line  (positive = OVER, negative = UNDER)
      direction   — OVER or UNDER
      confidence  — 0–100
      factors_pos — list of what's boosting the projection
      factors_neg — list of what's dragging the projection down
      skip        — True if edge < MIN_EDGE_PCT or insufficient data
      reason      — why skip=True
    """
    sport     = pick["sport"]
    stat_type = pick["stat_type"]
    line      = pick["line"]

    # Route to sport-specific projection
    projected_val  = None
    factors        = []
    consistency    = 0.5
    hit_rate       = stats.get("hit_rate", 0.5)

    if sport == "MLB":
        result = project_mlb_pitcher(stats, context, stat_type)
        if isinstance(result, tuple):
            projected_val, factors, consistency, hit_rate = result
        else:
            projected_val = result
    elif sport in ("NBA", "WNBA"):
        projected_val, factors, consistency, hit_rate = project_nba_player(
            stats, context, stat_type)
    elif sport == "TENNIS":
        # Simple: use avg with trend multiplier
        avg    = stats.get("avg", 0)
        values = stats.get("recent_values", [])
        if avg and values and len(values) >= 3:
            l3_avg = sum(values[:3]) / 3
            form   = 1.0 + ((l3_avg / avg) - 1.0) * DAMP["trend"]
            projected_val = round(avg * form, 2)
            consistency   = _variance_score(values)
            if form > 1.05:
                factors.append(f"✅ Hot form: L3 avg {l3_avg:.1f} vs {avg:.1f}")
            elif form < 0.95:
                factors.append(f"⚠️ Cold form: L3 avg {l3_avg:.1f} vs {avg:.1f}")

    if projected_val is None:
        return {
            "projected":   None,
            "line":        line,
            "edge_pct":    None,
            "direction":   None,
            "confidence":  None,
            "factors_pos": [],
            "factors_neg": [],
            "skip":        True,
            "reason":      "Projection failed — insufficient data",
        }

    # Compute edge
    edge_pct  = (projected_val - line) / (line + 1e-9)
    direction = "OVER" if edge_pct > 0 else "UNDER"

    # Hard gate: skip if edge too small
    if abs(edge_pct) < MIN_EDGE_PCT:
        return {
            "projected":   projected_val,
            "line":        line,
            "edge_pct":    round(edge_pct, 4),
            "direction":   direction,
            "confidence":  None,
            "factors_pos": [],
            "factors_neg": [],
            "skip":        True,
            "reason":      f"Edge {abs(edge_pct):.1%} below 8% minimum",
        }

    # Confidence
    n_games    = stats.get("n_games", MIN_GAMES)
    confidence = _confidence_from_edge_and_sample(
        edge_pct, n_games, consistency, hit_rate)

    # Split factors into positive/negative
    factors_pos = [f for f in factors if f.startswith("✅") or
                   ("home" in f.lower() and "away" not in f.lower()) or
                   "hot" in f.lower()]
    factors_neg = [f for f in factors if f.startswith("⚠️") or "cold" in f.lower()]
    factors_neu = [f for f in factors if f not in factors_pos and f not in factors_neg]

    return {
        "projected":    projected_val,
        "line":         line,
        "edge_pct":     round(edge_pct, 4),
        "edge_pct_pct": int(abs(edge_pct) * 100),
        "direction":    direction,
        "confidence":   round(confidence, 1),
        "conf_int":     int(confidence),
        "factors_pos":  factors_pos,
        "factors_neg":  factors_neg,
        "factors_neu":  factors_neu,
        "skip":         False,
        "reason":       None,
        "recent_values": stats.get("recent_values", [])[:6],
        "n_games":      n_games,
        "hit_rate":     round(hit_rate, 3),
        "consistency":  round(consistency, 3),
        "avg":          stats.get("avg", 0),
    }


def format_projection_output(player: str, stat_type: str, proj: dict) -> str:
    """
    Human-readable projection summary (used in notifications and logs).
    Matches the format ChatGPT recommended.
    """
    if proj.get("skip"):
        return f"{player} — SKIP: {proj.get('reason','')}"

    direction = proj["direction"]
    arrow     = "📈 OVER" if direction == "OVER" else "📉 UNDER"
    edge_sign = "+" if direction == "OVER" else "-"

    lines = [
        f"{'🏀' if 'Pts' in stat_type or 'Reb' in stat_type else '⚾'} {player}",
        f"Line:       {proj['line']}",
        f"Projection: {proj['projected']} ({arrow})",
        f"Edge:       {edge_sign}{proj['edge_pct_pct']}%",
        f"Confidence: {proj['conf_int']}%",
        f"Hit rate:   {proj['n_games']} games, {int(proj['hit_rate']*100)}% hit",
        f"L5:         {proj['recent_values'][:5]}",
    ]
    if proj["factors_pos"]:
        lines.append("Positives:")
        lines.extend(f"  {f}" for f in proj["factors_pos"])
    if proj["factors_neg"]:
        lines.append("Negatives:")
        lines.extend(f"  {f}" for f in proj["factors_neg"])
    if proj["factors_neu"]:
        lines.extend(f"  {f}" for f in proj["factors_neu"][:3])
    lines.append(f"{'→ RECOMMENDATION: ' + direction + ' ' + str(proj['line']) + ' ' + stat_type}")

    return "\n".join(lines)
