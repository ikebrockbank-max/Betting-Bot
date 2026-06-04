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
     Each pair stores: (bucket, p_a, p_b, hit_a, hit_b, both_hit).

  3. get_learned_joint_factor(legs): when called before building a parlay,
     looks up the bucket for the current game state, computes a
     BIAS-CORRECTED empirical factor using empirical marginal rates as the
     independence baseline (not raw model probabilities). Applies shrinkage
     toward independence (1.0) to stabilize early-N estimates.
     Returns it if min_pairs threshold is met.

  4. joint_game_correlation_factor() checks learned value first:
     → if available: use empirical factor
     → if not enough data: use dynamic formula (current behavior)

BIAS CORRECTION (critical):
  Naive formula:  empirical_factor = obs_joint_rate / mean(p_a × p_b)
  Problem:        p_a and p_b are model outputs — if the model is
                  overconfident (predicts 65%, actual hit rate is 58%),
                  the denominator is inflated and the factor absorbs
                  projection bias as if it were negative correlation.
  Fix:            empirical_factor = obs_joint_rate / (obs_rate_a × obs_rate_b)
                  where obs_rate_a/b are empirical marginal hit rates.
                  This measures only *dependence*, not bias.

SHRINKAGE (early-N stability):
  At N=20 pairs, variance is enormous. A 50/50 blend toward independence
  (1.0) stabilizes early estimates. Shrinkage fades as N grows:
    α = min_pairs / (N + min_pairs)        → 0.50 at N=20
    stable_factor = 1.0 × α + raw × (1-α)  → 0.25 at N=60, 0.17 at N=100

CALIBRATION TIMELINE:
  Bucket scheme: 3 K-env levels × 2 dominance levels = 6 buckets
  Threshold: 20 pairs per bucket to trust the empirical factor
  Expected data volume: ~120 same-game pairs total ≈ 2-3 months

STATUS LEVELS:
  "learning"    — fewer than 60 pairs total, formula still dominant
  "partial"     — some buckets calibrated, others still formula
  "calibrated"  — 4+ buckets with 20+ pairs, majority data-driven

TRANSPARENCY:
  get_calibration_summary()  — how many pairs, which buckets calibrated
  get_projection_bias()      — model calibration: predicted vs actual hit rate
                               per bucket, showing whether model is over/under
                               confident before the correlation correction.
"""

import json
import math
from pathlib import Path
from datetime import datetime, date, timezone

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

            # Extract continuous game state values for kernel regression
            # (stored alongside discrete bucket for backward compat)
            _gs_cont = {
                "dominance": round(float(game_state.get("pitcher_dominance", 0.0) or 0.0), 4),
                "k_env":     round(float(game_state.get("k_environment",    0.5) or 0.5), 4),
                "run_env":   round(float(game_state.get("run_environment",  0.5) or 0.5), 4),
            }

            data["pairs"].append({
                "pair_id":    pair_id,
                "game_id":    game_id,
                "bucket":     _bucket(game_state),
                # Continuous env values for kernel regression (see get_env_conditioned_joint_factor)
                "dominance":  _gs_cont["dominance"],
                "k_env":      _gs_cont["k_env"],
                "run_env":    _gs_cont["run_env"],
                "p_a":        pick_a["p_hit"],
                "p_b":        pick_b["p_hit"],
                "p_product":  round(pick_a["p_hit"] * pick_b["p_hit"], 4),
                "hit_a":      bool(pick_a["hit"]),   # individual outcomes for bias correction
                "hit_b":      bool(pick_b["hit"]),
                "both_hit":   pick_a["hit"] and pick_b["hit"],
                "directions": sorted([pick_a["direction"], pick_b["direction"]]),
                "date":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            })


# ── Learned joint factor ──────────────────────────────────────────────────────

def get_learned_joint_factor(legs: list, min_pairs: int = 20) -> float | None:
    """
    Return the empirically learned joint factor for this combination, or None.

    BIAS-CORRECTED FORMULA:
      Problem with naive approach (obs_joint / mean(p_a × p_b)):
        If the model overestimates hit probability (e.g. predicts 65% but
        legs actually hit 58%), the denominator is inflated — the factor
        learns a downward correction that looks like negative correlation
        but is actually just projection bias.

      Fix — use empirical marginal hit rates as the independence baseline:
        obs_rate_a     = actual hit rate for "a" picks in this bucket
        obs_rate_b     = actual hit rate for "b" picks in this bucket
        independence   = obs_rate_a × obs_rate_b
        raw_factor     = obs_joint_rate / independence

      This measures only *dependence*, not bias. get_projection_bias()
      separately surfaces the model's over/under-confidence.

    SHRINKAGE:
      Early N=20-40 pairs have enormous variance. Blend toward independence
      (1.0) using Bayesian shrinkage:
        α = min_pairs / (N + min_pairs)    # 0.50 at N=20, 0.25 at N=60
        stable = 1.0 × α + raw × (1-α)

    Backward compatible: pairs without hit_a/hit_b fields fall back to
    the naive p_product denominator with a conservative shrinkage penalty.

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
        # Not enough direction-matched pairs; use all pairs in bucket
        relevant = pairs
        if len(relevant) < min_pairs:
            return None

    n = len(relevant)
    obs_joint_rate = sum(1 for p in relevant if p["both_hit"]) / n

    # ── Bias-corrected independence baseline ──────────────────────────────────
    # Use empirical marginal hit rates if individual outcomes are stored
    # (new format: "hit_a" / "hit_b" fields present).
    # This ensures the factor measures only *dependence*, not projection bias.
    new_format_pairs = [p for p in relevant if "hit_a" in p and "hit_b" in p]

    if len(new_format_pairs) >= min_pairs:
        obs_rate_a = sum(p["hit_a"] for p in new_format_pairs) / len(new_format_pairs)
        obs_rate_b = sum(p["hit_b"] for p in new_format_pairs) / len(new_format_pairs)
        independence_baseline = max(0.01, obs_rate_a * obs_rate_b)
        raw_factor = obs_joint_rate / independence_baseline
        n_for_shrinkage = len(new_format_pairs)
    else:
        # Old-format pairs (no hit_a/hit_b): fall back to model p_product
        # as denominator. Apply extra conservatism by treating N as halved.
        avg_p_product = sum(p["p_product"] for p in relevant) / n
        if avg_p_product < 0.01:
            return None
        raw_factor = obs_joint_rate / avg_p_product
        n_for_shrinkage = n // 2   # conservative: treat old-format data as half weight

    # ── Shrinkage toward independence (1.0) ───────────────────────────────────
    # Stabilizes early estimates. At N=20: 50% prior, 50% data.
    # Prior = 1.0 (assume independence until proven otherwise).
    alpha = min_pairs / (n_for_shrinkage + min_pairs)
    stable_factor = 1.0 * alpha + raw_factor * (1.0 - alpha)

    return round(max(0.60, min(1.40, stable_factor)), 3)


# ── Kernel helpers ────────────────────────────────────────────────────────────

def _env_similarity(
    d_cur: float, k_cur: float, r_cur: float,
    d_hist: float, k_hist: float, r_hist: float,
    bandwidth: float = 0.18,
) -> float:
    """
    Gaussian kernel: how similar is a historical pair's game environment to now?

    Dimension weights:
      pitcher_dominance ×2.0 — strongest environmental factor (ace vs weak starter
        is a completely different game state; should shrink weight quickly)
      k_environment     ×1.5 — directly affects baserunner chains
      run_environment   ×1.0 — park/total context, less acute

    Returns 0..1. Two identical game states → 1.0. Wheeler (D=0.53) vs avg ace
    (D=0.46) → 0.86. Wheeler vs weak pitcher (D=0.00) → 0.03.
    """
    dist_sq = (
        2.0 * (d_cur - d_hist) ** 2 +
        1.5 * (k_cur - k_hist) ** 2 +
        1.0 * (r_cur - r_hist) ** 2
    )
    return math.exp(-dist_sq / (2.0 * bandwidth ** 2))


def _time_weight(pair_date_str: str, half_life_days: int = 60) -> float:
    """
    Exponential decay: pairs 60 days old get 50% weight, 120 days = 25%.

    Addresses the stationarity problem: pitcher form, lineup construction, and
    park effects drift over a season. More recent pairs reflect current reality
    more accurately than data from 3 months ago.
    """
    try:
        pair_dt = date.fromisoformat(pair_date_str)
        days_old = max(0, (date.today() - pair_dt).days)
        return math.exp(-math.log(2) * days_old / half_life_days)
    except Exception:
        return 0.5   # neutral weight when date is missing or malformed


# ── Environment-conditioned joint factor ─────────────────────────────────────

def get_env_conditioned_joint_factor(
    legs: list,
    min_effective_n: float = 10.0,
) -> float | None:
    """
    Environment-conditioned joint factor via kernel regression.

    This is the structurally correct form of the correlation correction:

      P(A,B | env) / (P(A | env) × P(B | env))

    Three explicit layers:
      1. Marginal model    — P(A), P(B): from the projection engine per pick
      2. Environment model — game_state: pitcher_dominance, k_env, run_env
      3. Dependency        — this function: how much do legs co-move *given* env?

    WHY KERNEL OVER DISCRETE BUCKETS:
      Both Wheeler (D=0.53, K=0.84) and an avg ace (D=0.46, K=0.66) land in
      "high_K|ace". But their kernel weight difference for a Wheeler-tier game
      is 0.86 vs 0.35 — the kernel naturally treats them as different.
      Discrete buckets collapse that distinction; kernel regression doesn't.

    WHY TEMPORAL DECAY:
      Addresses regime drift. Pair from July with a hot lineup is less relevant
      for a cold April game even if game state vectors are similar.

    HOW IT WORKS:
      For each historical pair with stored continuous game state (dominance,
      k_env, run_env), compute:
        weight = env_similarity(current, hist) × time_decay(pair_date)
      Then compute kernel-weighted hit rates:
        w_joint  = Σ(weight × both_hit) / Σ(weight)
        w_rate_a = Σ(weight × hit_a)    / Σ(weight)
        w_rate_b = Σ(weight × hit_b)    / Σ(weight)
      Raw factor = w_joint / (w_rate_a × w_rate_b)   ← bias-corrected

    EFFECTIVE SAMPLE SIZE (Kish):
      eff_n = (Σw)² / Σw²
      Shrinkage: α = min_eff_n / (eff_n + min_eff_n)
                 stable = 1.0 × α + raw × (1 − α)

    Requires pairs with "dominance", "k_env", "run_env", "hit_a", "hit_b".
    Gracefully returns None (callers fall back to bucket-learned, then formula).

    Args:
        legs:           list of scored pick dicts (need game_state)
        min_effective_n: minimum effective sample before returning a value
    """
    if not legs:
        return None

    # Get current game state — use the most dominant game across all legs
    best_gs  = {}
    best_dom = 0.0
    for leg in legs:
        gs = leg.get("game_state") or {}
        d  = float(gs.get("pitcher_dominance", 0.0) or 0.0)
        if d > best_dom:
            best_dom = d
            best_gs  = gs

    if not best_gs:
        return None

    d_cur = float(best_gs.get("pitcher_dominance", 0.0) or 0.0)
    k_cur = float(best_gs.get("k_environment",    0.5) or 0.5)
    r_cur = float(best_gs.get("run_environment",  0.5) or 0.5)

    data = _load()

    # Only usable pairs have continuous game state AND individual outcomes
    usable = [
        p for p in data.get("pairs", [])
        if "dominance" in p and "hit_a" in p and "hit_b" in p
    ]

    if len(usable) < 5:
        return None   # wait for a minimal corpus before kernel estimation

    # ── Per-pair weights ──────────────────────────────────────────────────────
    weights = []
    for p in usable:
        env_w  = _env_similarity(
            d_cur, k_cur, r_cur,
            float(p.get("dominance", 0.0) or 0.0),
            float(p.get("k_env",    0.5) or 0.5),
            float(p.get("run_env",  0.5) or 0.5),
        )
        time_w = _time_weight(p.get("date", ""))
        weights.append(env_w * time_w)

    total_w = sum(weights)
    if total_w < 0.01:
        return None

    # Kish effective sample size: penalises when few pairs dominate the weights
    sum_w2  = sum(w * w for w in weights)
    eff_n   = (total_w ** 2) / sum_w2 if sum_w2 > 0 else 0.0

    if eff_n < min_effective_n:
        return None   # not enough environmentally-relevant history yet

    # ── Kernel-weighted hit rates (bias-corrected baseline) ───────────────────
    w_joint  = sum(w * p["both_hit"] for w, p in zip(weights, usable)) / total_w
    w_rate_a = sum(w * p["hit_a"]    for w, p in zip(weights, usable)) / total_w
    w_rate_b = sum(w * p["hit_b"]    for w, p in zip(weights, usable)) / total_w

    independence_baseline = max(0.01, w_rate_a * w_rate_b)
    raw_factor = w_joint / independence_baseline

    # ── Shrinkage toward independence ─────────────────────────────────────────
    alpha         = min_effective_n / (eff_n + min_effective_n)
    stable_factor = 1.0 * alpha + raw_factor * (1.0 - alpha)

    return round(max(0.60, min(1.40, stable_factor)), 3)


# ── Projection bias ───────────────────────────────────────────────────────────

def get_projection_bias() -> dict:
    """
    Measure whether model probabilities are calibrated against actual outcomes.

    WHY THIS MATTERS:
      The correlation engine uses bias-corrected denominators (empirical
      marginal rates vs model predictions). But knowing the *size* of the bias
      is still useful — a model that's 10% overconfident on ace-pitcher games
      needs more data to stabilize than one that's 2% off.

    RETURNS per bucket (min 5 picks to compute):
      avg_predicted:  mean model p_hit across resolved picks
      avg_actual:     empirical hit rate (what actually happened)
      bias:           avg_predicted / avg_actual
                        > 1.0 → model overconfident (overestimates hit rate)
                        < 1.0 → model underconfident
                        = 1.0 → calibrated
      calibration:    "overconfident" | "underconfident" | "calibrated" (±5% band)

    Example: high_K|ace shows bias=1.18 → model predicts 64% but legs only
    hit 54% vs aces. Correlation factor correctly uses 54%, not 64%, as baseline.
    """
    data  = _load()
    picks = data.get("picks", {})

    bucket_data: dict = {}
    for pick in picks.values():
        b = pick.get("bucket", "unknown")
        if b not in bucket_data:
            bucket_data[b] = {"p_hits": [], "actuals": []}
        bucket_data[b]["p_hits"].append(float(pick.get("p_hit", 0.5) or 0.5))
        bucket_data[b]["actuals"].append(1.0 if pick.get("hit") else 0.0)

    result: dict = {}
    for b, d in bucket_data.items():
        n = len(d["p_hits"])
        if n < 5:
            continue
        avg_p      = sum(d["p_hits"]) / n
        avg_actual = sum(d["actuals"]) / n
        bias       = round(avg_p / max(0.01, avg_actual), 3)
        if avg_p > avg_actual + 0.05:
            cal = "overconfident"
        elif avg_p < avg_actual - 0.05:
            cal = "underconfident"
        else:
            cal = "calibrated"
        result[b] = {
            "n_picks":       n,
            "avg_predicted": round(avg_p, 3),
            "avg_actual":    round(avg_actual, 3),
            "bias":          bias,
            "calibration":   cal,
        }

    return result


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

    # Projection bias summary (how well model probs match actual hit rates)
    bias_summary = get_projection_bias()
    overconfident_buckets = [
        b for b, v in bias_summary.items() if v["calibration"] == "overconfident"
    ]

    # Kernel-ready pairs: have continuous game state + individual outcomes
    # (stored since this version — old pairs with only "both_hit" don't qualify)
    kernel_ready_pairs = sum(
        1 for p in pairs if "dominance" in p and "hit_a" in p and "hit_b" in p
    )

    return {
        "total_picks_resolved":   total_picks,
        "total_pairs_observed":   total_pairs,
        "pairs_needed_per_bucket": min_thresh,
        "buckets_calibrated":     n_calibrated,
        "buckets_total":          6,
        "bucket_counts":          bucket_counts,
        "bucket_factors":         bucket_factors,    # empirical when available
        "status":                 status,
        "pct_complete":           round(min(100, total_pairs / (min_thresh * 6) * 100), 1),
        # Kernel regression layer (most precise — conditioned on continuous env)
        "kernel_ready_pairs":     kernel_ready_pairs,
        "kernel_active_threshold": 5,    # pairs needed before kernel attempts estimate
        "kernel_active":          kernel_ready_pairs >= 5,
        # Bias correction transparency
        "projection_bias":        bias_summary,      # per-bucket predicted vs actual
        "overconfident_buckets":  overconfident_buckets,
        "bias_correction_active": any(                # True once we have enough picks
            v["n_picks"] >= 20 for v in bias_summary.values()
        ),
    }
