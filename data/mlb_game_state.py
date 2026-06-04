"""
mlb_game_state.py — Game-level latent state vector for MLB.

KEY INSIGHT: MLB player outcomes within the same game are NOT independent.
When Wheeler dominates, ALL opposing batters underperform together — it's one
shared game-state event, not three independent 30% misses.

This module computes a shared game state once per game and exposes two things:

  1. compute_game_state() — game state dict for annotation and debugging
  2. correlation_factor_same_game() — joint probability multiplier for parlays

CORRELATION MATH:
  Naïve parlay model:   P(A ∩ B ∩ C) = P(A) × P(B) × P(C)
  Correlated reality:   P(A ∩ B ∩ C) = P(A) × P(B) × P(C) × corr_factor

  OVER stack vs ace pitcher:   corr_factor < 1.0  (legs fail together)
  UNDER stack vs ace pitcher:  corr_factor > 1.0  (legs succeed together)
  Cross-game legs:             corr_factor = 1.0  (genuinely independent)

Game state components:
  pitcher_dominance  0–1: 0 = weak starter, 1 = elite ace lockdown
  run_environment    0–1: 0 = pitcher park / low total, 1 = hitter park / slugfest
  k_environment      0–1: 0 = contact game, 1 = strikeout-heavy
  variance_regime    "low" | "normal" | "high"
  correlation_class  "ace_suppression" | "neutral" | "high_run"
"""


def compute_game_state(
    pitcher_skill_score: float = None,
    park_factor: float = 1.0,
    game_total: float = None,
    pitcher_k_pct: float = None,
) -> dict:
    """
    Compute a game state vector from available context.

    All inputs are optional — falls back to league averages when missing.

    Args:
        pitcher_skill_score: composite pitcher skill (0–10, 5.0 = average)
        park_factor:         park run factor (1.0 = neutral, 1.10 = hitter park)
        game_total:          Vegas over/under (MLB league avg ~8.5)
        pitcher_k_pct:       pitcher K/BF rate (league avg ~0.220)

    Returns dict with pitcher_dominance, run_environment, k_environment,
    variance_regime, correlation_class.
    """
    # ── Pitcher dominance ─────────────────────────────────────────────────────
    # Normalise skill score: 5.0 = average → 0.0, 9.0 = elite → 1.0
    skill             = float(pitcher_skill_score) if pitcher_skill_score else 5.0
    pitcher_dominance = max(0.0, min(1.0, (skill - 5.0) / 4.0))

    # ── Run environment ───────────────────────────────────────────────────────
    # Park factor: 1.0 = neutral → 0.5. Each 0.10 above 1.0 adds 0.5.
    pf_score    = max(0.1, min(0.9, 0.5 + (float(park_factor) - 1.0) * 5.0))
    # Vegas total: MLB avg ~8.5. 7.0 → ~0.3, 8.5 → 0.5, 10.0 → ~0.7
    vegas_score = 0.5
    if game_total:
        vegas_score = max(0.1, min(0.9, 0.5 + (float(game_total) - 8.5) * 0.13))
    # Combine: Vegas total is a stronger signal (fresher, market-informed)
    run_environment = round(pf_score * 0.35 + vegas_score * 0.65, 3)

    # ── K environment ─────────────────────────────────────────────────────────
    # How many Ks is this game likely to produce?
    _league_k  = 0.220
    k_pct      = float(pitcher_k_pct) if pitcher_k_pct else _league_k
    k_environment = max(0.0, min(1.0, 0.5 + (k_pct - _league_k) / 0.10))

    # ── Variance regime ───────────────────────────────────────────────────────
    # Ace game → compressed outcomes (low variance)
    # Hitter park / high run env → fat right tail (high variance)
    if pitcher_dominance > 0.45:
        variance_regime = "low"
    elif run_environment > 0.65:
        variance_regime = "high"
    else:
        variance_regime = "normal"

    # ── Correlation class ─────────────────────────────────────────────────────
    if pitcher_dominance > 0.45:
        correlation_class = "ace_suppression"    # stack OVERS at your peril
    elif run_environment > 0.65:
        correlation_class = "high_run"           # OVER stack is correlated upward
    else:
        correlation_class = "neutral"

    return {
        "pitcher_dominance": round(pitcher_dominance, 3),
        "run_environment":   round(run_environment, 3),
        "k_environment":     round(round(k_environment, 3), 3),
        "variance_regime":   variance_regime,
        "correlation_class": correlation_class,
    }


def correlation_factor_same_game(legs: list) -> float:
    """
    Return a multiplier on joint win probability for a parlay combination.

    For each pair of legs from the SAME game:
      - OVER stack vs ace pitcher:   discount (legs fail together in dominated games)
      - UNDER stack vs ace pitcher:  slight bonus (legs succeed together)
      - Mixed / neutral game:        small discount for any same-game correlation

    The formula is a simplified first-order copula correction:
      P(A ∩ B) ≈ P(A) × P(B) × corr_factor
    where corr_factor encodes the shared game-state risk.

    Args:
        legs: list of scored pick dicts (need: game_id, direction,
              game_state or pitcher_skill_score)

    Returns:
        float multiplier (0.70–1.15). 1.0 = independent (cross-game).
    """
    # Group legs by game_id
    games: dict[str, list] = {}
    for i, leg in enumerate(legs):
        gid = leg.get("game_id") or f"_solo_{i}"
        games.setdefault(gid, []).append(leg)

    multiplier = 1.0

    for gid, glgs in games.items():
        if len(glgs) < 2 or gid.startswith("_solo_"):
            continue

        # Get pitcher dominance from pre-computed game_state or derive from pick
        gs = glgs[0].get("game_state") or {}
        if gs.get("pitcher_dominance") is not None:
            dominance = float(gs["pitcher_dominance"])
        else:
            max_skill = max(
                (float(leg.get("pitcher_skill_score") or 5.0) for leg in glgs),
                default=5.0,
            )
            dominance = max(0.0, min(1.0, (max_skill - 5.0) / 4.0))

        # Also factor in run environment (high run env → OVER correlation is positive)
        run_env = float(gs.get("run_environment", 0.5))

        directions = [leg.get("direction", "OVER") for leg in glgs]
        n_pairs    = len(glgs) * (len(glgs) - 1) / 2   # C(n, 2)

        if all(d == "OVER" for d in directions):
            # OVER stack: ace suppression is bad; high run env partially offsets
            net_dominance = dominance - max(0.0, run_env - 0.5) * 0.5
            net_dominance = max(0.0, net_dominance)
            discount      = n_pairs * net_dominance * 0.08   # up to 8% per pair
            multiplier   *= max(0.70, 1.0 - discount)

        elif all(d == "UNDER" for d in directions):
            # UNDER stack vs ace: they succeed together → slight bonus
            bonus      = n_pairs * dominance * 0.04          # up to 4% per pair
            multiplier *= min(1.15, 1.0 + bonus)

        else:
            # Mixed OVER/UNDER from same game: partial discount for shared game risk
            discount   = n_pairs * dominance * 0.03
            multiplier *= max(0.88, 1.0 - discount)

    return round(multiplier, 3)
