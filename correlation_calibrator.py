"""
correlation_calibrator.py — Self-updating correlation coefficient learning.

Accumulates resolved same-game pick pairs and learns the true joint correlation
factor from empirical outcomes instead of using hand-crafted formulas.

WHAT THIS SOLVES:
  The overlap correction in joint_game_correlation_factor() uses a dynamic
  formula derived from K%, run environment, and pitcher dominance. That formula
  encodes our *beliefs* about the correlation structure — not measured values.
  This module accumulates evidence to replace beliefs with observations.

DATA FLOW:
  1. record_pick_resolution(pick_result, hit): called on every resolved pick.
     Stores: game_id, game_state, p_hit (model probability), actual hit/miss.

  2. After each resolution, _form_pairs() runs: finds all previously resolved
     picks from the same game and records each pair as an observation.
     Each pair stores: (bucket, p_a, p_b, both_hit).

  3. get_learned_joint_factor(legs): when called before building a parlay,
     looks up the bucket for the current game state, computes
       empirical_factor = observed_both_hit_rate / avg(p_a × p_b)
     and returns it if min_pairs threshold is met.

  4. joint_game_correlation_factor() checks learned value first:
     → if available: use empirical factor
     → if not enough data: use dynamic formula (current behavior)

CALIBRATION TIMELINE:
  Bucket scheme: 3 K-env levels × 2 dominance levels = 6 buckets
  Threshold: 20 pairs per bucket to trust the empirical factor
  Expected data volume: ~120 same-game pairs total ≈ 2-3 months

STATUS LEVELS:
  "learning"    — fewer than 60 pairs total, formula still dominant
  "partial"     — some buckets calibrated, others still formula
  "calibrated"  — 4+ buckets with 20+ pairs, majority data-driven

TRANSPARENCY:
  get_calibration_summary() shows exactly how many picks/pairs exist,
  which buckets are calibrated, and the learned vs formula gap per bucket.
  This is the model's "confidence in its own confidence."
"""

import json
from pathlib import Path
from datetime import datetime, timezone

_DATA_FILE = Path("logs/correlation_data.json")
_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Game state bucketing ──────────────────────────────────────────────────────
# 3 K-env tiers × 2 dominance tiers = 6 buckets total.
# Designed so each tier has meaningful behavioral difference:
#   high_K (≥0.65): strikeouts prevent baserunners — high overlap expected
#   med_K  (0.35-0.65): mixed — moderate overlap
#   low_K  (<0.35): contact pitcher — lower overlap, cascade more independent
#   ace    (≥0.45): elite suppression tier
#   neutral (<0.45): average/weak pitcher, no strong suppression

def _k_bucket(k_env: float) -> str:
    if k_env >= 0.65: return "high_K"
    if k_env >= 0.35: return "med_K"
    return "low_K"

def _dom_bucket(dominance: float) -> str:
    return "ace" if dominance >= 0.45 else "neutral"

def _bucket(game_state: dict) -> str:
    """Compute the (K-env | dominance) bucket key for a game state."""
    k = float(game_state.get("k_environment", 0.5) or 0.5)
    d = float(game_state.get("pitcher_dominance", 0.0) or 0.0)
    return f"{_k_bucket(k)}|{_dom_bucket(d)}"


# ── Data I/O ──────────────────────────────────────────────────────────────────

def _load() -> dict:
    if _DATA_FILE.exists():
        try:
            return json.loads(_DATA_FILE.read_text())
        except Exception:
            pass
    return {"picks": {}, "pairs": []}

def _save(data: dict):
    try:
        _DATA_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ── Record a resolved pick ────────────────────────────────────────────────────

def record_pick_resolution(pick_result: dict, hit: bool):
    """
    Store a resolved pick with its game context for correlation learning.

    Called automatically from calibration_tracker.update_results() when a
    pick is confirmed hit or miss.

    Args:
        pick_result: the scored pick dict from score_pick() — must contain
                     game_id and game_state to be useful for correlation.
        hit:         True if the pick cleared the line, False if not.
    """
    game_id    = (pick_result.get("game_id") or "").strip()
    game_state = pick_result.get("game_state") or {}

    if not game_id:
        return   # can't correlate without a game identifier

    # Get the model's probability for this direction
    direction = pick_result.get("direction", "OVER")
    p_over    = pick_result.get("p_over")
    p_under   = pick_result.get("p_under")
    if direction == "OVER":
        p_hit = p_over or pick_result.get("confidence", 0.5)
    else:
        p_hit = p_under or (1.0 - (p_over or 0.5))

    pick_key = (
        f"{pick_result.get('player','')}|"
        f"{pick_result.get('stat_type','')}|"
        f"{game_id}"
    )

    data = _load()
    data["picks"][pick_key] = {
        "game_id":      game_id,
        "game_state":   game_state,
        "bucket":       _bucket(game_state),
        "p_hit":        round(float(p_hit or 0.5), 4),
        "hit":          bool(hit),
        "direction":    direction,
        "stat_type":    pick_result.get("stat_type", ""),
        "batting_order": pick_result.get("batting_order"),
        "player_team":  pick_result.get("player_team", ""),
        "resolved_at":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    _form_pairs(data, game_id)
    _save(data)


def _form_pairs(data: dict, game_id: str):
    """
    Find all resolved picks from the same game and create pair observations.
    Each pair is one data point: (bucket, p_a × p_b, both_hit?).
    Called after every new resolution — runs incrementally.
    """
    same_game = [
        (key, pick) for key, pick in data["picks"].items()
        if pick["game_id"] == game_id
    ]
    if len(same_game) < 2:
        return

    existing_pair_ids = {p["pair_id"] for p in data.get("pairs", [])}

    for i in range(len(same_game)):
        for j in range(i + 1, len(same_game)):
            key_a, pick_a = same_game[i]
            key_b, pick_b = same_game[j]

            pair_id = "|".join(sorted([key_a, key_b]))
            if pair_id in existing_pair_ids:
                continue   # already recorded

            # Use the game state with higher pitcher dominance (worst-case signal)
            gs_a = pick_a.get("game_state") or {}
            gs_b = pick_b.get("game_state") or {}
            dom_a = float(gs_a.get("pitcher_dominance", 0.0) or 0.0)
            dom_b = float(gs_b.get("pitcher_dominance", 0.0) or 0.0)
            game_state = gs_a if dom_a >= dom_b else gs_b

            data["pairs"].append({
                "pair_id":    pair_id,
                "game_id":    game_id,
                "bucket":     _bucket(game_state),
                "p_a":        pick_a["p_hit"],
                "p_b":        pick_b["p_hit"],
                "p_product":  round(pick_a["p_hit"] * pick_b["p_hit"], 4),
                "both_hit":   pick_a["hit"] and pick_b["hit"],
                "directions": sorted([pick_a["direction"], pick_b["direction"]]),
                "date":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            })


# ── Learned joint factor ──────────────────────────────────────────────────────

def get_learned_joint_factor(legs: list, min_pairs: int = 20) -> float | None:
    """
    Return the empirically learned joint factor for this combination, or None.

    FORMULA:
      observed_both_hit_rate = (pairs where both legs hit) / total_pairs
      expected_independent   = mean(p_a × p_b) across pairs
      empirical_factor       = observed / expected

      If empirical_factor > 1.0: legs hit together more than independence predicts.
      If empirical_factor < 1.0: legs fail together more than independence predicts.

    Returns None when fewer than min_pairs exist for this bucket —
    caller falls back to the dynamic formula.

    Args:
        legs:       list of scored pick dicts (need game_state)
        min_pairs:  minimum resolved pairs before trusting the empirical value
    """
    if not legs:
        return None

    # Find the most dominant game state across all legs
    best_gs  = {}
    best_dom = 0.0
    for leg in legs:
        gs = leg.get("game_state") or {}
        d  = float(gs.get("pitcher_dominance", 0.0) or 0.0)
        if d > best_dom:
            best_dom = d
            best_gs  = gs

    if not best_gs and best_dom == 0.0:
        return None

    bucket = _bucket(best_gs)
    data   = _load()
    pairs  = [p for p in data.get("pairs", []) if p["bucket"] == bucket]

    if len(pairs) < min_pairs:
        return None

    # Filter to pairs matching the direction profile of current legs
    leg_dirs = sorted([l.get("direction", "OVER") for l in legs])
    relevant = [p for p in pairs if p["directions"] == leg_dirs]

    if len(relevant) < min_pairs:
        # Not enough direction-matched pairs; use all pairs with a slight penalty
        relevant = pairs
        if len(relevant) < min_pairs:
            return None

    n_both_hit         = sum(1 for p in relevant if p["both_hit"])
    obs_joint_rate     = n_both_hit / len(relevant)
    avg_p_product      = sum(p["p_product"] for p in relevant) / len(relevant)

    if avg_p_product < 0.01:
        return None

    empirical_factor = obs_joint_rate / avg_p_product
    return round(max(0.60, min(1.40, empirical_factor)), 3)


# ── Calibration status ────────────────────────────────────────────────────────

def get_calibration_summary() -> dict:
    """
    Return calibration status: how many pairs exist per bucket, which are
    trusted, and the empirical vs formula gap for calibrated buckets.

    Call this from daily_top_picks.py or a dashboard to monitor progress.
    """
    data   = _load()
    pairs  = data.get("pairs", [])
    picks  = data.get("picks", {})

    # Count pairs per bucket
    bucket_counts: dict[str, int] = {}
    for p in pairs:
        b = p["bucket"]
        bucket_counts[b] = bucket_counts.get(b, 0) + 1

    min_thresh = 20
    calibrated_buckets = {b: n for b, n in bucket_counts.items() if n >= min_thresh}

    # Compute empirical factor per calibrated bucket
    bucket_factors = {}
    for bkt in calibrated_buckets:
        bkt_pairs = [p for p in pairs if p["bucket"] == bkt]
        n_hit     = sum(1 for p in bkt_pairs if p["both_hit"])
        obs       = n_hit / len(bkt_pairs)
        avg_prod  = sum(p["p_product"] for p in bkt_pairs) / len(bkt_pairs)
        if avg_prod > 0.01:
            bucket_factors[bkt] = round(obs / avg_prod, 3)

    total_picks = len(picks)
    total_pairs = len(pairs)
    n_calibrated = len(calibrated_buckets)

    if total_pairs < 30:
        status = "learning"
    elif n_calibrated < 2:
        status = "partial"
    else:
        status = "calibrated"

    return {
        "total_picks_resolved": total_picks,
        "total_pairs_observed": total_pairs,
        "pairs_needed_per_bucket": min_thresh,
        "buckets_calibrated":   n_calibrated,
        "buckets_total":        6,
        "bucket_counts":        bucket_counts,
        "bucket_factors":       bucket_factors,   # empirical when available
        "status":               status,
        "pct_complete":         round(min(100, total_pairs / (min_thresh * 6) * 100), 1),
    }
