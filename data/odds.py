import json
import os
import time
import requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

BASE_URL = "https://api.the-odds-api.com/v4"
API_KEY = os.getenv("ODDS_API_KEY")

# Cache sportsbook responses to disk — avoid burning API credits on repeat scans
_CACHE_DIR = Path("logs/.odds_cache")
_CACHE_TTL = 7200  # seconds — re-fetch after 2 hours


def _cache_get(key: str):
    path = _CACHE_DIR / f"{key}.json"
    if path.exists():
        data = json.loads(path.read_text())
        if time.time() - data["ts"] < _CACHE_TTL:
            return data["payload"]
    return None


def _cache_set(key: str, payload):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{key}.json"
    path.write_text(json.dumps({"ts": time.time(), "payload": payload}))

SPORT_KEYS = {
    "nba": "basketball_nba",
    "nfl": "americanfootball_nfl",
    "mlb": "baseball_mlb",
    "nhl": "icehockey_nhl",
}


def get_player_props(sport: str, event_id: str, markets: list[str]) -> dict:
    """Fetch player prop odds for a specific event. Results cached for 1 hour."""
    mkt_key = "_".join(sorted(markets))
    cache_key = f"props_{sport}_{event_id}_{mkt_key}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = {
        "apiKey": API_KEY,
        "regions": "us",
        "markets": ",".join(markets),
        "oddsFormat": "american",
    }
    resp = requests.get(
        f"{BASE_URL}/sports/{SPORT_KEYS[sport]}/events/{event_id}/odds",
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _cache_set(cache_key, data)
    return data


def get_events(sport: str) -> list[dict]:
    """Fetch upcoming events for a sport. Results cached for 1 hour."""
    cache_key = f"events_{sport}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = {"apiKey": API_KEY, "dateFormat": "iso"}
    resp = requests.get(
        f"{BASE_URL}/sports/{SPORT_KEYS[sport]}/events",
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _cache_set(cache_key, data)
    return data


def american_to_implied(odds: int) -> float:
    """Convert American odds to implied probability (raw, before vig removal)."""
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)


def remove_vig(over_odds: int, under_odds: int) -> tuple[float, float]:
    """
    Remove vig from a two-sided market.
    Returns (fair_over_prob, fair_under_prob) that sum to 1.0.
    """
    raw_over = american_to_implied(over_odds)
    raw_under = american_to_implied(under_odds)
    total = raw_over + raw_under
    return raw_over / total, raw_under / total


def consensus_prob(books: list[tuple[int, int]]) -> float:
    """
    Average fair over probability across multiple books.
    books: list of (over_odds, under_odds) tuples.
    """
    fair_probs = [remove_vig(o, u)[0] for o, u in books]
    return sum(fair_probs) / len(fair_probs)
