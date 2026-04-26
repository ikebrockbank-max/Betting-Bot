"""
Polymarket data client.

Uses the public Gamma API — no wallet or auth required for reading.
Markets are binary YES/NO contracts settled in USDC on Polygon.

Prices from outcomePrices are mid-prices (not bid/ask).
For accurate execution you'd need the CLOB order book,
but mid-prices are precise enough for arb scanning.
"""

import json
import time
import requests
from pathlib import Path

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

_CACHE_PATH = Path("logs/.poly_market_cache.json")
_CACHE_TTL  = 300  # seconds (5 min) — prices move, don't cache too long


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


def _parse_prices(market: dict) -> tuple[float | None, float | None]:
    """
    Extract (yes_price, no_price) in decimal [0, 1] from a Gamma market dict.
    outcomePrices is a JSON string like '["0.65", "0.35"]'.
    outcomes is a JSON string like '["Yes", "No"]'.
    Returns (None, None) if unparseable.
    """
    try:
        prices   = json.loads(market.get("outcomePrices", "[]"))
        outcomes = json.loads(market.get("outcomes",      "[]"))
    except (json.JSONDecodeError, TypeError):
        return None, None

    if len(prices) < 2 or len(outcomes) < 2:
        return None, None

    # Find YES/NO indices — some markets have reversed order
    yes_idx, no_idx = 0, 1
    if outcomes[0].lower() in ("no", "false"):
        yes_idx, no_idx = 1, 0

    try:
        return float(prices[yes_idx]), float(prices[no_idx])
    except (ValueError, TypeError):
        return None, None


def get_markets(active_only: bool = True, limit: int = 2000) -> list[dict]:
    """
    Fetch active Polymarket markets with current prices.

    Returns list of dicts:
        id            str   — unique market ID
        question      str   — market question text
        category      str   — e.g. "Politics", "Economics", "Crypto"
        yes_price     float — mid-price for YES (0–1)
        no_price      float — mid-price for NO (0–1)
        volume        float — total trading volume (USDC)
        end_date      str   — ISO resolution date
        url           str   — polymarket.com link
        condition_id  str   — on-chain condition ID
        raw           dict  — full original record
    """
    cached = _cache_load()
    if cached is not None:
        return cached

    markets = []
    offset  = 0
    batch_size = 100
    consecutive_empty = 0

    while len(markets) < limit:
        try:
            resp = requests.get(
                f"{GAMMA_BASE}/markets",
                params={
                    "active":  "true" if active_only else "",
                    "closed":  "false",
                    "limit":   batch_size,
                    "offset":  offset,
                },
                timeout=20,
            )
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            print(f"[polymarket] fetch error (offset={offset}): {e}")
            break

        if not batch:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            offset += batch_size
            continue
        consecutive_empty = 0

        for m in batch:
            if active_only and (m.get("closed") or not m.get("active", True)):
                continue
            yes_p, no_p = _parse_prices(m)
            if yes_p is None:
                continue

            slug = m.get("slug", "")
            markets.append({
                "id":           m.get("id", ""),
                "question":     m.get("question", ""),
                "category":     m.get("category", ""),
                "yes_price":    round(yes_p, 4),
                "no_price":     round(no_p,  4),
                "volume":       float(m.get("volume", 0) or 0),
                "end_date":     m.get("endDateIso", ""),
                "url":          f"https://polymarket.com/event/{slug}",
                "condition_id": m.get("conditionId", ""),
                "raw":          m,
            })

        offset += len(batch)
        if len(batch) < batch_size:
            break
        time.sleep(0.15)

    _cache_save(markets)
    return markets


def get_clob_book(token_id: str) -> dict | None:
    """
    Fetch live order book from CLOB API for a specific token.
    More accurate than Gamma mid-prices — use for execution sizing.

    Returns dict with 'bids' and 'asks' lists of {price, size} dicts,
    or None if unavailable.
    """
    try:
        resp = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "bids": data.get("bids", []),
            "asks": data.get("asks", []),
        }
    except Exception:
        return None
