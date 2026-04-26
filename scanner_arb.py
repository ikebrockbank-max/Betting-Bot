"""
scanner_arb.py — Prediction Market Cross-Platform Arbitrage Scanner.

Detects true arbitrage between Kalshi and a counterparty platform:

  True arb: buy YES on Platform A + NO on Platform B for < $1.00 combined.
  Regardless of outcome, one side pays $1.00 → guaranteed profit.

Example:
  Kalshi  YES = 42¢   (they think 42% chance)
  Counter YES = 51¢   (they think 51% chance)

  Buy Kalshi YES at 42¢ + Counter NO at 49¢ = 91¢ total cost
  If event happens:   Kalshi pays $1 − Counter pays $0  → net +9¢
  If event fails:     Kalshi pays $0 + Counter pays $1  → net +9¢
  Guaranteed 9% return.

Fee Reality by Platform
───────────────────────
  Kalshi   : ~7% of PROFIT on winning trades.
  Polymarket: ~2% of winnings (relayer + gas).   → need ~5% raw edge
  PredictIt : 10% of PROFIT + 5% WITHDRAWAL on all proceeds.
              → need ~15% raw edge (fees are brutal)

  The scanner accepts per-platform fee configs so the fee_adj_edge
  is accurate for whichever counterparty is being scanned.

Market Matching
───────────────
  The hardest part. Uses:
    1. Token overlap on key terms (numbers, named entities, dates)
    2. Fuzzy sequence similarity on normalized text
    3. Hard entity gate: returns 0.0 if no shared named entities
       (prevents "Israel PM" matching "Hungary PM" false positives)
  Pairs with match_score < MIN_MATCH_SCORE are rejected.
  Always verify matched markets resolve identically before trading!
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_RAW_EDGE    = 0.05   # minimum raw edge to flag (5%)
MIN_MATCH_SCORE = 0.62   # minimum similarity to consider markets equivalent

# Kalshi fees (always the same regardless of counterparty)
KALSHI_FEE      = 0.07   # 7% of profit on winning trades

# Counterparty fee presets — passed into find_arbs() as `cp_fees`
FEES_POLYMARKET  = {"profit": 0.02, "withdraw": 0.00}   # ~2% relayer/gas
FEES_PREDICTIT   = {"profit": 0.10, "withdraw": 0.05}   # 10% profit + 5% withdrawal
FEES_ROBINHOOD   = {"profit": 0.01, "withdraw": 0.00}   # ~1% (estimate)

# Stop words — stripped before matching
_STOPS = frozenset({
    "will", "the", "a", "an", "in", "by", "at", "on", "to", "for", "of",
    "be", "is", "are", "was", "were", "have", "has", "had", "do", "does",
    "did", "that", "this", "these", "those", "with", "from", "or", "and",
    "end", "before", "after", "which", "any", "all", "than", "more",
    "above", "below", "between", "during", "within", "next", "first",
    "last", "new", "us", "win", "won", "become", "president", "election",
})

# Generic structural tokens that appear in many markets — not discriminating alone
_GENERIC = frozenset({
    # Political structure (including common abbreviations)
    "prime", "minister", "president", "pm", "vp", "mp", "gov",
    "election", "win", "next", "become", "candidate", "nominee",
    "party", "race", "vote", "lead", "poll",
    # Sports context (common to many player/team prop markets)
    "score", "points", "tonight", "game", "season", "match", "player",
    "team", "series", "total", "assist", "rebound", "yards", "goals",
    # Financial structure
    "price", "close", "above", "reach", "least", "exceed", "market",
})

# Map Polymarket category strings → Kalshi category strings
_CAT_MAP: dict[str, str] = {
    "politics":   "Politics",
    "economics":  "Economics",
    "financial":  "Economics",
    "finance":    "Economics",
    "crypto":     "Crypto",
    "sports":     "Sports",
    "science":    "Science",
    "culture":    "Pop Culture",
    "tech":       "Technology",
    "technology": "Technology",
    "health":     "Health",
}


# ── Text normalisation ─────────────────────────────────────────────────────────

def _ascii(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return nfkd.encode("ascii", "ignore").decode("ascii")


def normalize(text: str) -> str:
    """Lowercase, ASCII, strip punctuation and stop words."""
    t = _ascii(text).lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    tokens = [w for w in t.split() if w not in _STOPS and len(w) > 1]
    return " ".join(tokens)


def key_tokens(text: str) -> set[str]:
    """
    Extract high-signal tokens: numbers, capitalised words, year-like tokens.
    These matter more than generic words for determining market equivalence.
    """
    norm = normalize(text)
    tokens = set(norm.split())
    # Also pull numbers from original (preserves "2025", "25bps", etc.)
    tokens |= set(re.findall(r"\d+(?:\.\d+)?", text.lower()))
    return tokens


def entity_tokens(text: str) -> set[str]:
    """
    Extract 'anchor' entity tokens — proper nouns and numbers.
    These uniquely identify the subject of a market (person, country, org, value).

    Strategy: extract words that are >= 4 chars and are NOT generic structural terms.
    This catches: country names, person last names, org names, numbers.
    """
    # CamelCase proper nouns (Obama, Israel, Netanyahu…)
    camel = set(re.findall(r"\b[A-Z][a-z]{2,}\b", text))
    # ALL-CAPS abbreviations (US, UK, EU, NATO, GDP…)
    abbrevs = set(re.findall(r"\b[A-Z]{2,}\b", text))

    # 3+ digit numbers (years like 2024; filter out 2-digit stat thresholds)
    nums = set(re.findall(r"\b\d{3,}\b", text))

    # Long non-generic normalised tokens
    norm_tokens = {
        w for w in normalize(text).split()
        if len(w) >= 5 and w not in _GENERIC and w not in _STOPS
    }

    # CamelCase: filter against BOTH _GENERIC and _STOPS
    # (catches "Will", "The", "Prime", "Minister")
    camel_clean = {
        _ascii(w).lower() for w in camel
        if _ascii(w).lower() not in _GENERIC and _ascii(w).lower() not in _STOPS
    }
    # Abbreviations: filter against _GENERIC only
    # ("US" must not be killed by "us" in _STOPS)
    abbrevs_clean = {
        _ascii(w).lower() for w in abbrevs
        if _ascii(w).lower() not in _GENERIC
    }

    result = camel_clean | abbrevs_clean | nums | norm_tokens
    return result


def match_score(a: str, b: str) -> float:
    """
    Combined similarity score [0, 1] between two market titles.

    Weights:
      50% — fuzzy sequence similarity on normalised text
      50% — entity Jaccard overlap (names, countries, numbers)

    Hard gate: if the entity sets share zero tokens, returns 0 regardless
    of text similarity.  This prevents structural matches like
    "next Prime Minister of Israel" ↔ "next Prime Minister of Hungary"
    from scoring above 0.
    """
    na, nb = normalize(a), normalize(b)
    seq_sim = SequenceMatcher(None, na, nb).ratio()

    ea, eb = entity_tokens(a), entity_tokens(b)
    e_union = ea | eb
    if not e_union:
        e_jac = 0.0
    elif not (ea & eb):
        # No shared entity — these are definitely different markets
        return 0.0
    else:
        e_jac = len(ea & eb) / len(e_union)

    return round(0.50 * seq_sim + 0.50 * e_jac, 4)


# ── Market matching ────────────────────────────────────────────────────────────

def match_markets(
    kalshi_markets: list[dict],
    poly_markets:   list[dict],
    min_score:      float = MIN_MATCH_SCORE,
) -> list[dict]:
    """
    Find equivalent market pairs across Kalshi and Polymarket.

    Returns list of pair dicts:
        kalshi        dict  — Kalshi market
        polymarket    dict  — Polymarket market
        match_score   float — similarity [0, 1]
    """
    pairs = []
    seen_poly_ids: set[str] = set()

    for km in kalshi_markets:
        k_title = km.get("title", "")
        if not k_title:
            continue

        best_score = 0.0
        best_pm    = None

        for pm in poly_markets:
            p_q = pm.get("question", "")
            if not p_q:
                continue

            # Fast pre-filter: at least 1 key token in common
            if not key_tokens(k_title) & key_tokens(p_q):
                continue

            s = match_score(k_title, p_q)
            if s > best_score:
                best_score = s
                best_pm    = pm

        if best_pm and best_score >= min_score:
            pairs.append({
                "kalshi":      km,
                "polymarket":  best_pm,
                "match_score": best_score,
            })
            seen_poly_ids.add(best_pm["id"])

    return pairs


# ── Arb calculation ────────────────────────────────────────────────────────────

def _fee_adj_edge(
    raw_edge:            float,
    kalshi_yes_ask_frac: float,
    cp_fees:             dict | None = None,
) -> float:
    """
    Fee-adjusted edge for a YES-Kalshi / NO-Counterparty arb.

    raw_edge           = cp_yes - kalshi_yes_ask   (before fees)
    kalshi_yes_ask_frac = fraction Kalshi asks for YES (e.g. 0.42)

    Kalshi fee (winning YES scenario):
        KALSHI_FEE * profit = KALSHI_FEE * (1 - k_ask)

    Counterparty fee (winning NO scenario):
        profit_fee  * profit  = cp_fees["profit"]  * (1 - cp_no_ask)
        withdraw_fee * payout = cp_fees["withdraw"] * 1.0

    We take the average of both winning scenarios (one always occurs).
    """
    if cp_fees is None:
        cp_fees = FEES_POLYMARKET

    # Counterparty NO ask ≈ 1 - raw_edge - kalshi_yes_ask (rough)
    cp_no_ask = 1.0 - kalshi_yes_ask_frac - raw_edge

    # Fee on Kalshi YES winning scenario
    k_fee = KALSHI_FEE * (1.0 - kalshi_yes_ask_frac)

    # Fee on counterparty NO winning scenario
    cp_profit_fee   = cp_fees.get("profit",   0.0) * max(0.0, 1.0 - cp_no_ask)
    cp_withdraw_fee = cp_fees.get("withdraw", 0.0)  # applied to full $1 payout
    cp_fee = cp_profit_fee + cp_withdraw_fee

    avg_fee = (k_fee + cp_fee) / 2.0
    return round(raw_edge - avg_fee, 4)


def find_arbs(
    kalshi_markets:  list[dict],
    counter_markets: list[dict],
    min_edge:        float       = MIN_RAW_EDGE,
    min_score:       float       = MIN_MATCH_SCORE,
    cp_fees:         dict | None = None,
    counterparty:    str         = "polymarket",
) -> dict:
    """
    Full arb scan: match markets then calculate cross-platform edges.

    Args:
        kalshi_markets:  list of Kalshi market dicts
        counter_markets: list of counterparty market dicts (Polymarket, PredictIt, …)
        min_edge:        minimum raw edge to flag (default 5%)
        min_score:       minimum match similarity (default 0.62)
        cp_fees:         counterparty fee dict {"profit": float, "withdraw": float}
                         defaults to FEES_POLYMARKET if None
        counterparty:    label string shown in output ("polymarket", "predictit", …)

    Returns:
        {
            "arbs":             list of arb dicts (sorted by raw_edge desc),
            "pairs":            list of matched pairs (all, not just arb),
            "kalshi_count":     int,
            "counter_count":    int,
            "pair_count":       int,
            "counterparty":     str,
        }
    """
    if cp_fees is None:
        cp_fees = FEES_POLYMARKET

    # Profitable threshold scales with counterparty fees
    # PredictIt needs higher edge to clear its 15%+ total fee drag
    cp_total_fee_approx = cp_fees.get("profit", 0) * 0.5 + cp_fees.get("withdraw", 0)
    profitable_min = max(0.02, cp_total_fee_approx * 0.3)

    pairs      = match_markets(kalshi_markets, counter_markets, min_score)
    arbs: list[dict] = []

    for pair in pairs:
        km = pair["kalshi"]
        pm = pair["polymarket"]   # field name stays "polymarket" in pair dict

        # Prices — Kalshi in cents, counterparty in decimal
        k_yes_ask = km["yes_ask"] / 100
        k_yes_bid = km["yes_bid"] / 100
        k_no_ask  = km["no_ask"]  / 100

        p_yes = pm["yes_price"]
        p_no  = pm["no_price"]

        cp_label = counterparty.capitalize()
        cp_id    = pm.get("id", "")

        # ── Arb A: YES on Kalshi + NO on Counterparty ────────────────────────
        # Profitable when counterparty prices YES higher than Kalshi
        cost_a    = k_yes_ask + p_no
        raw_a     = round(p_yes - k_yes_ask, 4)   # = 1 - cost_a
        fee_adj_a = _fee_adj_edge(raw_a, k_yes_ask, cp_fees)

        if raw_a >= min_edge:
            arbs.append({
                "arb_type":        f"YES_kalshi_NO_{counterparty}",
                "direction":       f"{cp_label} prices YES higher — buy YES Kalshi + NO {cp_label}",
                "match_score":     pair["match_score"],
                "counterparty":    counterparty,

                # Market info
                "kalshi_ticker":   km["ticker"],
                "kalshi_title":    km["title"],
                "kalshi_url":      km.get("url", ""),
                "kalshi_category": km.get("category", ""),
                "poly_id":         cp_id,
                "poly_question":   pm["question"],
                "poly_url":        pm.get("url", ""),
                "poly_category":   pm.get("category", ""),
                "close_time":      km.get("close_time", pm.get("end_date", "")),

                # Prices
                "k_yes_ask":       round(k_yes_ask, 4),
                "k_yes_ask_pct":   f"{k_yes_ask:.1%}",
                "p_no_price":      round(p_no,  4),
                "p_no_price_pct":  f"{p_no:.1%}",
                "total_cost":      round(cost_a, 4),

                # Edge
                "raw_edge":        raw_a,
                "raw_edge_pct":    f"{raw_a:.1%}",
                "fee_adj_edge":    fee_adj_a,
                "fee_adj_pct":     f"{fee_adj_a:.1%}",
                "profitable":      fee_adj_a > profitable_min,

                # Liquidity
                "kalshi_volume":   km.get("volume", 0),
                "poly_volume":     pm.get("volume", 0),

                # Human-readable action
                "action": (
                    f"BUY YES on Kalshi @ {k_yes_ask:.1%}  +  "
                    f"BUY NO on {cp_label} @ {p_no:.1%}  =  "
                    f"{cost_a:.2%} total → {raw_a:.1%} raw / {fee_adj_a:.1%} after fees"
                ),
            })

        # ── Arb B: NO on Kalshi + YES on Counterparty ────────────────────────
        # Profitable when counterparty prices YES lower than Kalshi
        cost_b    = k_no_ask + p_yes
        raw_b     = round(k_yes_bid - p_yes, 4)   # = 1 - cost_b (approx)
        fee_adj_b = _fee_adj_edge(raw_b, 1.0 - k_no_ask, cp_fees)

        if raw_b >= min_edge:
            arbs.append({
                "arb_type":        f"NO_kalshi_YES_{counterparty}",
                "direction":       f"{cp_label} prices YES lower — buy NO Kalshi + YES {cp_label}",
                "match_score":     pair["match_score"],
                "counterparty":    counterparty,

                "kalshi_ticker":   km["ticker"],
                "kalshi_title":    km["title"],
                "kalshi_url":      km.get("url", ""),
                "kalshi_category": km.get("category", ""),
                "poly_id":         cp_id,
                "poly_question":   pm["question"],
                "poly_url":        pm.get("url", ""),
                "poly_category":   pm.get("category", ""),
                "close_time":      km.get("close_time", pm.get("end_date", "")),

                "k_no_ask":        round(k_no_ask, 4),
                "k_no_ask_pct":    f"{k_no_ask:.1%}",
                "p_yes_price":     round(p_yes, 4),
                "p_yes_price_pct": f"{p_yes:.1%}",
                "total_cost":      round(cost_b, 4),

                "raw_edge":        raw_b,
                "raw_edge_pct":    f"{raw_b:.1%}",
                "fee_adj_edge":    fee_adj_b,
                "fee_adj_pct":     f"{fee_adj_b:.1%}",
                "profitable":      fee_adj_b > profitable_min,

                "kalshi_volume":   km.get("volume", 0),
                "poly_volume":     pm.get("volume", 0),

                "action": (
                    f"BUY NO on Kalshi @ {k_no_ask:.1%}  +  "
                    f"BUY YES on {cp_label} @ {p_yes:.1%}  =  "
                    f"{cost_b:.2%} total → {raw_b:.1%} raw / {fee_adj_b:.1%} after fees"
                ),
            })

    arbs.sort(key=lambda a: -a["raw_edge"])

    return {
        "arbs":          arbs,
        "pairs":         pairs,
        "kalshi_count":  len(kalshi_markets),
        "counter_count": len(counter_markets),
        "pair_count":    len(pairs),
        "counterparty":  counterparty,
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

def print_results(results: dict):
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cp = results.get("counterparty", "counterparty").capitalize()

    print(f"\n{'='*70}")
    print(f"PREDICTION MARKET ARB SCAN — {ts}")
    print(f"{'='*70}")
    print(f"  Kalshi markets:      {results['kalshi_count']}")
    print(f"  {cp} markets: {results.get('counter_count', results.get('poly_count', '?'))}")
    print(f"  Matched pairs:       {results['pair_count']}")
    print(f"  Arb opportunities:   {len(results['arbs'])}")

    arbs      = results["arbs"]
    profitable = [a for a in arbs if a["profitable"]]
    watch      = [a for a in arbs if not a["profitable"]]

    if profitable:
        print(f"\n  ✅ {len(profitable)} PROFITABLE ARB(S) (edge > fees):")
        for a in profitable[:10]:
            print(f"    [{a['match_score']:.2f}] {a['kalshi_title'][:55]}")
            print(f"      {a['action']}")
            print(f"      ⚠  Verify markets resolve identically!")

    if watch:
        print(f"\n  👀 {len(watch)} EDGE(S) BELOW FEE THRESHOLD (watching only):")
        for a in watch[:5]:
            print(f"    [{a['match_score']:.2f}] {a['kalshi_title'][:55]}")
            print(f"      raw={a['raw_edge_pct']} fee_adj={a['fee_adj_pct']}")

    if not arbs:
        print("\n  No arb opportunities found this scan.")
    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Prediction market arb scanner")
    parser.add_argument("--platform", choices=["polymarket", "predictit", "both"],
                        default="both", help="Counterparty platform to scan")
    args = parser.parse_args()

    from data.kalshi import get_open_prediction_markets

    print("[arb] Fetching Kalshi prediction markets...")
    kalshi = get_open_prediction_markets()
    print(f"[arb] {len(kalshi)} Kalshi markets")

    if args.platform in ("polymarket", "both"):
        from data.polymarket import get_markets as get_poly_markets
        print("\n[arb] ── Kalshi × Polymarket ──")
        poly = get_poly_markets()
        print(f"[arb] {len(poly)} Polymarket markets")
        results = find_arbs(kalshi, poly, cp_fees=FEES_POLYMARKET, counterparty="polymarket")
        print_results(results)

    if args.platform in ("predictit", "both"):
        from data.predictit import get_markets as get_pi_markets
        print("\n[arb] ── Kalshi × PredictIt ──")
        pi = get_pi_markets()
        print(f"[arb] {len(pi)} PredictIt binary markets")
        results = find_arbs(kalshi, pi, cp_fees=FEES_PREDICTIT, counterparty="predictit")
        print_results(results)
