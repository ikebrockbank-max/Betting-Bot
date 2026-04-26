"""
scanner_parlayplay.py — ParlayPlay bug & value detector.

ParlayPlay sets both a Less and More multiplier for every line. Bugs appear as:

1. MONOTONICITY VIOLATIONS
   As line_score increases:
     - more_mult must be non-decreasing (going over a higher line is harder → more payout)
     - less_mult must be non-increasing (going under a higher line is easier → less payout)
   Any reversal = mispriced line.

2. PROMO VALUE BUGS (🔥 lines)
   A 🔥-boosted line sometimes has a MORE or LESS multiplier higher than a
   neighbouring harder line — that's free value you can exploit before it's fixed.

3. CROSS-PLATFORM LINE GAP (vs. PrizePicks)
   When ParlayPlay's standard line for (player, stat) is significantly different from
   PrizePicks' standard line for the same player/stat, one platform has an edge:
     - PP std < PP std → bet MORE on ParlayPlay (easier line)
     - PP std > PP std → bet LESS on ParlayPlay (harder line... or bet MORE on PP)
"""

from __future__ import annotations
import unicodedata

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_MONO_JUMP   = 0.15   # mult must jump by at least this much to flag a reversal
MIN_PROMO_EDGE  = 0.20   # promo mult must exceed non-promo by this much to flag
MIN_LINE_GAP    = 1.5    # min absolute gap between PP and ParlayPlay standard lines
MIN_LINE_GAP_PCT = 0.08  # min % gap relative to PP line (both must be met)

# ── PP stat → ParlayPlay stat name mapping ────────────────────────────────────
PP_TO_PP_STAT: dict[str, str] = {
    "Points":                   "Points",
    "Rebounds":                 "Rebounds",
    "Assists":                  "Assists",
    "3-Pointers Made":          "3PT Made",
    "3-PT Made":                "3PT Made",
    "Pts+Reb+Ast":              "Pts + Reb + Ast",
    "Points+Rebounds+Assists":  "Pts + Reb + Ast",
    "Points+Rebounds":          "Pts + Reb",
    "Points+Assists":           "Pts + Ast",
    "Rebounds+Assists":         "Reb + Ast",
    "Blocks":                   "Blocks",
    "Steals":                   "Steals",
    "Turnovers":                None,  # not available on ParlayPlay
    "Fantasy Score":            "Fantasy Points",
}


def _normalize(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_ = nfkd.encode("ascii", "ignore").decode("ascii")
    return ascii_.lower().strip().replace(".", "").replace("-", " ").replace("'", "")


# ── Bug detectors ─────────────────────────────────────────────────────────────

def find_monotonicity_bugs(grouped: dict) -> list[dict]:
    """
    Detect lines where multipliers violate the expected monotonicity.

    As line_score increases:
      - more_mult should increase (going OVER is harder)
      - less_mult should decrease (going UNDER is easier)

    Returns list of bug dicts, sorted by severity (mult_reversal desc).
    """
    bugs = []
    for (player, stat), info in grouped.items():
        lines = [l for l in info["lines"]
                 if l["less_mult"] is not None and l["more_mult"] is not None]
        lines.sort(key=lambda l: l["line_score"])

        for i in range(1, len(lines)):
            prev, curr = lines[i - 1], lines[i]

            # more_mult should be >= prev more_mult
            if curr["more_mult"] < prev["more_mult"] - MIN_MONO_JUMP:
                bugs.append({
                    "bug_type":    "more_mult_reversal",
                    "player":      player,
                    "stat":        stat,
                    "standard":    info["standard"],
                    "line_low":    prev["line_score"],
                    "line_high":   curr["line_score"],
                    "mult_low":    prev["more_mult"],
                    "mult_high":   curr["more_mult"],
                    "reversal":    round(prev["more_mult"] - curr["more_mult"], 2),
                    "direction":   "MORE",
                    "action":      (
                        f"BET MORE {curr['line_score']} "
                        f"({curr['more_mult']}x) — higher line should pay MORE, "
                        f"but pays LESS than {prev['line_score']} ({prev['more_mult']}x)"
                    ),
                })

            # less_mult should be <= prev less_mult
            if curr["less_mult"] > prev["less_mult"] + MIN_MONO_JUMP:
                bugs.append({
                    "bug_type":    "less_mult_reversal",
                    "player":      player,
                    "stat":        stat,
                    "standard":    info["standard"],
                    "line_low":    prev["line_score"],
                    "line_high":   curr["line_score"],
                    "mult_low":    prev["less_mult"],
                    "mult_high":   curr["less_mult"],
                    "reversal":    round(curr["less_mult"] - prev["less_mult"], 2),
                    "direction":   "LESS",
                    "action":      (
                        f"BET LESS {prev['line_score']} "
                        f"({prev['less_mult']}x) — lower line should pay LESS for Less, "
                        f"but pays MORE than {curr['line_score']} ({curr['less_mult']}x)"
                    ),
                })

    bugs.sort(key=lambda b: -b["reversal"])
    return bugs


def find_promo_value_bugs(grouped: dict) -> list[dict]:
    """
    Detect 🔥 promo lines whose multiplier exceeds a neighbouring harder line's mult.
    These represent genuine value — the promo is offering a higher payout on what
    should be an easier pick.
    """
    bugs = []
    for (player, stat), info in grouped.items():
        lines = info["lines"]
        lines_sorted = sorted(lines, key=lambda l: l["line_score"])

        for ln in lines_sorted:
            if not (ln["is_promo_more"] or ln["is_promo_less"]):
                continue  # not a promo line

            # Check if this promo's MORE mult exceeds a harder (higher) line's MORE mult
            if ln["is_promo_more"] and ln["more_mult"]:
                harder_lines = [l for l in lines_sorted
                                if l["line_score"] > ln["line_score"]
                                and l["more_mult"] is not None
                                and not l["is_promo_more"]]
                for hard in harder_lines[:2]:
                    edge = ln["more_mult"] - hard["more_mult"]
                    if edge >= MIN_PROMO_EDGE:
                        bugs.append({
                            "bug_type":    "promo_more_exceeds_harder",
                            "player":      player,
                            "stat":        stat,
                            "promo_line":  ln["line_score"],
                            "promo_mult":  ln["more_mult"],
                            "hard_line":   hard["line_score"],
                            "hard_mult":   hard["more_mult"],
                            "edge":        round(edge, 2),
                            "action":      (
                                f"🔥 BET MORE {ln['line_score']} "
                                f"({ln['more_mult']}x) — promo pays MORE than "
                                f"harder line {hard['line_score']} ({hard['more_mult']}x)"
                            ),
                        })

            # Check if this promo's LESS mult exceeds an easier (lower) line's LESS mult
            if ln["is_promo_less"] and ln["less_mult"]:
                easier_lines = [l for l in lines_sorted
                                if l["line_score"] < ln["line_score"]
                                and l["less_mult"] is not None
                                and not l["is_promo_less"]]
                for easy in easier_lines[-2:]:
                    edge = ln["less_mult"] - easy["less_mult"]
                    if edge >= MIN_PROMO_EDGE:
                        bugs.append({
                            "bug_type":    "promo_less_exceeds_easier",
                            "player":      player,
                            "stat":        stat,
                            "promo_line":  ln["line_score"],
                            "promo_mult":  ln["less_mult"],
                            "easy_line":   easy["line_score"],
                            "easy_mult":   easy["less_mult"],
                            "edge":        round(edge, 2),
                            "action":      (
                                f"🔥 BET LESS {ln['line_score']} "
                                f"({ln['less_mult']}x) — promo Less pays MORE than "
                                f"easier Less line {easy['line_score']} ({easy['less_mult']}x)"
                            ),
                        })

    bugs.sort(key=lambda b: -b["edge"])
    return bugs


def find_cross_platform_edges(
    pp_groups: dict,
    parlayplay_grouped: dict,
) -> list[dict]:
    """
    Compare PrizePicks standard lines to ParlayPlay standard lines.
    A significant gap means one platform has an exploitable edge.

    Parameters
    ----------
    pp_groups : output of scanner_bugs._group_lines()
                Keys: (player_name, stat_type, game_id, league_id)
                Values: {standard, demon[], goblin[], league_id, start_time, ...}
    parlayplay_grouped : output of data.parlayplay.get_grouped_lines()[0]
                Keys: (player_name, stat)
                Values: {standard, lines: [...]}
    """
    # Full-game league IDs only — sub-leagues (1H, 1Q, etc.) have fractional lines
    # that can't be meaningfully compared to ParlayPlay's full-game lines.
    # 7=NBA, 237=NBA Playoffs, 2=MLB, 8=NHL, 9=NFL, 3=WNBA, 252=WNBA alt
    FULL_GAME_LEAGUES = {"7", "237", "2", "8", "9", "3", "252",
                         7, 237, 2, 8, 9, 3, 252}  # handle str and int IDs

    # Build PP lookup: normalized_name → {pp_stat → standard_line}
    pp_lookup: dict[str, dict[str, float]] = {}
    for key, info in pp_groups.items():
        std = info.get("standard")
        if std is None:
            continue
        player   = key[0]  # (player_name, stat_type, game_id, league_id)
        stat     = key[1]
        league_id = key[3]
        if league_id not in FULL_GAME_LEAGUES:
            continue  # skip sub-league lines (1H, 1Q, etc.)
        norm   = _normalize(player)
        # Keep first seen (could later prefer most recent, but first is fine for daily scan)
        existing = pp_lookup.setdefault(norm, {}).get(stat)
        if existing is None:
            pp_lookup[norm][stat] = std

    # ParlayPlay stat → PP stat reverse map
    plp_to_pp: dict[str, list[str]] = {}
    for pp_s, plp_s in PP_TO_PP_STAT.items():
        if plp_s:
            plp_to_pp.setdefault(plp_s, []).append(pp_s)

    edges = []
    for (plp_player, plp_stat), info in parlayplay_grouped.items():
        std_plp = info["standard"]
        if std_plp is None:
            continue

        norm_name = _normalize(plp_player)
        pp_stats = plp_to_pp.get(plp_stat, [plp_stat])

        pp_line, matched_pp_stat = None, None
        for pp_s in pp_stats:
            v = pp_lookup.get(norm_name, {}).get(pp_s)
            if v is not None:
                pp_line, matched_pp_stat = v, pp_s
                break

        if pp_line is None:
            continue

        gap     = std_plp - pp_line      # positive = ParlayPlay line is higher
        abs_gap = abs(gap)
        pct_gap = abs_gap / pp_line if pp_line > 0 else 0

        if abs_gap < MIN_LINE_GAP or pct_gap < MIN_LINE_GAP_PCT:
            continue

        std_ln = next((l for l in info["lines"] if l["line_score"] == std_plp), None)

        if gap > 0:
            # ParlayPlay line higher → LESS on ParlayPlay (or MORE on PP)
            action = (
                f"BET LESS on ParlayPlay {plp_stat} {std_plp} "
                f"OR BET MORE on PP {matched_pp_stat} {pp_line}"
            )
            direction = "parlayplay_higher"
        else:
            # ParlayPlay line lower → MORE on ParlayPlay (or LESS on PP)
            action = (
                f"BET MORE on ParlayPlay {plp_stat} {std_plp} "
                f"OR BET LESS on PP {matched_pp_stat} {pp_line}"
            )
            direction = "parlayplay_lower"

        edges.append({
            "bug_type":        "cross_platform_edge",
            "player":          plp_player,
            "stat":            plp_stat,
            "pp_line":         pp_line,
            "parlayplay_line": std_plp,
            "gap":             round(gap, 1),
            "abs_gap":         round(abs_gap, 1),
            "pct_gap":         round(pct_gap * 100, 1),
            "direction":       direction,
            "less_mult":       std_ln["less_mult"] if std_ln else None,
            "more_mult":       std_ln["more_mult"] if std_ln else None,
            "action":          action,
        })

    edges.sort(key=lambda e: -e["abs_gap"])
    return edges


# ── Full scan ─────────────────────────────────────────────────────────────────

def scan_parlayplay(pp_groups: dict | None = None) -> dict:
    """
    Run the full ParlayPlay scan.

    Parameters
    ----------
    pp_groups : optional dict from scanner_bugs._group_lines() for cross-platform comparison

    Returns
    -------
    dict with keys:
      "mono_bugs", "promo_bugs", "cross_platform_edges", "grouped", "raw"
    """
    from data.parlayplay import get_grouped_lines
    grouped, raw = get_grouped_lines()

    mono_bugs   = find_monotonicity_bugs(grouped)
    promo_bugs  = find_promo_value_bugs(grouped)
    cross_edges = (
        find_cross_platform_edges(pp_groups, grouped) if pp_groups else []
    )

    return {
        "mono_bugs":             mono_bugs,
        "promo_bugs":            promo_bugs,
        "cross_platform_edges":  cross_edges,
        "grouped":               grouped,
        "raw":                   raw,
    }


def print_results(results: dict):
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    g  = results["grouped"]
    print(f"\n{'='*65}")
    print(f"PARLAYPLAY BUG SCAN — {ts}")
    print(f"{'='*65}")
    print(f"  {len(results['raw'])} rows | {len(g)} (player,stat) combos")

    # ── Monotonicity bugs ─────────────────────────────────────────────────
    mono = results["mono_bugs"]
    if mono:
        print(f"\n  ⚡ {len(mono)} MULTIPLIER MONOTONICITY BUG(S):")
        for b in mono[:10]:
            print(f"    [{b['bug_type']}] {b['player']} {b['stat']}")
            print(f"      Line {b['line_low']}→{b['line_high']}: "
                  f"{b['direction']} mult REVERSES {b['mult_low']}→{b['mult_high']} "
                  f"(reversal={b['reversal']})")
            print(f"      → {b['action']}")
    else:
        print("\n  No monotonicity bugs found.")

    # ── Promo value bugs ──────────────────────────────────────────────────
    promos = results["promo_bugs"]
    if promos:
        print(f"\n  🔥 {len(promos)} PROMO VALUE BUG(S):")
        for b in promos[:10]:
            print(f"    {b['player']} {b['stat']}: {b['action']}")
    else:
        print("\n  No promo value bugs found.")

    # ── Cross-platform edges ──────────────────────────────────────────────
    cross = results["cross_platform_edges"]
    if cross:
        print(f"\n  📊 {len(cross)} CROSS-PLATFORM EDGE(S) (PrizePicks vs ParlayPlay):")
        for e in cross[:10]:
            print(f"    {e['player']} {e['stat']}: "
                  f"PP={e['pp_line']} vs ParlayPlay={e['parlayplay_line']} "
                  f"(gap={e['gap']:+.1f}, {e['pct_gap']}%)")
            print(f"      → {e['action']}")
    else:
        print("\n  No cross-platform edges found (or PP data not provided).")

    print(f"\n{'='*65}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("[parlayplay scanner] Starting scan...")
    results = scan_parlayplay()
    print_results(results)
