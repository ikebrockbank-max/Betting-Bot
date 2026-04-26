import base64
import datetime
import json
import os
import time
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

# Elections/political markets (original endpoint)
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# General trading API — broader market categories (economics, crypto, weather…)
TRADING_URL = "https://trading-api.kalshi.com/trade-api/v2"

KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
PRIVATE_KEY_B64  = os.getenv("KALSHI_PRIVATE_KEY_BASE64", "")

_MARKET_CACHE_PATH = Path("logs/.kalshi_market_cache.json")
_MARKET_CACHE_TTL  = 300  # seconds


def _load_private_key():
    # Cloud deploy: key stored as base64 env var
    if PRIVATE_KEY_B64:
        pem_bytes = base64.b64decode(PRIVATE_KEY_B64)
        return serialization.load_pem_private_key(pem_bytes, password=None)
    with open(PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign_request(method: str, path: str) -> dict:
    timestamp_ms = str(int(datetime.datetime.now(datetime.UTC).timestamp() * 1000))
    full_path = "/trade-api/v2" + path
    message = (timestamp_ms + method.upper() + full_path).encode("utf-8")
    private_key = _load_private_key()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "Content-Type": "application/json",
    }


def _signed_get(base_url: str, path: str, params: dict = None) -> dict:
    """Signed GET against any Kalshi API base URL."""
    headers = _sign_request("GET", path.split("?")[0])
    resp = requests.get(base_url + path, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get(path: str, params: dict = None) -> dict:
    """Signed GET against the elections API (original behaviour)."""
    return _signed_get(BASE_URL, path, params)


def get_trading(path: str, params: dict = None) -> dict:
    """Signed GET against the general trading API."""
    return _signed_get(TRADING_URL, path, params)


def get_balance() -> dict:
    return get("/portfolio/balance")


def get_markets(params: dict = None) -> dict:
    return get("/markets", params=params)


def get_market(ticker: str) -> dict:
    return get(f"/markets/{ticker}")


def get_orderbook(ticker: str) -> dict:
    return get(f"/markets/{ticker}/orderbook")


# ── Prediction market helpers ─────────────────────────────────────────────────

# Categories that represent real prediction markets (not sports combos)
_PRED_MARKET_CATS = {
    "Elections", "Politics", "Economics", "Companies",
    "Financials", "Science and Technology", "Health",
    "World", "Climate and Weather", "Social", "Transportation",
    "Entertainment",
}


def _to_cents(v) -> float:
    """Normalise a Kalshi price to cents (0–100). API returns dollars (0–1) or cents (0–99)."""
    v = float(v)
    return v if v > 1 else round(v * 100, 2)


def _parse_market(m: dict, category: str = "", event_ticker: str = "") -> dict | None:
    """Parse a raw Kalshi market dict into a standardised arb-ready dict."""
    ya = m.get("yes_ask_dollars") or m.get("yes_ask")
    yb = m.get("yes_bid_dollars") or m.get("yes_bid")
    na = m.get("no_ask_dollars")  or m.get("no_ask")
    nb = m.get("no_bid_dollars")  or m.get("no_bid")
    if ya is None or yb is None:
        return None
    try:
        ya, yb = _to_cents(ya), _to_cents(yb)
        na = _to_cents(na) if na is not None else round(100 - yb, 2)
        nb = _to_cents(nb) if nb is not None else round(100 - ya, 2)
    except (ValueError, TypeError):
        return None
    # Skip markets with no active quotes
    if ya < 1 or ya > 99:
        return None
    ticker = m.get("ticker", "")
    # Derive event_ticker from the market's own field, caller override, or ticker prefix
    evt = (
        m.get("event_ticker")
        or event_ticker
        or ticker.rsplit("-", 1)[0]   # fallback: strip last hyphen segment
    )
    return {
        "ticker":       ticker,
        "event_ticker": evt,
        "title":        m.get("title", ""),
        "category":     category or m.get("category", ""),
        "yes_ask":      round(ya, 2),
        "yes_bid":      round(yb, 2),
        "no_ask":       round(na, 2),
        "no_bid":       round(nb, 2),
        "volume":       int(m.get("volume", 0) or 0),
        "close_time":   m.get("close_time", ""),
        "url":          f"https://kalshi.com/markets/{ticker}",
        "raw":          m,
    }


def get_open_prediction_markets(limit: int = 2000) -> list[dict]:
    """
    Fetch all open Kalshi prediction markets filtered to real event categories
    (Elections, Politics, Economics, etc.) — excludes sports combo parlays.

    Uses the events endpoint to walk by category so we only fetch relevant markets.
    Falls back to the trading API if accessible.

    Returns standardised dicts with: ticker, title, category, yes_ask, yes_bid,
    no_ask, no_bid, volume, close_time, url.
    """
    # Try cache first
    if _MARKET_CACHE_PATH.exists():
        try:
            cached = json.loads(_MARKET_CACHE_PATH.read_text())
            if time.time() - cached.get("ts", 0) < _MARKET_CACHE_TTL:
                return cached["markets"]
        except Exception:
            pass

    markets: list[dict] = []
    seen_tickers: set[str] = set()

    # ── Step 1: events-based fetch (elections API) ────────────────────────────
    # Walk all events, pick ones in prediction-market categories,
    # then fetch their individual markets.
    try:
        event_cursor = None
        events_processed = 0

        while len(markets) < limit:
            params = {"status": "open", "limit": 200}
            if event_cursor:
                params["cursor"] = event_cursor
            r = get("/events", params)
            event_batch = r.get("events", [])
            if not event_batch:
                break

            for event in event_batch:
                cat = event.get("category", "")
                if cat not in _PRED_MARKET_CATS:
                    continue
                eticker = event.get("event_ticker", "")
                if not eticker:
                    continue
                try:
                    er = get(f"/events/{eticker}")
                    for m in er.get("markets", []):
                        parsed = _parse_market(m, category=cat, event_ticker=eticker)
                        if parsed and parsed["ticker"] not in seen_tickers:
                            markets.append(parsed)
                            seen_tickers.add(parsed["ticker"])
                    time.sleep(0.08)
                    events_processed += 1
                except Exception:
                    pass

            event_cursor = r.get("cursor")
            if not event_cursor or len(event_batch) < 200:
                break
            time.sleep(0.1)

        print(f"[kalshi] Events-based fetch: {len(markets)} prediction markets from {events_processed} events")

    except Exception as e:
        print(f"[kalshi] Events-based fetch failed: {e}")

    # ── Step 2: trading API fallback (if elections API gave nothing useful) ───
    if len(markets) < 10:
        try:
            cursor = None
            while len(markets) < limit:
                params = {"status": "open", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                data  = get_trading("/markets", params)
                batch = data.get("markets", [])
                for m in batch:
                    parsed = _parse_market(m)
                    if parsed and parsed["ticker"] not in seen_tickers:
                        markets.append(parsed)
                        seen_tickers.add(parsed["ticker"])
                cursor = data.get("cursor")
                if not cursor or len(batch) < 200:
                    break
                time.sleep(0.1)
            print(f"[kalshi] Trading API fallback: {len(markets)} markets total")
        except Exception as e:
            print(f"[kalshi] Trading API fallback failed: {e}")

    # Cache
    _MARKET_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MARKET_CACHE_PATH.write_text(json.dumps({"ts": time.time(), "markets": markets}))
    return markets
