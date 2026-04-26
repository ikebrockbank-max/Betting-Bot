"""
scanner_kalshi_internal.py — Kalshi Internal Arbitrage Scanner.

Finds guaranteed-profit opportunities WITHIN Kalshi alone by detecting
two types of mispricing inside multi-market events:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Type 1 — SWEEP ARB (mutually exclusive outcomes)
  An event has N markets where EXACTLY ONE resolves YES.
  Example: "Who will be Speaker?" — Ryan YES 30¢, Johnson YES 25¢, Other YES 15¢
  Total cost: 70¢.  One pays $1.00 → profit guaranteed.

  When to buy: total YES_ask cost < fee-adjusted break-even.

Type 2 — ORDINAL INVERSION (threshold markets)
  Markets on the same underlying with different numeric thresholds
  should always satisfy: easier threshold costs MORE.
  Example: "Above $80k" YES should be ≥ "Above $90k" YES.
  If inverted, buying YES on the easier + NO on the harder is profitable.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Fee math (Kalshi 7% of profit on winning trade):
  Sweep arb net when contract i wins:
    net_i = 0.93 * (1 - ya_i/100) + ya_i/100 - total_cost/100
          = 0.93 + 0.07*(ya_i/100) - total_cost/100
  This is minimised when ya_i is minimised (cheapest outcome wins = biggest profit = biggest fee).
  Profitable condition: total_cost < 93 + 0.07 * min(yes_ask)  (in cents)
"""

from __future__ import annotations

import re
from collections import defaultdict
from itertools import combinations

KALSHI_FEE = 0.07   # 7% of profit on winning trade

# Minimum edge (after fee) to flag, in cents
MIN_SWEEP_EDGE_CENTS  = 2.0   # 2¢ on a $1 contract = 2% edge
MIN_ORDINAL_EDGE_PCTS = 0.03  # 3% raw mispricing between two threshold markets


# ── Exclusivity confidence ────────────────────────────────────────────────────

def _exclusivity_confidence(markets: list[dict]) -> tuple[str, str]:
    """
    Estimate how likely this group of markets is to be truly mutually exclusive
    AND collectively exhaustive (both required for a guaranteed sweep arb).

    Returns (confidence, reason) where confidence is "HIGH", "MEDIUM", or "LOW".

    HIGH   — clear competition where exactly one outcome wins (nominations, etc.)
    MEDIUM — possibly exclusive but verify before trading
    LOW    — nested date conditions or independent parallel events (likely NOT exclusive)
    """
    titles = [m["title"] for m in markets]

    # ── RED FLAG (checked first): nested temporal/numerical markers ─────────────
    # If all titles become identical after stripping dates/numbers, they are
    # nested conditions: "before Jan 2027" ⊃ "before Jan 2028" — NOT exclusive.
    date_re = (r'\b(20\d\d|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|'
               r'jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t)?|oct(?:ober)?|'
               r'nov(?:ember)?|dec(?:ember)?|\d{1,2}(?:st|nd|rd|th)?|q[1-4])\b')
    stripped = [re.sub(date_re, 'DATE', t.lower()) for t in titles]
    stripped = [re.sub(r'\b\d[\d,]*(?:\.\d+)?\b', 'NUM', s) for s in stripped]
    stripped = [re.sub(r'\s+', ' ', s).strip() for s in stripped]
    if len(set(stripped)) == 1:
        return "LOW", "Same question at different dates/thresholds (nested — NOT mutually exclusive)"

    # ── GREEN: specific nomination/competition language ─────────────────────────
    # Use precise patterns only — "presidency" and "minister" are excluded because
    # they appear in non-competitive contexts ("during his presidency", etc.)
    if any(re.search(r'\bnominee\b|\bnomination\b|\bnominate\b', t, re.I) for t in titles):
        return "HIGH", "Nomination markets — exactly one nominee per party/role"
    if any(re.search(r'\bpope\b|\bcardinal\b', t, re.I) for t in titles):
        return "HIGH", "Papal election — exactly one Pope elected"
    if any(re.search(r'\bmatchup\b|\bchampion\b', t, re.I) for t in titles):
        return "HIGH", "Championship/matchup markets — one outcome per competition"

    # ── RED FLAG: high similarity = likely independent parallel events ───────────
    from difflib import SequenceMatcher
    t0_norm = re.sub(r'[^a-z\s]', '', titles[0].lower())
    similarities = [
        SequenceMatcher(None, t0_norm, re.sub(r'[^a-z\s]', '', t.lower())).ratio()
        for t in titles[1:]
    ]
    if similarities and min(similarities) > 0.80:
        return "LOW", "Titles too similar — likely independent parallel events, not competing outcomes"

    # ── MEDIUM: different subjects, same predicate ───────────────────────────────
    return "MEDIUM", "Different subjects, same predicate — verify exactly one can resolve YES"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_threshold(title: str) -> float | None:
    """
    Pull the primary numeric threshold from a market title.
    Returns the float value, or None if no threshold is found.

    Examples:
      "Will Bitcoin close above $100,000?"  → 100000.0
      "Will the Fed funds rate be 4.75%?"   → 4.75
      "Will GDP growth exceed 2.5%?"        → 2.5
      "Will Trump win the election?"        → None
    """
    # Dollar amounts with optional commas
    m = re.search(r'\$\s*([\d,]+(?:\.\d+)?)', title)
    if m:
        return float(m.group(1).replace(',', ''))
    # Percentages
    m = re.search(r'([\d]+(?:\.\d+)?)\s*%', title)
    if m:
        return float(m.group(1))
    # Bare large numbers (BPS, price levels)
    m = re.search(r'\b(\d{4,}(?:,\d{3})*(?:\.\d+)?)\b', title)
    if m:
        return float(m.group(1).replace(',', ''))
    return None


def _sweep_break_even(yes_asks_cents: list[float]) -> float:
    """
    Maximum total cost (cents) at which buying all contracts is still profitable,
    given Kalshi's 7% fee on winning trades.

    worst case: cheapest contract wins → biggest profit → biggest fee
    break_even = 93 + 0.07 * min(ya)
    """
    ya_min = min(yes_asks_cents)
    return 93.0 + 0.07 * ya_min


# ── Type 1: Sweep arb ─────────────────────────────────────────────────────────

def find_sweep_arbs(
    markets: list[dict],
    min_edge_cents: float = MIN_SWEEP_EDGE_CENTS,
) -> list[dict]:
    """
    Group markets by event_ticker.
    For each event with 3+ active markets, check whether buying all YES contracts
    costs less than the fee-adjusted break-even.

    Returns list of arb dicts sorted by raw_edge_pct descending.
    """
    # Group by event
    events: dict[str, list[dict]] = defaultdict(list)
    for m in markets:
        evt = m.get("event_ticker", "")
        if evt:
            events[evt].append(m)

    arbs: list[dict] = []

    for evt_ticker, evt_markets in events.items():
        # Need at least 3 markets for a genuine sweep (2-market events are just binary)
        if len(evt_markets) < 3:
            continue

        yes_asks = [m["yes_ask"] for m in evt_markets]
        total_cost = sum(yes_asks)
        be = _sweep_break_even(yes_asks)

        raw_edge_cents = be - total_cost  # positive = profitable
        if raw_edge_cents < min_edge_cents:
            continue

        # Fee-adjusted edge on best/worst outcomes
        worst_net_cents = 93.0 + 0.07 * min(yes_asks) - total_cost  # cheapest wins
        best_net_cents  = 93.0 + 0.07 * max(yes_asks) - total_cost  # most expensive wins

        confidence, confidence_reason = _exclusivity_confidence(evt_markets)

        arbs.append({
            "arb_type":         "sweep",
            "event_ticker":     evt_ticker,
            "market_count":     len(evt_markets),
            "markets":          sorted(evt_markets, key=lambda m: -m["yes_ask"]),

            # Exclusivity confidence
            "confidence":       confidence,
            "confidence_reason":confidence_reason,

            # Cost / edge
            "total_cost_cents":     round(total_cost, 2),
            "total_cost_pct":       f"{total_cost:.1f}¢",
            "break_even_cents":     round(be, 2),
            "raw_edge_cents":       round(raw_edge_cents, 2),
            "raw_edge_pct":         f"{raw_edge_cents/100:.1%}",

            # Net return range
            "worst_net_cents":      round(worst_net_cents, 2),
            "best_net_cents":       round(best_net_cents, 2),
            "worst_net_pct":        f"{worst_net_cents/100:.1%}",
            "best_net_pct":         f"{best_net_cents/100:.1%}",

            "category":         evt_markets[0].get("category", ""),
            "close_time":       evt_markets[0].get("close_time", ""),

            # Human-readable action
            "action": (
                f"BUY YES on all {len(evt_markets)} markets in event {evt_ticker!r}  "
                f"Total cost: {total_cost:.1f}¢ → "
                f"{'guaranteed' if confidence == 'HIGH' else 'potential'} "
                f"{worst_net_cents:.1f}–{best_net_cents:.1f}¢ profit"
            ),
        })

    arbs.sort(key=lambda a: -a["raw_edge_cents"])
    return arbs


# ── Type 2: Ordinal inversion arb ─────────────────────────────────────────────

def find_ordinal_arbs(
    markets: list[dict],
    min_edge_pct: float = MIN_ORDINAL_EDGE_PCTS,
) -> list[dict]:
    """
    Within each event, find threshold-market pairs where the relative pricing
    is inverted (harder condition priced higher than easier condition).

    For "above X" markets:
      - YES(above $80k) ≥ YES(above $90k)  must hold.
        If inverted: buy easier YES + harder NO for < $1.00.

    Strategy when easier YES is cheaper than harder YES (inverted):
      Buy easier_YES at ya_easy + buy harder_NO at na_hard
      Cost = ya_easy + na_hard

      If above harder threshold:   easier wins + harder NO loses → net = 93*(1-ya_easy) - na_hard
      If between thresholds:       easier wins + harder NO wins  → net = 93*(1-ya_easy) + 93*(1-na_hard) - ya_easy - na_hard  [both win!]
      If below easier threshold:   easier loses + harder NO wins → net = 93*(1-na_hard) - ya_easy

    This is NOT always risk-free (above-harder scenario might be negative).
    We flag the minimum outcome and mark it profitable only if ALL scenarios > 0.
    """
    events: dict[str, list[dict]] = defaultdict(list)
    for m in markets:
        evt = m.get("event_ticker", "")
        if evt:
            events[evt].append(m)

    arbs: list[dict] = []

    for evt_ticker, evt_markets in events.items():
        if len(evt_markets) < 2:
            continue

        # Tag markets with their extracted threshold
        tagged = []
        for m in evt_markets:
            threshold = _extract_threshold(m["title"])
            if threshold is not None:
                tagged.append((threshold, m))

        if len(tagged) < 2:
            continue

        # Sort by threshold ascending
        tagged.sort(key=lambda x: x[0])

        # Compare consecutive pairs: easier (lower threshold) vs harder (higher threshold)
        for i in range(len(tagged) - 1):
            thresh_easy, m_easy = tagged[i]
            thresh_hard, m_hard = tagged[i + 1]

            ya_easy = m_easy["yes_ask"] / 100
            ya_hard = m_hard["yes_ask"] / 100
            na_hard = m_hard["no_ask"]  / 100
            na_easy = m_easy["no_ask"]  / 100

            # ── Inversion A: harder priced higher than easier (the classic inversion)
            # Buy YES on easier + NO on harder
            if ya_hard > ya_easy + min_edge_pct:
                cost = ya_easy + na_hard

                # Net in each scenario (with 7% fee on profit)
                net_above_hard = (1 - KALSHI_FEE * (1 - ya_easy)) - na_hard - ya_easy  # easy wins, hard-NO loses
                net_between    = ((1 - KALSHI_FEE * (1 - ya_easy))            # easy YES wins
                                + (1 - KALSHI_FEE * (1 - na_hard))            # hard NO wins
                                - ya_easy - na_hard)                           # costs
                net_below_easy = (1 - KALSHI_FEE * (1 - na_hard)) - ya_easy - na_hard  # only hard-NO wins

                min_net = min(net_above_hard, net_between, net_below_easy)
                raw_edge = ya_hard - ya_easy  # the inversion size

                if raw_edge >= min_edge_pct:
                    arbs.append({
                        "arb_type":       "ordinal_inversion",
                        "subtype":        "harder_overpriced",
                        "event_ticker":   evt_ticker,

                        "easy_market":    m_easy,
                        "hard_market":    m_hard,
                        "easy_threshold": thresh_easy,
                        "hard_threshold": thresh_hard,

                        "ya_easy":        round(ya_easy, 4),
                        "ya_hard":        round(ya_hard, 4),
                        "total_cost":     round(cost, 4),

                        "raw_edge":       round(raw_edge, 4),
                        "raw_edge_pct":   f"{raw_edge:.1%}",

                        "net_above_hard": round(net_above_hard, 4),
                        "net_between":    round(net_between, 4),
                        "net_below_easy": round(net_below_easy, 4),
                        "min_net":        round(min_net, 4),
                        "guaranteed":     min_net > 0,

                        "category":       m_easy.get("category", ""),
                        "close_time":     m_easy.get("close_time", ""),

                        "action": (
                            f"BUY YES easier ({thresh_easy}) @ {ya_easy:.0%} + "
                            f"BUY NO harder ({thresh_hard}) @ {na_hard:.0%} "
                            f"= {cost:.0%} total | inversion: {raw_edge:.1%}"
                        ),
                    })

            # ── Inversion B: easier priced higher than harder (makes no sense — harder must be ≤ easier)
            # If ya_easy < ya_hard already handled above.
            # Edge case: if somehow ya_easy > ya_hard + min_edge significantly,
            # that could mean the markets have contradictory expectations.
            # (Not implemented here — Type A covers the actionable case.)

    arbs.sort(key=lambda a: -a["raw_edge"])
    return arbs


# ── Combined scan ─────────────────────────────────────────────────────────────

def scan_internal_arbs(markets: list[dict]) -> dict:
    """
    Run both sweep and ordinal inversion scans.
    Returns:
        {
            "sweep_arbs":         list of HIGH/MEDIUM confidence sweep arb dicts,
            "sweep_arbs_low":     list of LOW confidence sweep arb dicts (informational),
            "ordinal_arbs":       list of ordinal inversion dicts,
            "total":              int (HIGH/MEDIUM only),
        }
    """
    all_sweeps = find_sweep_arbs(markets)
    sweep_real = [a for a in all_sweeps if a["confidence"] in ("HIGH", "MEDIUM")]
    sweep_low  = [a for a in all_sweeps if a["confidence"] == "LOW"]
    ordinal    = find_ordinal_arbs(markets)
    return {
        "sweep_arbs":     sweep_real,
        "sweep_arbs_low": sweep_low,
        "ordinal_arbs":   ordinal,
        "total":          len(sweep_real) + len(ordinal),
    }


# ── CLI output ────────────────────────────────────────────────────────────────

def print_results(results: dict):
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    conf_icons = {"HIGH": "✅", "MEDIUM": "⚠ ", "LOW": "❌"}

    print(f"\n{'='*70}")
    print(f"KALSHI INTERNAL ARB SCAN — {ts}")
    print(f"{'='*70}")
    print(f"  Sweep arbs (HIGH/MEDIUM confidence): {len(results['sweep_arbs'])}")
    print(f"  Sweep arbs (LOW — likely false):     {len(results.get('sweep_arbs_low', []))}")
    print(f"  Ordinal inversions:                  {len(results['ordinal_arbs'])}")

    if results["sweep_arbs"]:
        print(f"\n  ── SWEEP ARBS (actionable) ──")
        for a in results["sweep_arbs"]:
            icon = conf_icons.get(a["confidence"], "?")
            print(f"\n  {icon} [{a['confidence']}] [{a['event_ticker']}]  "
                  f"{a['market_count']} markets  cost={a['total_cost_cents']:.1f}¢  edge={a['raw_edge_pct']}")
            print(f"    {a['confidence_reason']}")
            print(f"    Net return: {a['worst_net_pct']} – {a['best_net_pct']}")
            for m in a["markets"][:6]:
                print(f"      {m['yes_ask']:5.1f}¢  {m['title'][:60]}")
            print(f"    → {a['action']}")

    if results.get("sweep_arbs_low"):
        print(f"\n  ── SWEEP (LOW confidence — nested/independent, skip) ──")
        for a in results["sweep_arbs_low"]:
            print(f"    ❌ [{a['event_ticker']}]  {a['confidence_reason'][:70]}")

    if results["ordinal_arbs"]:
        print(f"\n  ── ORDINAL INVERSIONS ──")
        for a in results["ordinal_arbs"]:
            guar = "✅ GUARANTEED" if a["guaranteed"] else "⚠  NOT risk-free"
            print(f"\n  [{a['event_ticker']}]  {guar}  raw edge={a['raw_edge_pct']}")
            print(f"    Easy ({a['easy_threshold']}): YES={a['ya_easy']:.0%} | "
                  f"Hard ({a['hard_threshold']}): YES={a['ya_hard']:.0%}")
            print(f"    Scenarios:  above_hard={a['net_above_hard']:.1%}  "
                  f"between={a['net_between']:.1%}  below_easy={a['net_below_easy']:.1%}")
            print(f"    → {a['action']}")

    if results["total"] == 0:
        print("\n  No internal arb opportunities found.")
    print(f"\n{'='*70}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("[internal arb] Fetching Kalshi markets...")
    from data.kalshi import get_open_prediction_markets
    markets = get_open_prediction_markets()
    print(f"[internal arb] {len(markets)} markets across events")

    results = scan_internal_arbs(markets)
    print_results(results)
