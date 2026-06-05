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
ELITE_P_HIT    = 0.72   # picks above this can appear in 2 parlays (truly elite signal)
MAX_RISK_PCT   = 0.80   # never allocate more than 80% of bankroll across all parlays
POOL_LIMIT     = 25     # only consider top-N picks by p_hit when generating combos

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


def _get_p_hit(pick: dict) -> float:
    """Best available P(hit) estimate for a single leg."""
    direction = pick.get("direction", "OVER")
    p_over  = pick.get("p_over")
    p_under = pick.get("p_under")
    if direction == "OVER"  and p_over  and 0.05 < p_over  < 0.99:
        return p_over
    if direction == "UNDER" and p_under and 0.05 < p_under < 0.99:
        return p_under
    return pick.get("confidence", 0.62)


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
    raw = bankroll * full_kelly * frac
    return max(MIN_BET, min(MAX_BET, round(raw, 2)))


def _correlation_factor(combo: list[dict]) -> float:
    """
    Estimate correlation adjustment for a combination of picks.

    Tries the full correlation model first; falls back to a simple
    same-game / same-team heuristic.

    Returns a multiplier: < 1.0 = correlated (reduce p_win), > 1.0 = anti-correlated bonus.
    """
    try:
        from data.mlb_game_state import joint_game_correlation_factor as _jcf
        return _jcf(combo)
    except Exception:
        pass

    # Simple heuristic fallback
    game_ids = [p.get("game_id", "") for p in combo if p.get("game_id")]
    if len(game_ids) != len(set(game_ids)):  # any duplicate game_id = same game
        return 0.90
    return 1.0


def _combo_ev(combo: tuple, corr_factor: float) -> float:
    """EV = p_win × payout - 1, where p_win is correlation-adjusted joint probability."""
    n       = len(combo)
    payout  = PP_PAYOUTS.get(n, 20.0)
    p_indep = math.prod(_get_p_hit(leg) for leg in combo)
    p_win   = p_indep * corr_factor
    return p_win * payout - 1.0, p_win, p_indep, corr_factor


def build_diverse_parlays(
    scored_picks: list[dict],
    bankroll: float = 30.0,
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

    # Filter eligible picks
    eligible = [
        p for p in scored_picks
        if p.get("confidence", 0) >= 0.62 and _get_p_hit(p) >= 0.55
    ]
    eligible.sort(key=_get_p_hit, reverse=True)
    pool = eligible[:POOL_LIMIT]

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


def format_parlay_ntfy(parlays: list[dict], bankroll: float) -> tuple[str, str]:
    """
    Returns (title, body) for ntfy push notification.
    Human-readable format — designed for phone screen.
    Plain English, no jargon. Shows stat type on every leg.
    """
    if not parlays:
        return "No parlays found", "No qualifying parlays today."

    total_bet = sum(p["bet_size"] for p in parlays)
    total_win = sum(p["win_amount"] for p in parlays)

    title = f"🎯 {len(parlays)} parlays today — risk ${total_bet:.0f}, max win ${total_win:.0f}"

    lines = []
    for i, par in enumerate(parlays, 1):
        bet     = par["bet_size"]
        win     = par["win_amount"]
        p_win   = int(par["p_win"] * 100)
        n_legs  = par["n_legs"]
        payout  = int(par["payout"])

        lines.append(f"── Parlay {i}: ${bet:.0f} bet → ${win:.0f} if all {n_legs} hit ({p_win}% chance) ──")
        for leg in par["leg_summary"]:
            direction  = leg["direction"]
            line       = leg["line"]
            stat       = leg["stat_type"]
            player     = leg["player"]
            hit_pct    = int(leg["hit_rate"] * 100)
            avg        = leg["avg"]
            arrow      = "↑" if direction == "OVER" else "↓"
            lines.append(f"  {arrow} {player} — {direction} {line} {stat}  ({hit_pct}% hit rate, avg {avg})")
        lines.append("")

    body = "\n".join(lines)
    return title, body


def run_parlay_plan(
    scored_picks: list[dict],
    bankroll: float = 30.0,
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


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Parlay portfolio builder + Kelly sizer")
    parser.add_argument("--bankroll", type=float, default=30.0,
                        help="Available bankroll in dollars (default: 30)")
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
