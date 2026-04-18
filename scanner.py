"""
Phase 1 scanner: detect edges between Kalshi sports props and sportsbook consensus.
No trading — logs opportunities only.

Run: python3 scanner.py
"""

import csv
import re
import time
from datetime import datetime, UTC
from pathlib import Path

from data import kalshi, odds, action_network as an
from models.edge import evaluate_market

PRICE_HISTORY_PATH = Path("logs/price_history.csv")
PRICE_HISTORY_FIELDS = [
    "timestamp", "kalshi_ticker", "description", "game",
    "yes_ask", "yes_bid", "fair_prob", "edge_vs_ask", "edge_vs_bid",
    "size_ask", "size_bid", "books_used",
]


def _log_price_snapshot(row: dict):
    """Append a price snapshot to price_history.csv (all matched markets, not just edges)."""
    PRICE_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not PRICE_HISTORY_PATH.exists()
    with open(PRICE_HISTORY_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PRICE_HISTORY_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

# ── Kalshi ticker parsing ─────────────────────────────────────────────────────
# Example: KXNBAPTS-26APR14MIACHA-CHALBALL1-20
# Breakdown: KXNBAPTS = points prop, 26APR14 = date, MIACHA = away+home
#            CHALBALL1 = player (CHA + BALL1), 20 = threshold

STAT_TYPE_MAP = {
    "KXNBAPTS": "player_points",
    "KXNBA3PT": "player_threes",
    "KXNBAAST": "player_assists",
    "KXNBATOV": "player_turnovers",
    "KXNBAREB": "player_rebounds",
}

# Map Kalshi team codes → full names for matching to Odds API
TEAM_NAME_MAP = {
    "MIA": "Miami Heat",
    "CHA": "Charlotte Hornets",
    "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers",
    "PHI": "Philadelphia 76ers",
    "ORL": "Orlando Magic",
    "LAC": "Los Angeles Clippers",
    "GSW": "Golden State Warriors",
    "CLE": "Cleveland Cavaliers",
    "TOR": "Toronto Raptors",
    "DEN": "Denver Nuggets",
    "MIN": "Minnesota Timberwolves",
    "NYK": "New York Knicks",
    "ATL": "Atlanta Hawks",
    "LAL": "Los Angeles Lakers",
    "HOU": "Houston Rockets",
    "BOS": "Boston Celtics",
    "BKN": "Brooklyn Nets",
    "CHI": "Chicago Bulls",
    "DAL": "Dallas Mavericks",
    "DET": "Detroit Pistons",
    "IND": "Indiana Pacers",
    "MEM": "Memphis Grizzlies",
    "MIL": "Milwaukee Bucks",
    "NOP": "New Orleans Pelicans",
    "OKC": "Oklahoma City Thunder",
    "SAC": "Sacramento Kings",
    "SAS": "San Antonio Spurs",
    "UTA": "Utah Jazz",
    "WAS": "Washington Wizards",
}


def parse_kalshi_ticker(ticker: str) -> dict | None:
    """
    Parse a Kalshi NBA prop ticker into components.
    Returns None if unrecognized format.
    """
    # KXNBAPTS-26APR14MIACHA-CHALBALL1-20
    parts = ticker.split("-")
    if len(parts) < 4:
        return None

    stat_key = parts[0]
    if stat_key not in STAT_TYPE_MAP:
        return None

    # Parse date+teams from parts[1]: e.g. 26APR14MIACHA
    date_teams = parts[1]
    m = re.match(r"\d{2}[A-Z]{3}\d{2}([A-Z]{3})([A-Z]{3})", date_teams)
    if not m:
        return None
    away_code, home_code = m.group(1), m.group(2)

    # Parse player + threshold from parts[2] and parts[3]
    # parts[2] = e.g. CHALBALL1 (team code + player slug)
    # parts[3] = threshold integer
    try:
        threshold = int(parts[3])
    except ValueError:
        return None

    # Extract player name slug (strip 3-char team prefix)
    player_slug = parts[2][3:]  # e.g. "BALL1", "HERRO14", "MILLER24"

    return {
        "stat_type": STAT_TYPE_MAP[stat_key],
        "away_team": TEAM_NAME_MAP.get(away_code),
        "home_team": TEAM_NAME_MAP.get(home_code),
        "player_slug": player_slug,
        "threshold": threshold,
    }


def match_player_to_book(player_slug: str, book_outcomes: list[dict]) -> list[tuple]:
    """
    Match a Kalshi player slug to sportsbook outcomes.
    Returns list of (over_odds, under_odds, book_line) tuples across books.
    Uses fuzzy last-name matching on the slug.
    """
    # Extract last name fragment from slug (strip leading digits/numbers)
    name_fragment = re.sub(r"\d", "", player_slug).upper()

    matches = []
    for outcome_pair in book_outcomes:
        player_name = outcome_pair.get("description", "").upper()
        last_name = player_name.split()[-1] if player_name else ""
        if name_fragment in last_name or last_name in name_fragment:
            matches.append(outcome_pair)
    return matches


def get_fair_prob_for_market(parsed: dict, odds_data: dict) -> tuple[float, int] | None:
    """
    Find the vig-adjusted fair probability for a Kalshi market from sportsbook data.
    Returns (fair_prob, books_used) or None if insufficient data.
    """
    stat_type = parsed["stat_type"]
    threshold = parsed["threshold"]
    player_slug = parsed["player_slug"]

    book_probs = []

    for bookmaker in odds_data.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market["key"] != stat_type:
                continue

            outcomes = market["outcomes"]
            # Group into over/under pairs by (player, point) — a book may offer
            # multiple lines for the same player (e.g. Bovada at 7.5, 8.5, 9.5)
            pairs = {}
            for o in outcomes:
                key = (o["description"], o["point"])
                if key not in pairs:
                    pairs[key] = {}
                pairs[key][o["name"]] = o

            for (player_name, book_line), sides in pairs.items():
                if "Over" not in sides or "Under" not in sides:
                    continue

                over = sides["Over"]
                under = sides["Under"]

                # Check if this player matches our slug
                name_fragment = re.sub(r"\d", "", player_slug).upper()
                last_name = player_name.split()[-1].upper()
                if name_fragment not in last_name and last_name not in name_fragment:
                    continue

                fair_over, _ = odds.remove_vig(over["price"], under["price"])

                # Kalshi threshold N means "N or more" = same as sportsbook "over N-0.5"
                # So the correct match is book_line == threshold - 0.5
                # e.g. kalshi=20 matches book_line=19.5 (diff = -0.5) ✓
                # e.g. kalshi=1  matches book_line=0.5  (diff = -0.5) ✓
                # e.g. kalshi=1  vs book_line=1.5       (diff = +0.5) ✗ — "over 1.5" means 2+
                diff = book_line - threshold
                if not (-0.6 < diff < 0.0):
                    continue

                # Small adjustment for the 0.5 gap (P(>=N) ≈ P(>N-0.5) with tiny haircut)
                fair_over *= 0.99

                book_probs.append(fair_over)

    if not book_probs:
        return None

    return sum(book_probs) / len(book_probs), len(book_probs)


def scan_nba_markets():
    """Scan all open Kalshi NBA prop markets and log edges vs sportsbook consensus."""
    print(f"\n{'='*60}")
    print(f"SCAN START: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}")

    # 1. Get tonight's NBA events — Odds API with Action Network fallback
    event_odds = {}
    odds_api_ok = False
    try:
        events = odds.get_events("nba")
        odds_api_ok = True
    except Exception as e:
        print(f"[odds] Unavailable ({e}) — switching to Action Network")

    if odds_api_ok:
        for event in events:
            key = (event["away_team"], event["home_team"])
            try:
                odds_data = odds.get_player_props(
                    "nba", event["id"],
                    ["player_points", "player_threes", "player_assists", "player_rebounds"],
                )
                event_odds[key] = odds_data
                print(f"[odds] Fetched {event['away_team']} @ {event['home_team']}")
            except Exception as e:
                if "401" in str(e) or "Unauthorized" in str(e):
                    print(f"[odds] Credits exhausted — switching to Action Network")
                    odds_api_ok = False
                    break
                print(f"[warn] {event['away_team']} @ {event['home_team']}: {e}")
            time.sleep(0.5)

    if not odds_api_ok or not event_odds:
        print("[an]  Fetching from Action Network...")
        try:
            an_events = an.get_events()
            for event in an_events:
                if event.get("status") == "complete":
                    continue
                key = (event["away_team"], event["home_team"])
                try:
                    event_odds[key] = an.get_player_props(event["id"])
                    print(f"[an]  Fetched {event['away_team']} @ {event['home_team']}")
                except Exception as e:
                    print(f"[an]  warn {event['away_team']} @ {event['home_team']}: {e}")
                time.sleep(0.3)
        except Exception as e:
            print(f"[ERROR] Action Network also failed: {e}")

    # 2. Fetch Kalshi NBA prop markets
    try:
        result = kalshi.get("/markets", {"limit": 200, "status": "open"})
        # Also try specific series tickers
        nba_markets = []
        for prefix in ["KXNBAPTS", "KXNBA3PT", "KXNBAAST", "KXNBAREB"]:
            r = kalshi.get("/markets", {"limit": 200, "status": "open", "series_ticker": prefix})
            nba_markets.extend(r.get("markets", []))
        print(f"\n[kalshi] Found {len(nba_markets)} NBA prop markets")
    except Exception as e:
        print(f"[ERROR] Failed to fetch Kalshi markets: {e}")
        return

    # 3. Evaluate each market
    edges_found = 0
    evaluated = 0
    edge_opps = []

    for market in nba_markets:
        ticker = market.get("ticker", "")
        parsed = parse_kalshi_ticker(ticker)
        if not parsed:
            continue

        # Find matching odds data
        away, home = parsed["away_team"], parsed["home_team"]
        if not away or not home:
            continue

        odds_data = event_odds.get((away, home)) or event_odds.get((home, away))
        if not odds_data:
            continue

        result = get_fair_prob_for_market(parsed, odds_data)
        if not result:
            continue

        fair_prob, books_used = result

        yes_ask = market.get("yes_ask_dollars")
        yes_bid = market.get("yes_bid_dollars")
        if not yes_ask or not yes_bid or float(yes_ask) == 0:
            continue

        yes_ask_size = float(market.get("yes_ask_size_fp", 0) or 0)
        yes_bid_size = float(market.get("yes_bid_size_fp", 0) or 0)
        ask_size = max(yes_ask_size, yes_bid_size)

        # Log price snapshot for ALL matched markets (time-series data)
        edge_vs_ask = round(fair_prob - float(yes_ask), 4)
        edge_vs_bid = round((1 - fair_prob) - (1 - float(yes_bid)), 4)
        _log_price_snapshot({
            "timestamp": datetime.now(UTC).isoformat(),
            "kalshi_ticker": ticker,
            "description": market.get("title", ticker)[:60],
            "game": f"{away} @ {home}",
            "yes_ask": yes_ask,
            "yes_bid": yes_bid,
            "fair_prob": round(fair_prob, 4),
            "edge_vs_ask": edge_vs_ask,
            "edge_vs_bid": edge_vs_bid,
            "size_ask": yes_ask_size,
            "size_bid": yes_bid_size,
            "books_used": books_used,
        })

        # group_id: player slug + game code for correlation tracking
        game_code = f"{parsed['away_team'].split()[-1][:3].upper()}{parsed['home_team'].split()[-1][:3].upper()}"
        group_id = f"{parsed['player_slug']}_{game_code}"

        opp = evaluate_market(
            kalshi_ticker=ticker,
            description=market.get("subtitle", market.get("title", ticker))[:60],
            kalshi_yes_ask=float(yes_ask),
            kalshi_yes_bid=float(yes_bid),
            kalshi_ask_size=ask_size,
            fair_prob=fair_prob,
            group_id=group_id,
            books_used=books_used,
            notes=f"{away} @ {home}",
        )

        if opp is None:
            continue  # below liquidity threshold

        evaluated += 1
        if opp.clears_threshold:
            edges_found += 1
            edge_opps.append(opp)

    # ── Dashboard ─────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"SCAN COMPLETE: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}")
    print(f"  Markets evaluated : {evaluated}")
    print(f"  Edges found       : {edges_found}")
    if edge_opps:
        avg_edge = sum(o.best_edge for o in edge_opps) / len(edge_opps)
        avg_size = sum(o.kalshi_size_ask for o in edge_opps) / len(edge_opps)
        avg_quality = sum(o.edge_quality for o in edge_opps) / len(edge_opps)
        total_ev = sum(o.best_edge for o in edge_opps)
        print(f"  Avg edge          : {avg_edge:.2%}")
        print(f"  Avg size          : ${avg_size:.0f}")
        print(f"  Avg quality score : {avg_quality:.1f}")
        print(f"  Sum EV (equal $1) : {total_ev:.2%}")
        print(f"\n  Top 5 by quality:")
        top5 = sorted(edge_opps, key=lambda o: o.edge_quality, reverse=True)[:5]
        for o in top5:
            print(f"    {o.description[:40]:<40} edge={o.best_edge:.1%} size=${o.kalshi_size_ask:.0f} quality={o.edge_quality:.1f} [{o.liquidity_tier}]")
    print(f"\n  Log: logs/edges.csv")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    scan_nba_markets()
