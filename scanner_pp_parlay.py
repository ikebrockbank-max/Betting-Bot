"""
scanner_pp_parlay.py — Correlated PrizePicks parlay builder.

Reads the latest scanner edges from logs/edges.csv (or runs a fresh scan),
applies PrizePicks value ratings, and suggests optimal 2–3 pick parlays.

Correlation logic:
  - Picks from the SAME team are positively correlated (if team scores a lot,
    all players on that team tend to exceed their props).
  - Picks from OPPOSING teams are negatively correlated (zero-sum game total).
  - Avoid pairing a points OVER with the same player's assists OVER — both rise
    together but Kalshi may have already priced that in.

Parlay scoring:
  Combined probability (geometric mean of individual pick probabilities).
  Bonus for same-team correlation.
  Penalty for mixing OVER and UNDER on same player.

Usage:
  python3 scanner_pp_parlay.py            # prints suggestions + sends email
  from scanner_pp_parlay import run       # call from scheduler
"""

import csv
import itertools
import json
import sys
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from notify import send_push, send_email, _SIMPLE_WRAP, _SIMPLE_CARD, _simple_row

EDGES_CSV   = Path("logs/edges.csv")
LOG_PATH    = Path("logs/parlay_scan.log")

# PrizePicks payout multipliers by pick count (net profit per $1 risked)
PP_PAYOUTS = {2: 3.0, 3: 5.0, 4: 10.0, 5: 20.0, 6: 25.0}

# Break-even hit rate per pick for each parlay size
PP_BREAKEVEN = {2: 0.577, 3: 0.585, 4: 0.562, 5: 0.550, 6: 0.540}

# Min individual pick probability to include in parlay recommendations
MIN_PICK_PROB = 0.57   # at least marginal PP value

# Max picks per parlay (keep simple for best UX)
MAX_PARLAY_SIZE = 3

# Min combined probability for a parlay to be recommended
MIN_PARLAY_PROB = 0.30

# Max parlays to recommend
TOP_N_PARLAYS = 5


def _log(msg: str):
    ts   = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def pp_value_rating(prob: float) -> str:
    if prob >= 0.70: return "Elite"
    if prob >= 0.63: return "Good"
    if prob >= 0.578: return "Marginal"
    return "Skip"


def _load_edges() -> list[dict]:
    """Load most recent edge data from logs/edges.csv."""
    if not EDGES_CSV.exists():
        return []
    rows = []
    try:
        with open(EDGES_CSV) as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception as e:
        _log(f"[warn] Could not read edges.csv: {e}")
    return rows


def _get_recent_edges(hours: int = 2) -> list[dict]:
    """Return edges logged within the last N hours."""
    from datetime import timedelta, timezone
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    rows = _load_edges()
    recent = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                recent.append(r)
        except Exception:
            pass
    return recent


def _run_scanner_for_edges() -> list[dict]:
    """Fall back: run scanner.scan_nba_markets() and read fresh results."""
    _log("No recent edges in CSV — running live scan...")
    try:
        import scanner as sc
        sc.scan_nba_markets()
        return _get_recent_edges(hours=1)
    except Exception as e:
        _log(f"[ERROR] scanner failed: {e}")
        return []


def _parse_pick(row: dict) -> dict | None:
    """
    Parse a CSV edge row into a standardised pick dict.
    Returns None if the pick doesn't have sufficient edge.
    """
    try:
        fair_prob   = float(row.get("fair_prob", 0))
        side        = row.get("best_side", "YES")
        description = row.get("description", "")
        ticker      = row.get("kalshi_ticker", "")
        quality     = float(row.get("edge_quality", 0))
        edge        = float(row.get("best_edge", 0))
        game        = row.get("group_id", "")  # e.g. "JOKICNUGWOL"

        # PP-facing probability: YES side = fair_prob, NO side = 1 - fair_prob
        pp_prob = fair_prob if side == "YES" else (1.0 - fair_prob)

        if pp_prob < MIN_PICK_PROB:
            return None

        rating = pp_value_rating(pp_prob)
        if rating == "Skip":
            return None

        # Extract player name and stat from description
        # e.g. "Nikola Jokić: 30+ points"
        player_name = description.split(":")[0].strip() if ":" in description else description
        stat_desc   = description.split(":", 1)[1].strip() if ":" in description else ""

        # Extract team code from game/group_id (first 3 uppercase letters after player slug)
        # group_id = "JOKICNUGWOL" → we want team from ticker
        # Parse team from ticker: KXNBAPTS-26APR14DENMIN-DENJOKIC1-30
        import re
        team_code = ""
        m = re.match(r"KXNBA\w+?-\d+[A-Z]+\d+([A-Z]{3})([A-Z]{3})-([A-Z]{3})", ticker)
        if m:
            # parts[2] in ticker = player team code prefix in third segment
            team_code = m.group(3)  # first 3 chars of player segment = team code

        # Determine PP direction: YES → OVER (above threshold), NO → UNDER (below)
        pp_direction = "OVER" if side == "YES" else "UNDER"

        return {
            "ticker":       ticker,
            "player":       player_name,
            "stat":         stat_desc,
            "game":         game,
            "team_code":    team_code,
            "side":         side,
            "pp_direction": pp_direction,
            "fair_prob":    round(fair_prob, 4),
            "pp_prob":      round(pp_prob, 4),
            "edge":         round(edge, 4),
            "quality":      round(quality, 1),
            "rating":       rating,
            "description":  description,
        }
    except Exception:
        return None


def _correlation_bonus(picks: list[dict]) -> float:
    """
    Returns a correlation multiplier (>1 = positive, <1 = negative).
    Same-team stacks get a small boost; mixed directions on same team are penalised.
    """
    if len(picks) < 2:
        return 1.0

    teams = [p["team_code"] for p in picks]
    directions = [p["pp_direction"] for p in picks]

    # Same team, same direction (e.g. both OVERs) = positive correlation
    # This is the "stack" — if the team has a big game, all OVERs hit together
    team_counts = {}
    for t, d in zip(teams, directions):
        team_counts.setdefault(t, []).append(d)

    bonus = 1.0
    for t, dirs in team_counts.items():
        if len(dirs) >= 2:
            all_over  = all(d == "OVER" for d in dirs)
            all_under = all(d == "UNDER" for d in dirs)
            if all_over:
                bonus *= 1.05   # same-team OVERs: slight boost
            elif all_under:
                bonus *= 1.03   # same-team UNDERs: smaller boost (team blowouts are rare)
            else:
                bonus *= 0.95   # mixed OVERs/UNDERs on same team cancel out

    return bonus


def _parlay_ev(picks: list[dict]) -> dict:
    """
    Calculate parlay metrics:
      combined_prob: geometric product of individual probabilities (with correlation bonus)
      payout:        PP payout multiplier for this pick count
      ev:            expected profit per $1 risked
      label:         human-readable description
    """
    n = len(picks)
    payout = PP_PAYOUTS.get(n, 3.0)

    base_prob = 1.0
    for p in picks:
        base_prob *= p["pp_prob"]

    corr = _correlation_bonus(picks)
    combined = round(base_prob * corr, 4)

    ev = round(combined * payout - 1.0, 4)   # profit per $1 risked

    # Parlay label
    label_parts = []
    for p in picks:
        arrow = "↑" if p["pp_direction"] == "OVER" else "↓"
        label_parts.append(f"{p['player']} {p['stat']} {arrow}")
    label = " + ".join(label_parts)

    ratings = [p["rating"] for p in picks]
    rating_summary = " / ".join(ratings)

    return {
        "picks":        picks,
        "n":            n,
        "combined_prob": combined,
        "payout":       payout,
        "ev":           ev,
        "label":        label,
        "rating_summary": rating_summary,
        "has_elite":    any(p["rating"] == "Elite" for p in picks),
        "all_good_plus": all(p["rating"] in ("Elite", "Good") for p in picks),
    }


def build_parlays(picks: list[dict], max_size: int = MAX_PARLAY_SIZE) -> list[dict]:
    """
    Generate all 2- and 3-pick parlay combinations from valid picks.
    Filter by minimum combined probability.
    Sort by EV descending.
    """
    parlays = []

    # Deduplicate: take best pick per player (highest pp_prob)
    best_by_player: dict[str, dict] = {}
    for p in picks:
        key = p["player"].lower()
        if key not in best_by_player or p["pp_prob"] > best_by_player[key]["pp_prob"]:
            best_by_player[key] = p
    deduped = list(best_by_player.values())

    # Sort by pp_prob desc for faster pruning
    deduped.sort(key=lambda p: -p["pp_prob"])

    for size in range(2, max_size + 1):
        for combo in itertools.combinations(deduped, size):
            parlay = _parlay_ev(list(combo))
            if parlay["combined_prob"] >= MIN_PARLAY_PROB:
                parlays.append(parlay)

    # Sort by: (1) has elite pick, (2) all_good_plus, (3) EV
    parlays.sort(key=lambda p: (
        -int(p["has_elite"]),
        -int(p["all_good_plus"]),
        -p["ev"],
    ))

    return parlays[:TOP_N_PARLAYS * 3]   # keep more for filtering later


def _format_parlay_email(parlays: list[dict], all_picks: list[dict]) -> tuple[str, str, str]:
    """Build the parlay suggestion email."""
    count = len(parlays)
    subject = f"🎯 PP Parlay Builder — {count} Suggested Parlay{'s' if count > 1 else ''}"

    ts = datetime.now(UTC).strftime("%b %d %Y %H:%M UTC")

    # Best parlay card
    cards_html = ""
    plain_lines = [subject, ""]

    for i, parlay in enumerate(parlays[:TOP_N_PARLAYS]):
        rank    = ["🥇 Best Parlay", "🥈 2nd Pick", "🥉 3rd Pick",
                   "4th Option", "5th Option"][i]
        prob_pct = f"{parlay['combined_prob']*100:.1f}%"
        ev_str   = f"+{parlay['ev']*100:.1f}¢ per $1" if parlay["ev"] > 0 else f"{parlay['ev']*100:.1f}¢ per $1"
        payout   = f"{parlay['payout']:.0f}x"
        n        = parlay["n"]

        rows = _simple_row("Payout", payout, "#059669") \
             + _simple_row("Win probability", prob_pct, "#2563eb") \
             + _simple_row("Expected value", ev_str, "#059669" if parlay["ev"] > 0 else "#dc2626") \
             + _simple_row("Pick ratings", parlay["rating_summary"], "#374151")

        pick_rows = ""
        for j, p in enumerate(parlay["picks"], 1):
            arrow = "↑ OVER" if p["pp_direction"] == "OVER" else "↓ UNDER"
            pick_rows += _simple_row(
                f"Pick {j}: {p['player']}",
                f"{p['stat']} {arrow} ({p['pp_prob']*100:.1f}% — {p['rating']})",
                "#059669" if p["rating"] in ("Elite", "Good") else "#374151",
            )

        accent = "#059669" if i == 0 else ("#2563eb" if i == 1 else "#7c3aed")
        cards_html += _SIMPLE_CARD.format(
            accent=accent,
            action=f"{rank} — {n}-Pick Parlay",
            subtitle=parlay["label"][:70],
            rows=rows + pick_rows,
        )

        plain_lines += [
            f"{rank} ({n} picks, {prob_pct} win prob, {payout} payout):",
        ]
        for p in parlay["picks"]:
            arrow = "OVER" if p["pp_direction"] == "OVER" else "UNDER"
            plain_lines.append(
                f"  • {p['player']} {p['stat']} {arrow} — {p['pp_prob']*100:.1f}% ({p['rating']})"
            )
        plain_lines.append(f"  EV: {ev_str}")
        plain_lines.append("")

    # All valid picks summary
    picks_rows = ""
    plain_lines += ["All valid picks:", ""]
    for p in sorted(all_picks, key=lambda x: -x["pp_prob"])[:10]:
        arrow = "↑" if p["pp_direction"] == "OVER" else "↓"
        picks_rows += _simple_row(
            f"{p['player']} — {p['stat']} {arrow}",
            f"{p['pp_prob']*100:.1f}% ({p['rating']})",
            "#059669" if p["rating"] in ("Elite", "Good") else "#374151",
        )
        plain_lines.append(f"  {p['player']} {p['stat']} {arrow} — {p['pp_prob']*100:.1f}% ({p['rating']})")

    if picks_rows:
        cards_html += _SIMPLE_CARD.format(
            accent="#374151",
            action="All Valid Picks Today",
            subtitle="Sorted by PP probability (highest = best OVER/UNDER edge)",
            rows=picks_rows,
        )

    html = _SIMPLE_WRAP.format(
        header_color="#059669",
        header_title="🎯 PrizePicks Parlay Builder",
        header_sub=f"Kalshi-calibrated picks • {ts}",
        body=cards_html,
    )

    return subject, html, "\n".join(plain_lines)


def run(send_notifications: bool = True) -> list[dict]:
    """
    Main entry point. Loads recent edges, builds parlays, sends notifications.
    Returns list of suggested parlays.
    """
    _log("=== PP Parlay scan started ===")

    # Load recent edges (or run fresh scan if stale)
    edges = _get_recent_edges(hours=2)
    if not edges:
        edges = _run_scanner_for_edges()

    if not edges:
        _log("No edge data available — cannot build parlays.")
        return []

    _log(f"Loaded {len(edges)} edge rows")

    # Parse into picks
    picks = []
    for row in edges:
        p = _parse_pick(row)
        if p:
            picks.append(p)

    _log(f"{len(picks)} valid PP picks (prob >= {MIN_PICK_PROB:.1%})")
    for p in sorted(picks, key=lambda x: -x["pp_prob"])[:8]:
        _log(f"  {p['rating']:8s} {p['player']:<28s} {p['stat']:<20s} "
             f"{p['pp_direction']} {p['pp_prob']*100:.1f}%")

    if not picks:
        _log("No picks meet PP threshold — no parlays to build.")
        return []

    # Build parlays
    parlays = build_parlays(picks)
    _log(f"Built {len(parlays)} parlay combinations")

    if not parlays:
        _log("No profitable parlays found (min combined prob not met).")
        return []

    # Print top picks
    _log("Top parlay suggestions:")
    for i, parlay in enumerate(parlays[:TOP_N_PARLAYS], 1):
        _log(
            f"  #{i} ({parlay['n']} picks) {parlay['combined_prob']*100:.1f}% win "
            f"| {parlay['payout']:.0f}x | EV={parlay['ev']:+.3f} | {parlay['label'][:60]}"
        )

    # Notifications
    if send_notifications:
        top = parlays[0]
        push_msg = (
            f"Best parlay: {top['label'][:60]} | "
            f"{top['combined_prob']*100:.1f}% win / {top['payout']:.0f}x payout"
        )
        send_push(push_msg, title="🎯 PP Parlay Builder")
        _log(f"Push sent: {push_msg[:100]}")

        try:
            subj, html, plain = _format_parlay_email(parlays, picks)
            send_email(subj, html, plain)
            _log(f"Email sent: {subj}")
        except Exception as e:
            _log(f"Email error (non-fatal): {e}")

    _log(f"=== PP Parlay scan complete — {len(parlays)} parlays ===")
    return parlays


if __name__ == "__main__":
    run()
