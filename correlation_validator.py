"""
correlation_validator.py — Tier comparison and self-calibration for the
correlation engine.

WHAT THIS ANSWERS:
  "Is the kernel actually predicting joint outcomes better than the bucket
  system or naive independence? Or is it adding noise?"

  That's the question ChatGPT flagged as the real bottleneck after the kernel
  architecture was complete:

    "The next meaningful leap is validating whether kernel similarity actually
     predicts joint outcome correlation better than your previous bucket system.
     That's the point where this stops being an architecture project and becomes
     a performance-validated edge system."

METHOD: Leave-one-out Brier scoring
  For each resolved pair in the corpus, temporarily remove it from training
  and ask: what would each tier predict for this pair? Score against actual
  both_hit outcome with Brier score (mean squared error of probabilities).

  Brier score = mean((predicted_P(both_hit) - actual_both_hit)²)
  Lower = better. Random = 0.25. Perfect = 0.

  Three tiers:
    - naive:  p_a × p_b  (pure independence assumption)
    - bucket: p_a × p_b × bucket_factor_leave_one_out (discrete bucket, bias-corrected)
    - kernel: p_a × p_b × kernel_factor_leave_one_out (continuous env, temporal decay)

SELECTION BIAS NOTE:
  Bucket and kernel only produce predictions when they have sufficient data
  (min_pairs=20 and min_eff_n=5 respectively). Their Brier scores are computed
  on different (non-identical) subsets of pairs — the kernel covers more pairs
  at lower data volumes because it uses ALL pairs weighted by similarity, not
  only exact-bucket matches. This means early comparisons are not apples-to-apples.
  validate_tiers() returns n_scored per tier so you can see the coverage gap.

PUBLIC API:
  validate_tiers()      — main comparison: Brier scores per tier, winner
  optimize_bandwidth()  — tune kernel bandwidth parameter (run at 30+ pairs)
  calibrate_min_eff_n() — tune eff_n threshold (run at 30+ pairs)
  run_cli()             — print full report; called by __main__

USAGE:
  python correlation_validator.py           # full report
  python correlation_validator.py --bw      # bandwidth tuning only
  python correlation_validator.py --eff-n   # min_eff_n tuning only
"""

import sys
import math
from datetime import date

# ── Import correlation infrastructure ─────────────────────────────────────────
# Private functions are intentionally imported — this module is the measurement
# layer for correlation_calibrator, not a new parallel implementation.
from correlation_calibrator import (
    _load,
    _env_similarity,
    _time_weight,
)

_DEFAULT_BANDWIDTH  = 0.18
_DEFAULT_MIN_EFF_N  = 10.0
_DEFAULT_MIN_BUCKET = 20


# ── Leave-one-out tier predictors ─────────────────────────────────────────────

def _kernel_factor_loo(
    target_pair: dict,
    all_pairs: list,
    bandwidth: float = _DEFAULT_BANDWIDTH,
    min_eff_n: float = _DEFAULT_MIN_EFF_N,
) -> float | None:
    """
    Kernel joint factor for target_pair trained on all OTHER pairs.

    Leave-one-out prevents the target from being its own training data,
    which would artificially inflate kernel performance.
    """
    others = [
        p for p in all_pairs
        if p["pair_id"] != target_pair["pair_id"]
        and "dominance" in p
        and "hit_a" in p
        and "hit_b" in p
    ]
    if len(others) < 3:
        return None

    d_cur = float(target_pair.get("dominance", 0.0) or 0.0)
    k_cur = float(target_pair.get("k_env",    0.5) or 0.5)
    r_cur = float(target_pair.get("run_env",  0.5) or 0.5)

    weights = []
    for p in others:
        env_w  = _env_similarity(
            d_cur, k_cur, r_cur,
            float(p.get("dominance", 0.0) or 0.0),
            float(p.get("k_env",    0.5) or 0.5),
            float(p.get("run_env",  0.5) or 0.5),
            bandwidth=bandwidth,
        )
        time_w = _time_weight(p.get("date", ""))
        weights.append(env_w * time_w)

    total_w = sum(weights)
    if total_w < 0.01:
        return None

    sum_w2 = sum(w * w for w in weights)
    eff_n  = (total_w ** 2) / sum_w2 if sum_w2 > 0 else 0.0
    if eff_n < min_eff_n:
        return None

    w_joint  = sum(w * p["both_hit"] for w, p in zip(weights, others)) / total_w
    w_rate_a = sum(w * p["hit_a"]    for w, p in zip(weights, others)) / total_w
    w_rate_b = sum(w * p["hit_b"]    for w, p in zip(weights, others)) / total_w

    baseline = max(0.01, w_rate_a * w_rate_b)
    raw      = w_joint / baseline

    alpha  = min_eff_n / (eff_n + min_eff_n)
    factor = 1.0 * alpha + raw * (1.0 - alpha)
    return max(0.60, min(1.40, factor))


def _bucket_factor_loo(
    target_pair: dict,
    all_pairs: list,
    min_pairs: int = _DEFAULT_MIN_BUCKET,
) -> float | None:
    """
    Bucket joint factor for target_pair trained on other pairs in same bucket.

    Uses bias-corrected denominator (empirical marginals) + shrinkage.
    """
    bucket = target_pair.get("bucket")
    others = [
        p for p in all_pairs
        if p["pair_id"] != target_pair["pair_id"]
        and p.get("bucket") == bucket
        and "hit_a" in p
        and "hit_b" in p
    ]
    if len(others) < min_pairs:
        return None

    obs_rate_a = sum(p["hit_a"] for p in others) / len(others)
    obs_rate_b = sum(p["hit_b"] for p in others) / len(others)
    obs_joint  = sum(1 for p in others if p["both_hit"]) / len(others)

    baseline = max(0.01, obs_rate_a * obs_rate_b)
    raw      = obs_joint / baseline

    alpha  = min_pairs / (len(others) + min_pairs)
    factor = 1.0 * alpha + raw * (1.0 - alpha)
    return max(0.60, min(1.40, factor))


# ── Main validation ───────────────────────────────────────────────────────────

def validate_tiers(min_usable_pairs: int = 10) -> dict:
    """
    Leave-one-out Brier score comparison: kernel vs bucket vs naive.

    For each pair with full data (dominance/k_env/run_env/hit_a/hit_b),
    computes predicted joint P(both_hit) from each tier using all other
    pairs as training, then scores against actual outcome.

    Brier score = mean((predicted - actual)²)
    Lower = better. Independence baseline (naive) = something like 0.20-0.25.

    Returns:
        status:                    "ok" | "insufficient_data"
        n_pairs_usable:            pairs with full data for kernel/bucket
        n_pairs_scored_*:          how many pairs each tier predicted (coverage)
        brier_*:                   per-tier Brier score
        winner:                    which tier has lowest Brier
        *_vs_naive_improvement_pct: % Brier reduction vs naive (positive = better)
        interpretation:            plain-text reading of results
    """
    data       = _load()
    all_pairs  = data.get("pairs", [])
    usable     = [
        p for p in all_pairs
        if "dominance" in p and "hit_a" in p and "hit_b" in p
    ]

    if len(usable) < min_usable_pairs:
        return {
            "status":           "insufficient_data",
            "n_pairs_total":    len(all_pairs),
            "n_pairs_usable":   len(usable),
            "min_required":     min_usable_pairs,
            "message":          (
                f"Need {min_usable_pairs} usable pairs; have {len(usable)}. "
                "Run again after more picks resolve."
            ),
        }

    errors_naive  = []
    errors_bucket = []
    errors_kernel = []

    for pair in usable:
        actual  = 1.0 if pair["both_hit"] else 0.0
        p_naive = pair["p_a"] * pair["p_b"]

        # Naive: pure independence
        errors_naive.append((p_naive - actual) ** 2)

        # Bucket: leave-one-out empirical factor
        bf = _bucket_factor_loo(pair, all_pairs)
        if bf is not None:
            p_bucket = min(0.99, max(0.01, p_naive * bf))
            errors_bucket.append((p_bucket - actual) ** 2)

        # Kernel: leave-one-out env-conditioned factor
        kf = _kernel_factor_loo(pair, all_pairs)
        if kf is not None:
            p_kernel = min(0.99, max(0.01, p_naive * kf))
            errors_kernel.append((p_kernel - actual) ** 2)

    brier_naive  = sum(errors_naive)  / len(errors_naive)  if errors_naive  else None
    brier_bucket = sum(errors_bucket) / len(errors_bucket) if errors_bucket else None
    brier_kernel = sum(errors_kernel) / len(errors_kernel) if errors_kernel else None

    # Winner among tiers that actually made predictions
    scored = {
        "naive":  brier_naive,
        "bucket": brier_bucket,
        "kernel": brier_kernel,
    }
    available = {k: v for k, v in scored.items() if v is not None}
    winner = min(available, key=available.get) if available else "none"

    def pct_improvement(challenger, baseline):
        if challenger is None or baseline is None or baseline == 0:
            return None
        return round((baseline - challenger) / baseline * 100, 1)

    return {
        "status":                           "ok",
        "n_pairs_total":                    len(all_pairs),
        "n_pairs_usable":                   len(usable),
        "n_pairs_scored_naive":             len(errors_naive),
        "n_pairs_scored_bucket":            len(errors_bucket),
        "n_pairs_scored_kernel":            len(errors_kernel),
        "brier_naive":                      round(brier_naive,  4) if brier_naive  else None,
        "brier_bucket":                     round(brier_bucket, 4) if brier_bucket else None,
        "brier_kernel":                     round(brier_kernel, 4) if brier_kernel else None,
        "winner":                           winner,
        "kernel_vs_naive_improvement_pct":  pct_improvement(brier_kernel, brier_naive),
        "bucket_vs_naive_improvement_pct":  pct_improvement(brier_bucket, brier_naive),
        "kernel_vs_bucket_improvement_pct": pct_improvement(brier_kernel, brier_bucket),
        "interpretation":                   _interpret(winner, brier_kernel, brier_naive, len(usable)),
        # Selection bias note — bucket/kernel scored on different subsets
        "_selection_bias_note": (
            "brier_bucket and brier_kernel are computed on different pair subsets "
            "(only pairs where that tier made a prediction). Direct comparison is only "
            "valid when n_scored is similar across tiers."
        ),
    }


def _interpret(winner: str, brier_kernel, brier_naive, n: int) -> str:
    if winner == "none":
        return "No tier produced predictions yet. Collect more resolved picks."
    if n < 20:
        return (
            f"Early stage ({n} usable pairs) — results are statistically noisy. "
            "Winner may flip with more data. Check again at 30+ pairs."
        )
    if winner == "naive":
        return (
            "Independence baseline is winning. Kernel/bucket may be adding noise. "
            "Consider widening kernel bandwidth or waiting for more data."
        )
    if winner == "kernel":
        pct = round((brier_naive - brier_kernel) / brier_naive * 100, 1) if brier_naive else "?"
        return (
            f"Kernel outperforms naive by {pct}%. "
            "Game state similarity is a real predictor of joint outcomes. "
            "The correlation structure exists and is learnable."
        )
    if winner == "bucket":
        return (
            "Bucket system is outperforming kernel. "
            "Possible causes: kernel bandwidth too narrow (over-fitting) or "
            "not enough data for effective kernel coverage. "
            "Run optimize_bandwidth() to check."
        )
    return f"{winner.capitalize()} tier winning."


# ── Bandwidth tuning ──────────────────────────────────────────────────────────

def optimize_bandwidth(
    bandwidths: list | None = None,
    min_pairs: int = 20,
) -> dict:
    """
    Find the kernel bandwidth that minimizes leave-one-out Brier score.

    The bandwidth controls how broadly the kernel interpolates — narrower
    means only very similar games inform the factor, wider means more games
    contribute but with less precision.

    Call this once you have 30+ usable pairs. Re-run monthly.
    Current default: 0.18. If recommended_bandwidth differs significantly,
    update _DEFAULT_BANDWIDTH in both this file and correlation_calibrator.py.

    Args:
        bandwidths: list of bandwidth values to test. Default covers a wide range.
        min_pairs:  minimum usable pairs to attempt tuning.
    """
    if bandwidths is None:
        bandwidths = [0.06, 0.10, 0.14, 0.18, 0.22, 0.28, 0.38, 0.50]

    data      = _load()
    all_pairs = data.get("pairs", [])
    usable    = [p for p in all_pairs if "dominance" in p and "hit_a" in p and "hit_b" in p]

    if len(usable) < min_pairs:
        return {
            "status":         "insufficient_data",
            "n_usable_pairs": len(usable),
            "min_required":   min_pairs,
            "message":        f"Need {min_pairs} usable pairs for bandwidth tuning.",
        }

    brier_per_bw: dict = {}
    coverage_per_bw: dict = {}
    for bw in bandwidths:
        errors = []
        for pair in usable:
            actual  = 1.0 if pair["both_hit"] else 0.0
            p_naive = pair["p_a"] * pair["p_b"]
            kf = _kernel_factor_loo(pair, all_pairs, bandwidth=bw)
            if kf is not None:
                p_pred = min(0.99, max(0.01, p_naive * kf))
                errors.append((p_pred - actual) ** 2)
        if errors:
            brier_per_bw[bw]    = round(sum(errors) / len(errors), 5)
            coverage_per_bw[bw] = round(len(errors) / len(usable) * 100, 1)

    if not brier_per_bw:
        return {"status": "no_predictions", "bandwidths_tested": bandwidths}

    best_bw = min(brier_per_bw, key=brier_per_bw.get)
    current_brier = brier_per_bw.get(_DEFAULT_BANDWIDTH)
    best_brier    = brier_per_bw[best_bw]

    return {
        "n_usable_pairs":      len(usable),
        "current_bandwidth":   _DEFAULT_BANDWIDTH,
        "recommended_bandwidth": best_bw,
        "current_brier":       current_brier,
        "best_brier":          best_brier,
        "improvement_pct":     round(
            (current_brier - best_brier) / max(0.0001, current_brier) * 100, 1
        ) if current_brier else None,
        "brier_per_bandwidth": brier_per_bw,
        "coverage_pct":        coverage_per_bw,
        "recommendation": (
            f"Update bandwidth to {best_bw} in correlation_calibrator.py "
            f"if improvement_pct > 5%."
            if best_bw != _DEFAULT_BANDWIDTH
            else "Current bandwidth is optimal."
        ),
    }


# ── min_effective_n tuning ────────────────────────────────────────────────────

def calibrate_min_eff_n(
    candidates: list | None = None,
    min_pairs: int = 15,
    min_coverage_pct: float = 20.0,
) -> dict:
    """
    Find the min_effective_n threshold where kernel predictions are reliable.

    Higher min_eff_n = more conservative (fewer predictions but more stable).
    Lower min_eff_n = more coverage but early predictions may be noisy.

    Picks the threshold with lowest Brier score among those with ≥ min_coverage_pct
    of pairs scored (avoid thresholds so tight they barely predict anything).

    Call this once you have 30+ usable pairs. Current default: 10.0.
    """
    if candidates is None:
        candidates = [2.0, 3.0, 5.0, 8.0, 10.0, 15.0, 20.0]

    data      = _load()
    all_pairs = data.get("pairs", [])
    usable    = [p for p in all_pairs if "dominance" in p and "hit_a" in p and "hit_b" in p]

    if len(usable) < min_pairs:
        return {
            "status":       "insufficient_data",
            "n_usable":     len(usable),
            "min_required": min_pairs,
        }

    results: dict = {}
    for min_n in candidates:
        errors   = []
        n_skip   = 0
        for pair in usable:
            actual  = 1.0 if pair["both_hit"] else 0.0
            p_naive = pair["p_a"] * pair["p_b"]
            kf = _kernel_factor_loo(pair, all_pairs, min_eff_n=min_n)
            if kf is not None:
                p_pred = min(0.99, max(0.01, p_naive * kf))
                errors.append((p_pred - actual) ** 2)
            else:
                n_skip += 1
        if errors:
            coverage = len(errors) / len(usable) * 100
            results[min_n] = {
                "brier":      round(sum(errors) / len(errors), 5),
                "coverage":   round(coverage, 1),
                "n_scored":   len(errors),
                "n_skipped":  n_skip,
            }

    if not results:
        return {"status": "no_predictions"}

    # Best = lowest Brier with acceptable coverage
    viable = {n: v for n, v in results.items() if v["coverage"] >= min_coverage_pct}
    if viable:
        best_n = min(viable, key=lambda n: viable[n]["brier"])
    else:
        best_n = min(results, key=lambda n: results[n]["brier"])

    return {
        "n_usable_pairs":       len(usable),
        "current_min_eff_n":    _DEFAULT_MIN_EFF_N,
        "recommended_min_eff_n": best_n,
        "results_per_threshold": results,
        "note": (
            "Update get_env_conditioned_joint_factor(min_effective_n=...) in "
            "correlation_calibrator.py if recommended differs from current."
        ),
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

def run_cli(args: list | None = None) -> None:
    """Print a full validation report to stdout."""
    if args is None:
        args = sys.argv[1:]

    run_bw   = "--bw"    in args or "--bandwidth" in args or not args
    run_eff  = "--eff-n" in args or not args
    run_main = "--tiers" in args or not args

    print("\n" + "=" * 60)
    print("  CORRELATION VALIDATION REPORT")
    print(f"  {date.today().isoformat()}")
    print("=" * 60)

    data    = _load()
    n_total = len(data.get("pairs", []))
    n_usable = sum(
        1 for p in data.get("pairs", [])
        if "dominance" in p and "hit_a" in p and "hit_b" in p
    )
    print(f"\nCorpus: {n_total} total pairs, {n_usable} kernel-usable\n")

    if run_main:
        print("── TIER COMPARISON (Leave-one-out Brier Score) ──")
        r = validate_tiers()
        if r.get("status") == "insufficient_data":
            print(f"  ⏳ {r['message']}")
        else:
            print(f"  Pairs scored: naive={r['n_pairs_scored_naive']}, "
                  f"bucket={r['n_pairs_scored_bucket']}, "
                  f"kernel={r['n_pairs_scored_kernel']}")
            print(f"  Brier naive:  {r['brier_naive']}")
            print(f"  Brier bucket: {r['brier_bucket']}  "
                  f"(vs naive: {r['bucket_vs_naive_improvement_pct']}%)")
            print(f"  Brier kernel: {r['brier_kernel']}  "
                  f"(vs naive: {r['kernel_vs_naive_improvement_pct']}%)")
            print(f"  Winner: {r['winner'].upper()}")
            print(f"\n  → {r['interpretation']}")
        print()

    if run_bw:
        print("── BANDWIDTH OPTIMIZATION ──")
        r = optimize_bandwidth()
        if r.get("status") == "insufficient_data":
            print(f"  ⏳ {r['message']}")
        else:
            print(f"  Current: {r['current_bandwidth']} (Brier={r['current_brier']})")
            print(f"  Best:    {r['recommended_bandwidth']} (Brier={r['best_brier']})")
            print(f"  Improvement: {r['improvement_pct']}%")
            print(f"  Brier by bandwidth: {r['brier_per_bandwidth']}")
            print(f"\n  → {r['recommendation']}")
        print()

    if run_eff:
        print("── MIN_EFF_N CALIBRATION ──")
        r = calibrate_min_eff_n()
        if r.get("status") == "insufficient_data":
            print(f"  ⏳ Need {r['min_required']} pairs; have {r['n_usable']}.")
        else:
            print(f"  Current min_eff_n: {r['current_min_eff_n']}")
            print(f"  Recommended:       {r['recommended_min_eff_n']}")
            for n, v in r["results_per_threshold"].items():
                print(f"    min_eff_n={n:5.1f}: "
                      f"Brier={v['brier']}, coverage={v['coverage']}%")
            print(f"\n  → {r['note']}")
        print()

    print("=" * 60 + "\n")


if __name__ == "__main__":
    run_cli()
