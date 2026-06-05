"""
parlay_builder.py — Diversified parlay portfolio optimizer with Kelly bankroll sizing.

Takes scored picks from scanner_power_parlay and builds a slate of diverse parlays
with recommended bet sizes for a given bankroll.

Design principles:
  - Diversity: max 1 player shared between any two parlays (one miss ≠ all parlays dead)
  - Concentration: when a pick is truly elite (p_hit ≥ 0.72), allow it in 2 parlays
  - Kelly sizing: fractional (25%) Kelly per parlay based on p_win × payout
  - Budget: total allocation ≤ bankroll, min $1 per parlay, max $10 per parlay
  - Transparency: every parlay shows exact why, EV, and Kelly math

PrizePicks Power Play payouts (all-or-nothing):
  2-pick = 3x | 3-pick = 5x | 4-pick = 10x | 5-pick = 20x

Kelly formula per parlay:
  b = payout_multiple - 1  (net payout on $1)
  f* = (b × p_win - (1 - p_win)) / b
  bet = bankroll × f* × kelly_fraction
"""

import itertools
import math
from typing import Optional

# ── Constants ──────────────────────────────────────────────────────────────────
PP_PAYOUTS = {2: 3.0, 3: 5.0, 4: 10.0, 5: 20.0}

KELLY_FRACTION = 0.25   # 25% fractional Kelly — conservative for high-variance parlays
MAX_PARLAYS    = 6      # max parlays to recommend
MIN_EV         = 0.03   # min 3% EV to include a parlay
MIN_P_WIN      = 0.25   # min parlay win probability (prevents degenerate long shots)
MAX_OVERLAP    = 1      # max players shared between any two parlays
ELITE_P_HIT    = 0.72   # picks above this can appear in 2 parlays (truly elite signal)
MIN_BET        = 1.00   # min bet size in dollars
MAX_BET        = 10.00  # max bet size in dollars (hard cap for small bankrolls)
MAX_RISK_PCT   = 0.90   # never allocate more than 90% of bankroll across all parlays
POOL_LIMIT     = 25     # only consider top-N picks by p_hit when generating combos


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
    n_parlays: int = MAX_PARLAYS,
    max_overlap: int = MAX_OVERLAP,
    kelly_frac: float = KELLY_FRACTION,
) -> list[dict]:
    """
    Build a diversified slate of parlays from scored picks.

    Algorithm:
      1. Score all valid combinations (2–5 legs, top-POOL_LIMIT picks)
      2. Rank by adjusted EV (post-correlation)
      3. Greedily select parlays while enforcing diversity:
         - No player appears in more than 2 selected parlays total
         - No two parlays share more than max_overlap players
         - Elite picks (p_hit ≥ ELITE_P_HIT) exempt from single-parlay limit
      4. Size each parlay with fractional Kelly, capped at MAX_BET
      5. Scale down proportionally if total > MAX_RISK_PCT × bankroll

    Returns list of parlay dicts with 'bet_size' and 'kelly_math' added.
    """
    if not scored_picks:
        return []

    # Filter to picks that pass individual confidence threshold
    eligible = [
        p for p in scored_picks
        if p.get("confidence", 0) >= 0.62 and _get_p_hit(p) >= 0.55
    ]
    eligible.sort(key=_get_p_hit, reverse=True)
    pool = eligible[:POOL_LIMIT]

    if len(pool) < 2:
        return []

    # ── Step 1: Score all combinations ───────────────────────────────────────
    candidates = []
    for n_legs in range(2, min(6, len(pool) + 1)):
        payout = PP_PAYOUTS.get(n_legs, 20.0)
        for combo in itertools.combinations(pool, n_legs):
            # Skip if any two legs are from the exact same player (different stats OK,
            # but we'll treat as separate legs — this is fine, just keep track)
            corr  = _correlation_factor(list(combo))
            ev, p_win, p_indep, _ = _combo_ev(combo, corr)
            if ev < MIN_EV:
                continue
            if p_win < MIN_P_WIN:
                continue
            candidates.append({
                "combo":   combo,
                "n_legs":  n_legs,
                "payout":  payout,
                "p_win":   round(p_win, 4),
                "p_indep": round(p_indep, 4),
                "corr":    round(corr, 4),
                "ev":      round(ev, 4),
                "ev_pct":  int(ev * 100),
                "ev_rating": (
                    "🔥 HIGH" if ev >= 0.20 else
                    "✅ MED-HIGH" if ev >= 0.10 else
                    "🟡 MED" if ev >= 0.05 else "⚠️ LOW"
                ),
            })

    # Rank by EV descending
    candidates.sort(key=lambda x: x["ev"], reverse=True)

    # ── Step 2: Diversity-filtered selection ─────────────────────────────────
    selected     = []
    player_count: dict[str, int] = {}  # how many selected parlays contain this player
    parlay_players: list[set] = []     # set of player names per selected parlay

    for cand in candidates:
        if len(selected) >= n_parlays:
            break

        combo        = cand["combo"]
        this_players = {leg["player"] for leg in combo}

        # Check overlap with every already-selected parlay
        too_similar = False
        for existing_players in parlay_players:
            shared = this_players & existing_players
            if len(shared) > max_overlap:
                too_similar = True
                break

        if too_similar:
            continue

        # Check per-player appearance limit
        violates_limit = False
        for player in this_players:
            p_hit = _get_p_hit(next(leg for leg in combo if leg["player"] == player))
            limit = 2 if p_hit >= ELITE_P_HIT else 1
            if player_count.get(player, 0) >= limit:
                violates_limit = True
                break

        if violates_limit:
            continue

        # Passed all filters — select this parlay
        selected.append(cand)
        parlay_players.append(this_players)
        for player in this_players:
            player_count[player] = player_count.get(player, 0) + 1

    # ── Step 3: Kelly sizing ──────────────────────────────────────────────────
    for cand in selected:
        raw_size = kelly_size(cand["p_win"], cand["payout"], bankroll, kelly_frac)
        b        = cand["payout"] - 1.0
        full_k   = (b * cand["p_win"] - (1 - cand["p_win"])) / b
        cand["kelly_full_pct"]  = round(full_k * 100, 1)
        cand["kelly_frac_pct"]  = round(full_k * kelly_frac * 100, 1)
        cand["bet_raw"]         = raw_size
        cand["win_amount_raw"]  = round(raw_size * cand["payout"], 2)

    # ── Step 4: Budget scaling ────────────────────────────────────────────────
    total_raw = sum(c["bet_raw"] for c in selected)
    max_total = bankroll * MAX_RISK_PCT
    if total_raw > max_total and total_raw > 0:
        scale = max_total / total_raw
        for cand in selected:
            cand["bet_raw"] = max(MIN_BET, round(cand["bet_raw"] * scale, 2))

    # Round bets to $0.50 increments (PrizePicks minimum is $5, but you can
    # stack smaller amounts; round to nearest practical unit)
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
    """Round bet to nearest $0.50; ensure it's at least $1 and at most MAX_BET."""
    rounded = round(amount * 2) / 2  # nearest $0.50
    return max(MIN_BET, min(MAX_BET, rounded))


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
    total_bet  = sum(p["bet_size"] for p in parlays)
    total_win  = sum(p["win_amount"] for p in parlays)
    # Expected value: EV per parlay = (p_win × payout - 1) × bet_size
    total_ev   = sum(p["ev"] * p["bet_size"] for p in parlays)

    lines.append(f"💰 PARLAY PORTFOLIO — ${bankroll:.0f} bankroll")
    lines.append(f"   Risking ${total_bet:.2f} across {len(parlays)} parlays")
    lines.append(f"   Expected value: +${total_ev:.2f} | Max win: ${total_win:.2f}")
    lines.append("")

    sport_emoji = {"MLB": "⚾", "WNBA": "🏀", "NBA": "🏀", "NHL": "🏒",
                   "TENNIS": "🎾", "SOCCER": "⚽"}

    for i, par in enumerate(parlays, 1):
        payout     = par["payout"]
        p_win_pct  = int(par["p_win"] * 100)
        ev_pct     = par["ev_pct"]
        bet        = par["bet_size"]
        win_amt    = par["win_amount"]
        net        = par["net_profit"]
        ev_tag     = par["ev_rating"]
        corr       = par["corr"]

        lines.append(
            f"━━━ Parlay {i}  ·  {par['n_legs']}-pick  ·  {payout:.0f}x  ·  {ev_tag} ━━━"
        )
        lines.append(f"   P(win): {p_win_pct}%  |  EV: +{ev_pct}%  |  Bet: ${bet:.2f}  →  Win: ${win_amt:.2f} (+${net:.2f})")

        # Correlation note
        if corr < 0.94:
            lines.append(f"   ⚠️  Same-game correlation: ×{corr:.2f} applied to p_win")
        elif corr > 1.03:
            lines.append(f"   ✅  Lineup correlation bonus: ×{corr:.2f}")

        lines.append("")

        for leg in par["leg_summary"]:
            e     = sport_emoji.get(leg["sport"], "🎯")
            arrow = "📈" if leg["direction"] == "OVER" else "📉"
            src   = "📊" if leg["p_src"] == "model" else "🔢"
            lines.append(
                f"   {e}{arrow} {leg['player']}  {leg['direction']} {leg['line']} {leg['stat_type']}"
            )
            lines.append(
                f"      {src} P(hit)={leg['p_hit_pct']}%  ·  {int(leg['hit_rate']*100)}% HR  ·  avg {leg['avg']}  ·  L5: {leg['recent_5']}"
            )

        # Kelly math transparency
        lines.append("")
        lines.append(
            f"   Kelly: full={par['kelly_full_pct']}% → 25% fractional={par['kelly_frac_pct']}% → ${bet:.2f}"
        )
        lines.append("")

    lines.append("─" * 50)
    lines.append(f"TOTAL RISK: ${total_bet:.2f} / ${bankroll:.0f} ({total_bet/bankroll*100:.0f}%)")
    lines.append(f"EXPECTED RETURN: +${total_ev:.2f}  (EV on risk: +{total_ev/total_bet*100:.1f}%)")
    lines.append("")

    # Standalone top singles (high confidence but no parlay)
    if top_singles:
        lines.append("BEST STANDALONE EDGES (single picks, no parlay sizing):")
        for s in top_singles[:5]:
            e     = sport_emoji.get(s["sport"], "🎯")
            arrow = "📈" if s["direction"] == "OVER" else "📉"
            lines.append(
                f"  {e}{arrow} {s['player']} {s['direction']} {s['line']} {s['stat_type']} "
                f"— {s['conf_pct']}% conf · {int(s.get('hit_rate',0)*100)}% HR"
            )

    return "\n".join(lines)


def format_parlay_ntfy(parlays: list[dict], bankroll: float) -> tuple[str, str]:
    """
    Returns (title, body) for ntfy push notification.
    Compact format — designed for phone screen.
    """
    if not parlays:
        return "No parlays found", "No qualifying parlays today."

    total_bet = sum(p["bet_size"] for p in parlays)
    total_win = sum(p["win_amount"] for p in parlays)
    total_ev  = sum(p["ev"] * p["bet_size"] for p in parlays)

    title = (
        f"🎯 {len(parlays)} parlays | Risk ${total_bet:.0f} | Max ${total_win:.0f} | EV +${total_ev:.2f}"
    )

    lines = []
    for i, par in enumerate(parlays, 1):
        legs_str = " + ".join(
            f"{l['player'].split()[-1]} {l['direction']} {l['line']}"
            for l in par["leg_summary"]
        )
        lines.append(
            f"[{i}] ${par['bet_size']:.0f}→${par['win_amount']:.0f} | {par['n_legs']}pk {par['payout']:.0f}x | "
            f"P={int(par['p_win']*100)}% EV+{par['ev_pct']}%\n"
            f"    {legs_str}"
        )

    body = "\n".join(lines)
    return title, body


def run_parlay_plan(
    scored_picks: list[dict],
    bankroll: float = 30.0,
    kelly_frac: float = KELLY_FRACTION,
    n_parlays: int = MAX_PARLAYS,
    verbose: bool = True,
) -> list[dict]:
    """
    Main entry point: build + size + print parlays.

    Args:
        scored_picks: output of scanner_power_parlay.score_pick()
        bankroll:     total dollars available to bet
        kelly_frac:   fraction of full Kelly to use (default 0.25)
        n_parlays:    max parlays to recommend
        verbose:      print the plan to stdout

    Returns:
        List of parlay dicts with 'bet_size', 'win_amount', 'ev', etc.
    """
    parlays = build_diverse_parlays(
        scored_picks,
        bankroll=bankroll,
        n_parlays=n_parlays,
        kelly_frac=kelly_frac,
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
    parser.add_argument("--kelly", type=float, default=KELLY_FRACTION,
                        help=f"Kelly fraction (default: {KELLY_FRACTION})")
    parser.add_argument("--parlays", type=int, default=MAX_PARLAYS,
                        help=f"Max parlays (default: {MAX_PARLAYS})")
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

    parlays = run_parlay_plan(
        scored,
        bankroll=args.bankroll,
        kelly_frac=args.kelly,
        n_parlays=args.parlays,
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
