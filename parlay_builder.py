"""
parlay_builder.py — Diversified parlay portfolio optimizer with Kelly bankroll sizing.

Takes scored picks from scanner_power_parlay and builds a slate of diverse parlays
with recommended bet sizes for a given bankroll.

Design principles:
  - Tiered mix: 2 bankers (2-pick) + 2 core (3-pick) + 2 shooters (4-5 pick)
    → 2-picks hit ~45% of the time (steady wins + model feedback)
    → 3-picks hit ~25-30% (solid payoff, reasonable frequency)
    → 4-5 picks hit ~10-15% (high payout, small size, lottery-ticket role)
  - Diversity: max 1 player shared between any two parlays
  - Kelly sizing: fractional (25%) Kelly per parlay, sized by tier
  - Budget: total allocation ≤ 90% of bankroll

PrizePicks Power Play payouts (all-or-nothing):
  2-pick = 3x | 3-pick = 5x | 4-pick = 10x | 5-pick = 20x

Kelly formula per parlay:
  b = payout_multiple - 1  (net payout on $1)
  f* = (b × p_win - (1 - p_win)) / b
  bet = bankroll × f* × kelly_fraction

Tier sizing (fraction of bankroll allocated per tier):
  Bankers (2-pick): larger bets — hit often, steady bankroll growth
  Core (3-pick):    medium bets — good risk/reward balance
  Shooters (4-5 pick): small bets — high upside, capped downside
"""

import itertools
import math
from typing import Optional

# ── Constants ──────────────────────────────────────────────────────────────────
PP_PAYOUTS = {2: 3.0, 3: 6.0, 4: 10.0, 5: 20.0}

KELLY_FRACTION = 0.25   # 25% fractional Kelly — conservative for high-variance parlays
MAX_OVERLAP    = 1      # max players shared between any two parlays
ELITE_P_HIT    = 1.01   # disabled — no player appears in more than 1 parlay ever
                         # (Suzuki 93% p_hit but 2 walks lesson: even "locks" miss,
                         #  and one miss killing 3 parlays defeats the purpose of a portfolio)
MAX_RISK_PCT   = 0.80   # never allocate more than 80% of bankroll across all parlays
POOL_LIMIT     = 25     # only consider top-N picks by p_hit when generating combos

# Sport-level calibration multipliers — applied to p_hit for pool ranking and Kelly.
# Based on 3259-pick dataset: WNBA bet picks 60% (n=72) vs MLB 52% (n=634).
# A 1.06x multiplier on WNBA effectively recalibrates to observed accuracy.
SPORT_MULTIPLIERS = {
    "WNBA": 1.06,
}

# ── Tiered slot system ─────────────────────────────────────────────────────────
# Reserves specific slots per leg count so the portfolio always has a mix.
# Adjust these to change the parlay style (e.g. more bankers = more conservative).
TIER_SLOTS = {
    2: 2,   # 2 banker parlays (2-pick, 3x) — hit ~40-50%, steady wins
    3: 2,   # 2 core parlays  (3-pick, 5x) — hit ~25-35%, solid payoff
    4: 1,   # 1 big parlay    (4-pick, 10x) — hit ~15-20%, high upside
    5: 1,   # 1 shooter       (5-pick, 20x) — hit ~10-15%, lottery ticket
}
# Per-tier Kelly fraction — bankers get more of the bankroll, shooters get less
TIER_KELLY = {
    2: 0.35,   # bankers: 35% Kelly (hit often enough to justify larger size)
    3: 0.25,   # core: standard 25% Kelly
    4: 0.15,   # big: 15% Kelly (high variance, size down)
    5: 0.10,   # shooter: 10% Kelly (essentially lottery, keep it small)
}
# Per-tier max bet (absolute cap regardless of Kelly)
TIER_MAX_BET = {
    2: 12.00,
    3: 8.00,
    4: 5.00,
    5: 3.00,
}
MIN_BET = 1.00   # minimum bet in dollars
MIN_EV  = 0.03   # min 3% EV to include any parlay

# ── Stat types excluded from parlays ──────────────────────────────────────────
# Measured from 3259 resolved picks / 706 bet picks (2026-06-13).
#
#   Pitching Outs          0%   (6 bet picks)  — confirmed disaster
#   Hits Allowed           38%  (21 bet picks) — model consistently wrong
#   Pitcher Strikeouts     46%  (24 bet picks) — no edge
#   Hitter Strikeouts      49%  (37 bet picks) — coin flip confirmed
#   Singles                50%  (121 bet picks)— coin flip with large n
#   Hits+Runs+RBIs         n/a  — composite stat, 3 uncorrelated vars compound error
#   Turnovers              30%  (49 all picks) — terrible
#   3-PT Made              36%  (11 all picks) — terrible
#
# WNBA combos: promising but below n≥30 threshold for inclusion.
#   Pts+Rebs+Asts: 73% (11 bet picks), Pts+Asts: 60% (10), Rebs+Asts: 86% (7)
#   Revisit when each hits 30+ resolved bet picks.
#
EXCLUDED_STAT_TYPES = {
    # MLB pitcher — confirmed terrible
    "Pitcher Strikeouts",   # 46% hit rate (24 bet picks) — no edge
    "Strikeouts",           # same stat, alternate API name
    "Pitching Outs",        # 0% actual hit rate (6 picks)
    "Hits Allowed",         # 38% hit rate (21 bet picks) — confirmed bad
    "Pitcher Fantasy Score",# OVER: 0/4 = 0% stays excluded.
                            # UNDER: 100% (10/10) — unlocked via UNDER_EXCEPTIONS below.
    # MLB batter — confirmed bad
    "Hitter Strikeouts",    # 49% (37 bet picks) — coin flip, confirmed bad
    "Hits+Runs+RBIs",       # composite: 3 uncorrelated stats inflate false confidence
    "Singles",              # 50% (121 bet picks) — coin flip, no edge
    # MLB pitcher (batter-facing stats) — confirmed bad
    "Earned Runs Allowed",  # 42% (19 bet picks) — OVER 43%, UNDER 42%, both bad
    # NBA/WNBA — confirmed bad
    "Turnovers",            # 30.6% hit rate (49 picks) — confirmed terrible
    "3-PT Made",            # 36.4% hit rate (11 picks)
    "3-Pointers Made",      # same stat, alternate name
    # WNBA combos — promising but below n≥30 threshold
    "Pts+Rebs+Asts",        # 73% (11 bet picks) — good signal but wait for n≥30
    "Pts+Rebs",             # 53% (15 bet picks) — marginal, wait for n≥30
    "Pts+Asts",             # 60% (10 bet picks) — looks better now, wait for n≥30
    "Rebs+Asts",            # 86% (7 bet picks) — tiny sample, wait for n≥30
}

# Stat types that are EXCLUDED for OVER but explicitly allowed for UNDER.
# Each entry requires a higher hit_rate floor (see eligible filter below).
# Data source: analyze_unders.py run 2026-06-13 on 228 UNDER bet picks.
UNDER_EXCEPTIONS = {
    "Pitcher Fantasy Score",  # UNDER: 100% (10/10). Structural: pitchers pulled early,
                              # natural ceiling on fantasy production. Hit_rate floor: 0.75.
}
MIN_HIT_RATE_UNDER_EXCEPTION = 0.75  # stricter than OVER floor (0.67) — limited sample

# Quality gates — all three must pass for a pick to enter a parlay.
# 3259-pick dataset (2026-06-13): confidence buckets vs actual hit rate on BET picks:
#   65–70%:  47% actual (n=196) — negative EV
#   70–75%:  54% actual (n=280) — real signal, positive EV
#   75–80%:  57% actual (n=194) — best bucket
#   80–85%:  55% actual (n=33)  — over-confident, model thinks 82% but hits 55%
MIN_CONF_PARLAY  = 0.70   # 65-70% bucket hits 47% (n=196 bet picks) — no edge below 70%
MIN_HIT_RATE     = 0.67   # historical hit rate is our most reliable signal
MIN_P_HIT_PARLAY = 0.70   # model probability must agree with confidence
MIN_EDGE_PCT_PARLAY = 0.30  # 15-25% edge zone hits 38-45% (n=32) — cut below 30%
# UNDER picks banned from parlays.
# 3259-pick dataset: OVER bet picks 56% (n=478), UNDER bet picks 47% (n=228).
# UNDERs confirmed bad with n≥150 — OVERS_ONLY is the correct long-term policy.
PARLAY_OVERS_ONLY = True    # Confirmed: UNDER 47% (n=228) vs OVER 56% (n=478)
# Max gap the model probability can exceed empirical hit rate.
# If model says 93% but history says 60%, we cap p_hit at 75%.
MAX_MODEL_OVERREACH = 0.15


def _get_p_hit(pick: dict) -> float:
    """
    Best available P(hit) estimate for a single leg.

    Priority:
      1. Model probability (p_over / p_under from distribution engine).
      2. Confidence score as fallback when model hasn't set p_over/p_under.

    In both cases, the result is capped at hit_rate + MAX_MODEL_OVERREACH to
    prevent the Gaussian or zero-inflated model from being wildly overconfident
    relative to what the player has actually done historically.

    Example: Cameron Brink OVER 13.5 PRA — model gives 96% (Gaussian on 22.99 avg),
    but empirical hit rate is 60%. Cap: 60% + 15% = 75%. That's what we use.
    """
    direction  = pick.get("direction", "OVER")
    p_over     = pick.get("p_over")
    p_under    = pick.get("p_under")
    hit_rate   = pick.get("hit_rate", 0.0)

    # Get raw model probability
    if direction == "OVER" and p_over and 0.05 < p_over < 0.99:
        model_p = p_over
    elif direction == "UNDER" and p_under and 0.05 < p_under < 0.99:
        model_p = p_under
    else:
        # No model probability — fall back to confidence, capped by hit rate.
        # Use `is not None` not `> 0.10` — hit_rate=0.0 is a real value (player
        # never hit this line) and should still cap the model, not be skipped.
        conf = pick.get("confidence", 0.62)
        sport = pick.get("sport", "")
        base = min(conf, hit_rate + MAX_MODEL_OVERREACH) if hit_rate is not None else conf
        return min(0.95, base * SPORT_MULTIPLIERS.get(sport, 1.0))

    # Cap: model can't claim more than hit_rate + MAX_MODEL_OVERREACH.
    # hit_rate=0.0 is falsy but it IS a real value — use `is not None`.
    if hit_rate is not None:
        model_p = min(model_p, hit_rate + MAX_MODEL_OVERREACH)

    # Sport calibration: WNBA empirically hits 60% vs MLB 52% — upward adjust.
    sport = pick.get("sport", "")
    model_p = min(0.95, model_p * SPORT_MULTIPLIERS.get(sport, 1.0))

    return model_p


def kelly_size(p_win: float, payout_multiple: float, bankroll: float,
               frac: float = KELLY_FRACTION) -> float:
    """
    Fractional Kelly bet size in dollars.

    b    = payout_multiple - 1  (net odds)
    f*   = (b × p - q) / b     (full Kelly fraction of bankroll)
    bet  = bankroll × f* × frac

    Returns 0.0 if the bet has negative EV.
    """
    b = payout_multiple - 1.0
    q = 1.0 - p_win
    full_kelly = (b * p_win - q) / b  # fraction of bankroll
    if full_kelly <= 0:
        return 0.0
    raw     = bankroll * full_kelly * frac
    max_bet = max(v for v in TIER_MAX_BET.values())   # use highest tier cap as ceiling
    return max(MIN_BET, min(max_bet, round(raw, 2)))


def _correlation_factor(combo: list[dict]) -> float:
    """
    Estimate correlation adjustment for a combination of picks.

    Layers applied in order (multiplicative):

      1. Same-team batters (vs same pitcher) → ×0.88 per extra teammate
         Two Cubs batters face the same pitcher — pitcher dominance or wildness
         affects BOTH. Measured inter-batter correlation ≈ 0.12–0.18.
         Applied BEFORE the game-state model so it can't be swallowed by it.

      2. Full game-state model (pitcher skill × lineup cascade × park)
         Uses data.mlb_game_state when available.

      3. Fallback: different-team same-game → ×0.92, different games → 1.0

    Returns a multiplier: < 1.0 = correlated (reduces p_win estimate).
    """
    # ── Layer 1: Same-team batter correlation (always applies first) ──────────
    team_counts: dict[str, int] = {}
    for p in combo:
        team = p.get("player_team", "").strip()
        if team:
            team_counts[team] = team_counts.get(team, 0) + 1

    same_team_factor = 1.0
    for team, count in team_counts.items():
        if count >= 2:
            # 0.88 per extra same-team player: 2 players → ×0.88, 3 → ×0.77
            same_team_factor *= 0.88 ** (count - 1)

    # ── Layer 2: Full game-state model ────────────────────────────────────────
    game_state_factor = 1.0
    try:
        from data.mlb_game_state import joint_game_correlation_factor as _jcf
        game_state_factor = _jcf(combo)
    except Exception:
        # ── Layer 3: Heuristic fallback ───────────────────────────────────────
        game_ids = [p.get("game_id", "") for p in combo if p.get("game_id")]
        if len(game_ids) != len(set(game_ids)):
            game_state_factor = 0.92  # different teams, same game

    return round(same_team_factor * game_state_factor, 3)


def _combo_ev(combo: tuple, corr_factor: float) -> float:
    """EV = p_win × payout - 1, where p_win is correlation-adjusted joint probability."""
    n       = len(combo)
    payout  = PP_PAYOUTS.get(n, 20.0)
    p_indep = math.prod(_get_p_hit(leg) for leg in combo)
    p_win   = p_indep * corr_factor
    return p_win * payout - 1.0, p_win, p_indep, corr_factor


def _passes_direction_gate(p: dict) -> bool:
    """
    Returns True if the pick is allowed based on stat type and direction.

    Rules:
    - Picks in EXCLUDED_STAT_TYPES are blocked UNLESS they're in UNDER_EXCEPTIONS
      with direction=UNDER and hit_rate >= MIN_HIT_RATE_UNDER_EXCEPTION.
    - PARLAY_OVERS_ONLY blocks all UNDERs UNLESS the stat is in UNDER_EXCEPTIONS.
    """
    stat = p.get("stat_type", "")
    direction = p.get("direction", "OVER")
    hit_rate = p.get("hit_rate", 0.0)

    is_under_exception = (
        stat in UNDER_EXCEPTIONS
        and direction == "UNDER"
        and hit_rate >= MIN_HIT_RATE_UNDER_EXCEPTION
    )

    # Block excluded stat types (except unlocked UNDERs)
    if stat in EXCLUDED_STAT_TYPES and not is_under_exception:
        return False

    # Block UNDERs when PARLAY_OVERS_ONLY is set (except unlocked UNDERs)
    if PARLAY_OVERS_ONLY and direction != "OVER" and not is_under_exception:
        return False

    return True


def build_diverse_parlays(
    scored_picks: list[dict],
    bankroll: float = 50.0,
    tier_slots: dict = None,
    max_overlap: int = MAX_OVERLAP,
) -> list[dict]:
    """
    Build a tiered, diversified parlay portfolio.

    Uses TIER_SLOTS to reserve specific slots per leg count, so the portfolio
    always contains a mix of banker (2-pick), core (3-pick), and shooter (4-5 pick)
    parlays — rather than filling all slots with high-EV 5-picks.

    Algorithm per tier:
      1. Generate all combinations of that leg count (top-POOL_LIMIT picks)
      2. Score by EV, rank descending
      3. Select best combos that pass diversity filters:
         - Max MAX_OVERLAP players shared between any two selected parlays
         - A player only appears in 1 parlay (2 if elite, p_hit ≥ ELITE_P_HIT)
      4. Size with tier-specific fractional Kelly, capped at TIER_MAX_BET

    Returns list of parlay dicts ordered: bankers first, then core, then shooters.
    """
    if tier_slots is None:
        tier_slots = TIER_SLOTS

    if not scored_picks:
        return []

    # Filter eligible picks — ALL gates must pass:
    #   1. Composite model confidence ≥ 65%
    #   2. Empirical hit rate ≥ 62%  (historical evidence floor — model can't override)
    #   3. Model probability ≥ 68%   (after hit_rate cap applied in _get_p_hit)
    #   4. Stat type not in EXCLUDED_STAT_TYPES (composite/overconfident stat types)
    eligible = [
        p for p in scored_picks
        if p.get("confidence", 0) >= MIN_CONF_PARLAY
        and p.get("hit_rate", 0) >= MIN_HIT_RATE
        and _get_p_hit(p) >= MIN_P_HIT_PARLAY
        and p.get("edge_pct", 0) >= MIN_EDGE_PCT_PARLAY
        and _passes_direction_gate(p)
    ]
    eligible.sort(key=_get_p_hit, reverse=True)

    # Deduplicate by player: keep only the highest-scoring pick per player.
    # Without this, a player with Points + PRA + Pts+Rebs all scoring highly
    # consumes 3 of 25 pool slots — but only 1 can ever enter a parlay (player
    # uniqueness rule). The other 2 are wasted slots that crowd out other players.
    seen_players: set[str] = set()
    deduped: list[dict] = []
    for p in eligible:
        player = p.get("player", "")
        if player not in seen_players:
            seen_players.add(player)
            deduped.append(p)

    # Cap "Hitter Fantasy Score" picks — HFS dominates the pool on MLB days
    # (every player has an HFS line) and creates high intra-portfolio correlation.
    # Max 4 HFS picks in the 25-pick pool so other stat types get representation.
    MAX_HFS_IN_POOL = 4
    hfs_count = 0
    capped: list[dict] = []
    for p in deduped:
        if p.get("stat_type") == "Hitter Fantasy Score":
            if hfs_count >= MAX_HFS_IN_POOL:
                continue
            hfs_count += 1
        capped.append(p)

    pool = capped[:POOL_LIMIT]

    if len(pool) < 2:
        return []

    # ── Step 1: Build candidate list per tier ────────────────────────────────
    candidates_by_tier: dict[int, list] = {n: [] for n in tier_slots}

    for n_legs, slots in tier_slots.items():
        if slots == 0 or len(pool) < n_legs:
            continue
        payout = PP_PAYOUTS.get(n_legs, 20.0)
        for combo in itertools.combinations(pool, n_legs):
            # ── PrizePicks rule: same player can't appear twice in one parlay ───
            player_names = [leg["player"] for leg in combo]
            if len(player_names) != len(set(player_names)):
                continue

            # ── PrizePicks rule: can't parlay players all from the same team ─────
            known_teams = [leg.get("player_team", "").strip() for leg in combo
                           if leg.get("player_team", "").strip()]
            if len(known_teams) >= 2 and len(set(known_teams)) == 1:
                continue

            corr = _correlation_factor(list(combo))
            ev, p_win, p_indep, _ = _combo_ev(combo, corr)
            if ev < MIN_EV:
                continue
            # Min win probability floor per tier (2-picks need higher floor)
            min_p = {2: 0.35, 3: 0.25, 4: 0.18, 5: 0.10}.get(n_legs, 0.10)
            if p_win < min_p:
                continue
            candidates_by_tier[n_legs].append({
                "combo":   combo,
                "n_legs":  n_legs,
                "payout":  payout,
                "p_win":   round(p_win, 4),
                "p_indep": round(p_indep, 4),
                "corr":    round(corr, 4),
                "ev":      round(ev, 4),
                "ev_pct":  int(ev * 100),
                "ev_rating": (
                    "🔥 HIGH"      if ev >= 0.20 else
                    "✅ MED-HIGH"  if ev >= 0.10 else
                    "🟡 MED"       if ev >= 0.05 else "⚠️ LOW"
                ),
            })
        candidates_by_tier[n_legs].sort(key=lambda x: x["ev"], reverse=True)

    # ── Step 2: Select with diversity constraint across ALL tiers ─────────────
    selected:       list[dict]      = []
    player_count:   dict[str, int]  = {}
    parlay_players: list[set]       = []

    def _try_add(cand: dict) -> bool:
        combo        = cand["combo"]
        this_players = {leg["player"] for leg in combo}

        for existing in parlay_players:
            if len(this_players & existing) > max_overlap:
                return False

        for player in this_players:
            p_hit = _get_p_hit(next(leg for leg in combo if leg["player"] == player))
            limit = 2 if p_hit >= ELITE_P_HIT else 1
            if player_count.get(player, 0) >= limit:
                return False

        selected.append(cand)
        parlay_players.append(this_players)
        for player in this_players:
            player_count[player] = player_count.get(player, 0) + 1
        return True

    # Fill tier slots in order: bankers → core → shooters
    for n_legs in sorted(tier_slots.keys()):
        slots = tier_slots[n_legs]
        filled = 0
        for cand in candidates_by_tier.get(n_legs, []):
            if filled >= slots:
                break
            if _try_add(cand):
                filled += 1

    # ── Step 3: Tier-specific Kelly sizing ───────────────────────────────────
    for cand in selected:
        n   = cand["n_legs"]
        kf  = TIER_KELLY.get(n, KELLY_FRACTION)
        cap = TIER_MAX_BET.get(n, 10.0)
        b   = cand["payout"] - 1.0
        full_k = max(0.0, (b * cand["p_win"] - (1 - cand["p_win"])) / b)
        raw    = bankroll * full_k * kf
        cand["kelly_full_pct"] = round(full_k * 100, 1)
        cand["kelly_frac_pct"] = round(full_k * kf  * 100, 1)
        cand["bet_raw"]        = max(MIN_BET, min(cap, round(raw, 2)))
        cand["win_amount_raw"] = round(cand["bet_raw"] * cand["payout"], 2)

    # ── Step 4: Budget cap ────────────────────────────────────────────────────
    total_raw = sum(c["bet_raw"] for c in selected)
    max_total = bankroll * MAX_RISK_PCT
    if total_raw > max_total and total_raw > 0:
        scale = max_total / total_raw
        for cand in selected:
            cand["bet_raw"] = max(MIN_BET, round(cand["bet_raw"] * scale, 2))

    for cand in selected:
        cand["bet_size"]    = _round_bet(cand["bet_raw"], bankroll)
        cand["win_amount"]  = round(cand["bet_size"] * cand["payout"], 2)
        cand["net_profit"]  = round(cand["win_amount"] - cand["bet_size"], 2)

    # Build final leg breakdowns
    for cand in selected:
        cand["legs"] = list(cand["combo"])
        cand["leg_summary"] = []
        for leg in cand["legs"]:
            ph = _get_p_hit(leg)
            p_src = "model" if (leg.get("p_over") or leg.get("p_under")) else "conf"
            cand["leg_summary"].append({
                "player":    leg["player"],
                "sport":     leg["sport"],
                "direction": leg["direction"],
                "line":      leg["line"],
                "stat_type": leg["stat_type"],
                "p_hit":     round(ph, 3),
                "p_hit_pct": int(ph * 100),
                "p_src":     p_src,
                "conf_pct":  leg.get("conf_pct", 0),
                "hit_rate":  round(leg.get("hit_rate", 0), 3),
                "avg":       leg.get("avg", 0),
                "n_games":   leg.get("n_games", 0),
                "recent_5":  leg.get("recent_values", [])[:5],
            })
        del cand["combo"]  # not JSON-serializable cleanly

    return selected


def _round_bet(amount: float, bankroll: float) -> float:
    """Round bet to nearest $0.50; ensure it's at least $1."""
    rounded = round(amount * 2) / 2  # nearest $0.50
    return max(MIN_BET, rounded)


def format_parlay_plan(
    parlays: list[dict],
    bankroll: float,
    top_singles: Optional[list[dict]] = None,
) -> str:
    """
    Format the full parlay portfolio for a human-readable push/Discord message.

    Shows:
      - Each parlay with legs, sizing, and win amounts
      - Total risk and expected return
      - Kelly math transparency
    """
    if not parlays:
        return "No qualifying parlays found."

    lines = []
    total_bet = sum(p["bet_size"] for p in parlays)
    total_win = sum(p["win_amount"] for p in parlays)

    lines.append(f"💰 PARLAY PLAN — ${bankroll:.0f} bankroll")
    lines.append(f"   Placing ${total_bet:.2f} across {len(parlays)} parlays")
    lines.append(f"   If all parlays hit: ${total_win:.2f}  |  Most likely outcome: some hit, some miss")
    lines.append("")

    sport_emoji = {"MLB": "⚾", "WNBA": "🏀", "NBA": "🏀", "NHL": "🏒",
                   "TENNIS": "🎾", "SOCCER": "⚽"}

    for i, par in enumerate(parlays, 1):
        payout    = int(par["payout"])
        p_win_pct = int(par["p_win"] * 100)
        bet       = par["bet_size"]
        win_amt   = par["win_amount"]
        net       = par["net_profit"]
        corr      = par["corr"]
        n_legs    = par["n_legs"]

        lines.append(f"━━━ Parlay {i}  ({n_legs}-pick, {payout}x payout) ━━━")
        lines.append(f"   Bet ${bet:.2f}  →  Win ${win_amt:.2f} (+${net:.2f} profit) if all {n_legs} hit")
        lines.append(f"   Model win probability: {p_win_pct}%")

        if corr < 0.94:
            lines.append(f"   ⚠️  Two legs in the same game — win chance reduced slightly")
        elif corr > 1.03:
            lines.append(f"   ✅  Lineup correlation bonus applied")

        lines.append("")

        for leg in par["leg_summary"]:
            e         = sport_emoji.get(leg["sport"], "🎯")
            direction = leg["direction"]
            arrow     = "↑" if direction == "OVER" else "↓"
            hit_pct   = int(leg["hit_rate"] * 100)
            avg       = leg["avg"]
            recent    = leg["recent_5"]
            lines.append(
                f"   {e}{arrow} {leg['player']}  {direction} {leg['line']} {leg['stat_type']}"
            )
            lines.append(
                f"      Hit {hit_pct}% of games  ·  season avg {avg}  ·  last 5: {recent}"
            )

        lines.append(f"   (Sized by Kelly formula on ${bankroll:.0f} bankroll)")
        lines.append("")

    lines.append("─" * 50)
    lines.append(f"TOTAL AT RISK: ${total_bet:.2f} of ${bankroll:.0f}")
    lines.append("")

    if top_singles:
        lines.append("OTHER STRONG SINGLE PICKS (not in parlays above):")
        for s in top_singles[:5]:
            e     = sport_emoji.get(s["sport"], "🎯")
            arrow = "↑" if s["direction"] == "OVER" else "↓"
            lines.append(
                f"  {e}{arrow} {s['player']} {s['direction']} {s['line']} {s['stat_type']} "
                f"— {int(s.get('hit_rate',0)*100)}% hit rate, avg {s.get('avg','?')}"
            )

    return "\n".join(lines)


def format_parlay_ntfy(parlays: list[dict], bankroll: float,
                       goblin_parlays: list[dict] = None,
                       demon_parlays:  list[dict] = None,
                       top_picks: list[dict] = None) -> tuple[str, str]:
    """
    Returns (title, body) for ntfy push notification.
    Human-readable format — designed for phone screen.
    Shows top individual picks, then standard + goblin + demon parlays.
    """
    all_parlays = parlays or []
    has_any = all_parlays or goblin_parlays or demon_parlays
    if not has_any:
        return "No parlays found", "No qualifying parlays today."

    total_bet = sum(p["bet_size"] for p in all_parlays)
    total_win = sum(p["win_amount"] for p in all_parlays)

    extras = []
    if goblin_parlays:
        total_bet += sum(p["bet_size"] for p in goblin_parlays)
        total_win += sum(p["win_amount"] for p in goblin_parlays)
        extras.append("🧌 goblin")
    if demon_parlays:
        total_bet += sum(p["bet_size"] for p in demon_parlays)
        total_win += sum(p["win_amount"] for p in demon_parlays)
        extras.append("😈 demon")

    extra_str = f" + {', '.join(extras)}" if extras else ""
    title = f"🎯 {len(all_parlays)} standard{extra_str} — risk ${total_bet:.0f}, max win ${total_win:.0f}"

    lines = []

    # Top individual picks — the strongest signals regardless of parlay eligibility
    if top_picks:
        lines.append("── TOP PICKS ──")
        for p in top_picks[:6]:
            arrow  = "↑" if p.get("direction") == "OVER" else "↓"
            sport  = p.get("sport", "")
            e      = {"MLB": "⚾", "WNBA": "🏀", "NBA": "🏀", "NHL": "🏒"}.get(sport, "🎯")
            conf   = p.get("conf_pct") or int(p.get("confidence", 0) * 100)
            hr     = int(p.get("hit_rate", 0) * 100)
            lines.append(
                f"  {e}{arrow} {p['player']} {p.get('direction','OVER')} {p['line']} "
                f"{p['stat_type']} ({conf}% · {hr}% HR)"
            )
        lines.append("")

    # Standard parlays
    for i, par in enumerate(all_parlays, 1):
        bet    = par["bet_size"]
        win    = par["win_amount"]
        p_win  = int(par["p_win"] * 100)
        n_legs = par["n_legs"]
        payout = int(par["payout"])
        lines.append(f"── Standard {i}: ${bet:.0f}→${win:.0f} | {n_legs}-pick {payout}x | {p_win}% ──")
        for leg in par["leg_summary"]:
            arrow = "↑" if leg["direction"] == "OVER" else "↓"
            lines.append(f"  {arrow} {leg['player']} — {leg['direction']} {leg['line']} {leg['stat_type']}  ({int(leg['hit_rate']*100)}% HR, avg {leg['avg']})")
        lines.append("")

    # Goblin parlay
    for par in (goblin_parlays or [])[:1]:
        bet    = par["bet_size"]
        win    = par["win_amount"]
        p_win  = int(par["p_win"] * 100)
        n_legs = par["n_legs"]
        lines.append(f"── 🧌 Goblin: ${bet:.0f}→~${win:.0f} | {n_legs}-pick ~{par['payout']}x | {p_win}% (check app for real multiplier) ──")
        for leg in par["leg_summary"]:
            lines.append(f"  ↑ {leg['player']} — OVER {leg['line']} {leg['stat_type']}  ({int(leg['hit_rate']*100)}% HR, avg {leg['avg']})")
        lines.append("")

    # Demon parlay
    for par in (demon_parlays or [])[:1]:
        bet    = par["bet_size"]
        win    = par["win_amount"]
        p_win  = int(par["p_win"] * 100)
        n_legs = par["n_legs"]
        lines.append(f"── 😈 Demon: ${bet:.0f}→~${win:.0f} | {n_legs}-pick ~{par['payout']}x | {p_win}% (check app for real multiplier) ──")
        for leg in par["leg_summary"]:
            arrow = "↑" if leg["direction"] == "OVER" else "↓"
            lines.append(f"  {arrow} {leg['player']} — {leg['direction']} {leg['line']} {leg['stat_type']}  ({int(leg['hit_rate']*100)}% HR, avg {leg['avg']})")
        lines.append("")

    body = "\n".join(lines)
    return title, body


def run_parlay_plan(
    scored_picks: list[dict],
    bankroll: float = 50.0,
    tier_slots: dict = None,
    verbose: bool = True,
) -> list[dict]:
    """
    Main entry point: build + size + print parlays.

    Args:
        scored_picks: output of scanner_power_parlay.score_pick()
        bankroll:     total dollars available to bet
        tier_slots:   override TIER_SLOTS dict, e.g. {2:3, 3:2, 4:1, 5:0}
        verbose:      print the plan to stdout

    Returns:
        List of parlay dicts with 'bet_size', 'win_amount', 'ev', etc.
    """
    parlays = build_diverse_parlays(
        scored_picks,
        bankroll=bankroll,
        tier_slots=tier_slots,
    )

    if verbose:
        # Picks not in any parlay → surface as standalone edges
        in_parlay_players = {
            leg["player"]
            for par in parlays
            for leg in par["leg_summary"]
        }
        standalone = [
            p for p in scored_picks
            if p["player"] not in in_parlay_players
            and p.get("confidence", 0) >= 0.70
        ]
        standalone.sort(key=lambda x: x["confidence"], reverse=True)
        plan_str = format_parlay_plan(parlays, bankroll, top_singles=standalone[:5])
        print(plan_str)

    return parlays


# ── Goblin & Demon parlay builders ────────────────────────────────────────────
# ── Goblin / Demon multiplier system ─────────────────────────────────────────
# PrizePicks does NOT expose multipliers via API — they're calculated dynamically
# in the app. The formula is payout = standard_payout[N] × product(pick_factors).
#
# Derived from known data points:
#   2-pick all-goblin  = 1.4x  → goblin_factor = sqrt(1.4/3) ≈ 0.683
#   2-pick 1D+1G       = 5.0x  → demon_factor_easy ≈ 2.44  (at ~rank 400-500)
#   6-pick all-demon   = 1000x → demon_factor_avg  ≈ 1.73  (avg across all ranks)
#
# Harder demon lines (lower rank) carry higher per-pick multipliers.
# The rank field (lower = harder = higher real multiplier) drives the adjustment.
# ALWAYS verify the exact multiplier in the PrizePicks app before submitting.

_STANDARD_PAYOUTS = {2: 3.0, 3: 6.0, 4: 10.0, 5: 20.0, 6: 37.5}
_GOBLIN_FACTOR    = 0.683   # per goblin pick
# Demon per-pick factor by difficulty_rank bucket (lower rank = harder = bigger factor)
_DEMON_FACTORS    = [
    (150,  2.10),   # rank ≤ 150: extreme demon (5.5 TB, very high ERA lines)
    (200,  1.95),   # rank 151-200: very hard
    (300,  1.85),   # rank 201-300: hard
    (400,  1.75),   # rank 301-400: moderate demon
    (9999, 1.65),   # rank > 400: easier demon
]

def _demon_factor(rank: int) -> float:
    """Return estimated per-pick multiplier for a demon pick of this difficulty rank."""
    for threshold, factor in _DEMON_FACTORS:
        if rank <= threshold:
            return factor
    return 1.65

def _calc_payout(n_legs: int, picks: list[dict]) -> tuple[float, str]:
    """
    Calculate approximate payout for a goblin/demon lineup.
    Returns (payout_multiplier, note_string).
    """
    base = _STANDARD_PAYOUTS.get(n_legs, 37.5)
    multiplier = 1.0
    for p in picks:
        kind = p.get("projection_kind", "standard")
        if kind == "goblin":
            multiplier *= _GOBLIN_FACTOR
        elif kind == "demon":
            rank = p.get("difficulty_rank", 500)
            multiplier *= _demon_factor(rank)
        # standard: × 1.0
    payout = round(base * multiplier, 1)
    note = f"~{payout}x est. (rank-based approx — verify in app)"
    return payout, note

# Legacy flat tables kept for display fallback
PP_GOBLIN_PAYOUTS = {2: 1.5,  3: 3.0,  4: 5.0,  5: 8.0}
PP_DEMON_PAYOUTS  = {2: 15.0, 3: 50.0, 4: 150.0, 5: 500.0, 6: 2000.0}

# Lower thresholds for goblin — lines are already easy
MIN_CONF_GOBLIN  = 0.55
MIN_HIT_GOBLIN   = 0.55
MIN_EDGE_GOBLIN  = 0.03   # any positive edge (line is set low, any gap matters)

# Lower thresholds for demon — OVER on a hard line, multiplier compensates.
# Demon lines are set above the player's average, so hit rates will be lower.
# We need players trending up / on hot streaks to actually clear the demon line.
MIN_CONF_DEMON   = 0.50
MIN_HIT_DEMON    = 0.35   # lowered — demon OVER at 35-50% hit rate is viable at 500x+
MIN_EDGE_DEMON   = 0.00   # even flat edge ok — the multiplier does the work


def _pick_hardest_viable(picks: list[dict], min_hit: float) -> list[dict]:
    """
    For each (player, stat_type) group, keep only the hardest line that still
    has hit_rate >= min_hit. 'Hardest' = lowest difficulty_rank (lower rank =
    harder line = higher real multiplier on PrizePicks).

    This ensures we always bet at the highest-multiplier line the player can
    still realistically clear, rather than defaulting to the easiest line.
    """
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for p in picks:
        key = (p["player"], p["stat_type"])
        groups[key].append(p)

    best: list[dict] = []
    for key, candidates in groups.items():
        # Filter to only viable candidates first
        viable = [c for c in candidates if c.get("hit_rate", 0) >= min_hit]
        if not viable:
            # Fall back to the easiest line that passes at all
            viable = candidates
        # Pick the hardest viable line (lowest difficulty_rank = hardest)
        viable.sort(key=lambda x: x.get("difficulty_rank", 999))
        best.append(viable[0])

    return best


def build_goblin_parlays(goblin_picks: list[dict], bankroll: float = 50.0) -> list[dict]:
    """
    Build a goblin-only parlay from picks tagged projection_kind='goblin'.

    Goblins: PrizePicks sets line BELOW player's expected output → OVER is easy.
    PrizePicks requires you pick MORE (OVER) on all goblin projections.
    Within the goblin tier there are multiple difficulty levels — we always
    pick the hardest goblin line that still clears our hit-rate floor, since
    harder goblin lines carry higher multipliers.
    Returns 1 parlay (2-3 legs) optimised for hit probability at best multiplier.
    """
    eligible = [
        p for p in goblin_picks
        if p.get("confidence", 0) >= MIN_CONF_GOBLIN
        and p.get("hit_rate", 0)  >= MIN_HIT_GOBLIN
        and p.get("stat_type", "") not in EXCLUDED_STAT_TYPES
        and p.get("edge_pct", 0)  >= MIN_EDGE_GOBLIN
        and p.get("direction") == "OVER"          # goblins require MORE
        and p.get("projection_kind") == "goblin"
    ]
    # Per player+stat: pick the hardest viable goblin line (highest real multiplier)
    eligible = _pick_hardest_viable(eligible, min_hit=MIN_HIT_GOBLIN)
    eligible.sort(key=lambda x: (x.get("hit_rate", 0), _get_p_hit(x)), reverse=True)
    pool = eligible[:15]

    if len(pool) < 2:
        return []

    best = None
    best_p = 0.0

    # Prefer 3-pick; fall back to 2-pick
    for n in [3, 2]:
        if len(pool) < n:
            continue
        payout = PP_GOBLIN_PAYOUTS.get(n, 1.5)
        for combo in itertools.combinations(pool[:10], n):
            names = [leg["player"] for leg in combo]
            if len(names) != len(set(names)):
                continue
            p_win = math.prod(_get_p_hit(leg) for leg in combo)
            if p_win > best_p:
                best_p = p_win
                best = _make_parlay_dict(combo, n, payout, p_win, bankroll,
                                         kind="goblin", kelly_frac=0.12)
        if best:
            break

    return [best] if best else []


def build_demon_parlays(demon_picks: list[dict], bankroll: float = 50.0) -> list[dict]:
    """
    Build a demon-only parlay from picks tagged projection_kind='demon'.

    Demons: PrizePicks sets line ABOVE player's expected output → OVER is hard
    but the multiplier is huge. We look for demon OVER picks where our model
    still gives decent confidence (player trending up / exceeding the hard line).
    Returns 1 parlay (4-6 legs) optimised for EV given the large multiplier.
    """
    # Demon = PrizePicks requires you pick MORE (OVER), same as goblins.
    # Demon lines are set ABOVE the player's average — these are hard-line OVER bets.
    # We use lower thresholds than standard because demon multipliers compensate
    # for the lower individual hit rate. Look for players trending up or on hot
    # streaks whose recent form can actually clear the hard demon line.
    eligible = [
        p for p in demon_picks
        if p.get("confidence", 0) >= MIN_CONF_DEMON
        and p.get("hit_rate", 0)  >= MIN_HIT_DEMON
        and p.get("stat_type", "") not in EXCLUDED_STAT_TYPES
        and p.get("edge_pct", 0)  >= MIN_EDGE_DEMON
        and p.get("direction") == "OVER"           # demons require MORE
        and p.get("projection_kind") == "demon"
    ]
    # Per player+stat: pick the hardest viable demon line (highest real multiplier)
    eligible = _pick_hardest_viable(eligible, min_hit=MIN_HIT_DEMON)
    eligible.sort(key=lambda x: (x.get("hit_rate", 0), _get_p_hit(x)), reverse=True)
    pool = eligible[:20]

    if len(pool) < 3:
        return []

    best = None
    best_ev = -999.0

    # Prefer more legs (bigger multiplier); min 3 legs
    for n in [6, 5, 4, 3]:
        if len(pool) < n:
            continue
        payout = PP_DEMON_PAYOUTS.get(n, 50.0)
        for combo in itertools.combinations(pool[:12], n):
            names = [leg["player"] for leg in combo]
            if len(names) != len(set(names)):
                continue
            p_win = math.prod(_get_p_hit(leg) for leg in combo)
            ev = p_win * payout - 1.0
            if ev > best_ev:
                best_ev = ev
                best = _make_parlay_dict(combo, n, payout, p_win, bankroll,
                                         kind="demon", kelly_frac=0.04)
        if best:
            break

    return [best] if best else []


def _make_parlay_dict(combo, n_legs: int, payout: float, p_win: float,
                      bankroll: float, kind: str, kelly_frac: float) -> dict:
    """Helper: pack a combo tuple into the standard parlay dict format."""
    # Use rank-based payout estimation instead of flat approximation
    combo_list = list(combo)
    calc_payout, calc_note = _calc_payout(n_legs, combo_list)

    bet  = round(bankroll * kelly_frac, 2)
    win  = round(bet * calc_payout, 2)
    ev   = round(p_win * calc_payout - 1.0, 4)
    return {
        "n_legs":      n_legs,
        "payout":      calc_payout,
        "p_win":       round(p_win, 4),
        "ev_pct":      int(ev * 100),
        "ev_rating":   ("🔥 HIGH" if ev >= 0.20 else "✅ MED" if ev >= 0.05 else "⚠️ LOW"),
        "bet_size":    bet,
        "win_amount":  win,
        "net_profit":  round(win - bet, 2),
        "kelly_full_pct":  0.0,
        "kelly_frac_pct":  round(kelly_frac * 100, 1),
        "parlay_type": kind,
        "note":        calc_note,
        "leg_summary": [
            {
                "player":          leg["player"],
                "stat_type":       leg["stat_type"],
                "direction":       leg["direction"],
                "line":            leg["line"],
                "hit_rate":        leg.get("hit_rate", 0),
                "p_hit_pct":       int(_get_p_hit(leg) * 100),
                "confidence":      leg.get("confidence", 0),
                "avg":             leg.get("avg", "?"),
                "n_games":         leg.get("n_games", 0),
                "difficulty_rank": leg.get("difficulty_rank", 999),
            }
            for leg in combo
        ],
    }


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Parlay portfolio builder + Kelly sizer")
    parser.add_argument("--bankroll", type=float, default=50.0,
                        help="Available bankroll in dollars (default: 50)")
    parser.add_argument("--bankers", type=int, default=TIER_SLOTS[2],
                        help=f"Number of 2-pick banker parlays (default: {TIER_SLOTS[2]})")
    parser.add_argument("--core",    type=int, default=TIER_SLOTS[3],
                        help=f"Number of 3-pick core parlays (default: {TIER_SLOTS[3]})")
    parser.add_argument("--big",     type=int, default=TIER_SLOTS[4],
                        help=f"Number of 4-pick parlays (default: {TIER_SLOTS[4]})")
    parser.add_argument("--shooter", type=int, default=TIER_SLOTS[5],
                        help=f"Number of 5-pick shooter parlays (default: {TIER_SLOTS[5]})")
    parser.add_argument("--sports", nargs="+", default=["MLB", "WNBA"],
                        help="Sports to scan (default: MLB WNBA)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't send notifications, just print")
    args = parser.parse_args()

    # Import scanner and run full pipeline
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))

    from scanner_power_parlay import (
        fetch_standard_lines, get_stats_for_pick, score_pick,
        MIN_CONF, MIN_GAMES, _log, _load_nba_def_ratings,
    )
    import time

    _log(f"Fetching lines for: {', '.join(args.sports)}")
    _load_nba_def_ratings()
    all_lines = fetch_standard_lines(args.sports)
    _log(f"Total lines: {len(all_lines)}")

    scored = []
    for i, pick in enumerate(all_lines):
        stats = get_stats_for_pick(pick)
        if stats is None:
            continue
        if stats.get("n_games", 0) < MIN_GAMES:
            continue
        s = score_pick(stats, pick)
        if s["confidence"] >= MIN_CONF:
            scored.append(s)
        if (i + 1) % 20 == 0:
            _log(f"  Scored {i+1}/{len(all_lines)}, {len(scored)} qualified...")
        time.sleep(0.05)

    _log(f"Qualified picks: {len(scored)}")
    scored.sort(key=lambda x: x["confidence"], reverse=True)

    custom_slots = {2: args.bankers, 3: args.core, 4: args.big, 5: args.shooter}
    parlays = run_parlay_plan(
        scored,
        bankroll=args.bankroll,
        tier_slots=custom_slots,
        verbose=True,
    )

    if not args.dry_run and parlays:
        try:
            from notify import send_push
            title, body = format_parlay_ntfy(parlays, args.bankroll)
            send_push(body, title=title)
            _log("Push notification sent.")
        except Exception as e:
            _log(f"Push failed: {e}")
