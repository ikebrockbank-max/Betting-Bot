"""
PredictIt data client.

Uses the public PredictIt Market Data API — no auth required.
Returns BINARY markets only (single-contract YES/NO), which map
directly to Kalshi binary markets for arb scanning.

Fee Reality:
  PredictIt charges 10% of PROFIT on winning trades
  PLUS  5% withdrawal fee on ALL proceeds (not just profit).

  Effective fee on a $1 payout when you bought at price p:
    fee = 0.10 * (1 - p) + 0.05
    net payout = 1 - 0.10*(1-p) - 0.05 = 0.85 + 0.10*p

  Example: buy NO at 0.48 and NO wins:
    net = 0.85 + 0.10*0.48 = $0.898 on a $0.48 buy → ~87% return
    versus Polymarket: 0.98 on a $0.48 buy → ~104% return

  → You need a much larger raw edge vs Kalshi to clear PredictIt fees.
    Rule of thumb: need 15%+ raw edge for PredictIt arbs to be profitable.

Position cap: $850 per market contract.
"""

from __future__ import annotations

import json
import time
import requests
from pathlib import Path

API_URL    = "https://www.predictit.org/api/marketdata/all/"
_CACHE_PATH = Path("logs/.predictit_cache.json")
_CACHE_TTL  = 300   # seconds — 5 min

# Fee constants (exported for use in scanner_arb.py)
PROFIT_FEE   = 0.10   # 10% of profit on the winning side
WITHDRAW_FEE = 0.05   # 5% of total payout (applied regardless of size)
MAX_POSITION = 850    # USD cap per contract


def _cache_load() -> list[dict] | None:
    if _CACHE_PATH.exists():
        try:
            raw = json.loads(_CACHE_PATH.read_text())
            if time.time() - raw.get("ts", 0) < _CACHE_TTL:
                return raw["markets"]
        except Exception:
            pass
    return None


def _cache_save(markets: list[dict]):
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps({"ts": time.time(), "markets": markets}))


def _guess_category(name: str) -> str:
    """Heuristic category from market name — PredictIt is mostly political."""
    n = name.lower()
    if any(w in n for w in ("fed ", "rate", "gdp", "inflation", "cpi", "recession", "unemploy", "dow", "s&p")):
        return "Economics"
    if any(w in n for w in ("bitcoin", "crypto", "btc", "eth")):
        return "Crypto"
    if any(w in n for w in ("nba", "nfl", "mlb", "nhl", "cup", "championship", "playoff", "super bowl")):
        return "Sports"
    if any(w in n for w in ("oscar", "emmy", "grammy", "award")):
        return "Entertainment"
    return "Politics"  # default — PredictIt is ~90% political


def get_markets(binary_only: bool = True) -> list[dict]:
    """
    Fetch active PredictIt binary markets.

    binary_only=True (default) filters to single-contract markets only —
    these are simple YES/NO bets that map 1:1 to Kalshi binary markets.
    Multi-contract markets (e.g. "who wins the nomination?" with 5 candidates)
    are excluded because they don't have a clean NO side.

    Returns list of dicts:
        id           int   — PredictIt market ID
        question     str   — market question (the market name)
        category     str   — heuristic category
        yes_price    float — best buy YES cost (ask, 0–1)   ← buy price you'd pay
        no_price     float — best buy NO cost  (ask, 0–1)   ← buy price you'd pay
        yes_bid      float — best sell YES price (bid, 0–1)
        no_bid       float — best sell NO price  (bid, 0–1)
        volume       float — always 0.0 (PredictIt API doesn't provide)
        end_date     str   — resolution date or "N/A"
        url          str   — predictit.org market URL
        contract_id  int   — internal contract ID
        platform     str   — "predictit" (for multi-platform arb labeling)
    """
    cached = _cache_load()
    if cached is not None:
        return cached

    try:
        resp = requests.get(API_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[predictit] fetch error: {e}")
        return []

    markets: list[dict] = []

    for m in data.get("markets", []):
        contracts = [c for c in m.get("contracts", []) if c.get("status") == "Open"]

        if binary_only and len(contracts) != 1:
            continue
        if not contracts:
            continue

        c = contracts[0]

        # --- Extract prices --------------------------------------------------
        # bestBuyYesCost  = ASK for YES (what you pay to go long YES)
        # bestBuyNoCost   = ASK for NO  (what you pay to go long NO)
        # bestSellYesCost = BID for YES (what you receive selling YES)
        # bestSellNoCost  = BID for NO  (what you receive selling NO)
        yes_ask = c.get("bestBuyYesCost")
        no_ask  = c.get("bestBuyNoCost")
        yes_bid = c.get("bestSellYesCost")
        no_bid  = c.get("bestSellNoCost")

        if yes_ask is None or no_ask is None:
            continue

        try:
            yes_ask = float(yes_ask)
            no_ask  = float(no_ask)
            yes_bid = float(yes_bid) if yes_bid is not None else max(0.01, yes_ask - 0.02)
            no_bid  = float(no_bid)  if no_bid  is not None else max(0.01, no_ask  - 0.02)
        except (ValueError, TypeError):
            continue

        if yes_ask <= 0 or yes_ask >= 1:
            continue

        markets.append({
            "id":          m.get("id"),
            "question":    m.get("name", ""),
            "short_name":  m.get("shortName", ""),
            "category":    _guess_category(m.get("name", "")),
            # yes_price / no_price mimic Polymarket format so find_arbs() works unchanged
            "yes_price":   round(yes_ask, 4),   # use ASK since that's execution cost
            "no_price":    round(no_ask,  4),
            "yes_bid":     round(yes_bid, 4),
            "no_bid":      round(no_bid,  4),
            "volume":      0.0,                  # not provided by API
            "end_date":    c.get("dateEnd", ""),
            "url":         m.get("url", ""),
            "contract_id": c.get("id"),
            "platform":    "predictit",
        })

    print(f"[predictit] {len(markets)} binary open markets")
    _cache_save(markets)
    return markets
