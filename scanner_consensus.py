"""
scanner_consensus.py — Compare PrizePicks / Underdog lines to sportsbook consensus.

Primary source: Action Network (free, no API key, NBA only).
Fallback:       The Odds API (uses credits — cached aggressively, NBA/NFL/MLB/NHL).

Consensus edge types:
  over  — platform line is BELOW consensus → bet OVER (easier than books think)
  under — platform line is ABOVE consensus → bet UNDER (easier than books think)

Multi-leg correlation:
  If the same player has 2+ stats all biased in the same direction vs consensus,
  flag as a correlated parlay — each leg supports the same "good/bad game" thesis.

Thresholds (user-tunable):
  MIN_ABS_DIFF = 2.0   absolute units
  MIN_PCT_DIFF = 0.20  20 percent
  Both must be met — avoids alerting on tiny differences for small-number stats.
"""

import unicodedata
from statistics import median
from pathlib import Path

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_ABS_DIFF  = 2.0   # minimum absolute gap between platform line and consensus
MIN_PCT_DIFF  = 0.20  # minimum percentage gap (20%)
MIN_BOOKS     = 2     # need at least this many books to trust consensus

# ── PP stat name → Action Network / Odds API market key ───────────────────────
STAT_TO_MARKET = {
    "Points":              "player_points",
    "Rebounds":            "player_rebounds",
    "Assists":             "player_assists",
    "3-Pointers Made":     "player_threes",
    "3-PT Made":           "player_threes",
    "Blocks":              "player_blocks",
    "Steals":              "player_steals",
    "Turnovers":           "player_turnovers",
    "Points+Rebounds+Assists": "player_points_rebounds_assists",
    "Points+Rebounds":     "player_points_rebounds",
    "Points+Assists":      "player_points_assists",
    "Rebounds+Assists":    "player_rebounds_assists",
}

# All PP league IDs covered by Action Network (NBA, MLB, NHL, NFL, WNBA)
AN_SUPPORTED_LEAGUES = {7, 84, 192, 237, 250, 2, 8, 227, 231, 9, 3, 252}


# ── Name normalization ────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    """Lowercase, strip accents, remove punctuation — for fuzzy name matching."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = nfkd.encode("ascii", "ignore").decode("ascii")
    return ascii_name.lower().strip().replace(".", "").replace("-", " ").replace("'", "")


# ── Consensus extraction ──────────────────────────────────────────────────────

def _extract_consensus(bookmakers: list[dict]) -> dict[tuple[str, str], float]:
    """
    Parse bookmaker data (Odds API or Action Network format) into a consensus map.

    Returns {(player_name_normalized, market_key): median_over_line}
    Only includes entries with at least MIN_BOOKS data points.
    """
    # Collect: {(norm_name, market_key): [line, line, ...]}
    raw: dict[tuple[str, str], list[float]] = {}

    for book in bookmakers:
        for market in book.get("markets", []):
            mkt_key = market.get("key", "")
            for outcome in market.get("outcomes", []):
                if outcome.get("name") != "Over":
                    continue
                player = outcome.get("description", "")
                point  = outcome.get("point")
                if not player or point is None:
                    continue
                key = (_normalize(player), mkt_key)
                raw.setdefault(key, []).append(float(point))

    return {
        key: median(lines)
        for key, lines in raw.items()
        if len(lines) >= MIN_BOOKS
    }


# ── Action Network consensus (NBA, free) ──────────────────────────────────────

def _get_an_consensus(league_ids: set[int] | None = None) -> dict[tuple[str, str], float]:
    """
    Fetch sportsbook consensus from Action Network (free, no key needed).
    Covers NBA, MLB, NHL, NFL, WNBA. Returns empty dict on error.
    """
    try:
        from data.action_network import get_all_consensus, get_events, get_player_props, LEAGUE_TO_SPORT, SPORT_CONFIG

        # Determine which sports to fetch
        if league_ids:
            sports = {LEAGUE_TO_SPORT[lid] for lid in league_ids if lid in LEAGUE_TO_SPORT}
        else:
            sports = set(SPORT_CONFIG.keys())

        consensus: dict[tuple[str, str], float] = {}
        for sport in sports:
            try:
                events = get_events(sport)
                for ev in events:
                    try:
                        props = get_player_props(ev["id"])
                        consensus.update(_extract_consensus(props.get("bookmakers", [])))
                    except Exception:
                        continue
            except Exception as e:
                print(f"[consensus] AN fetch failed for {sport}: {e}")
                continue

        return consensus
    except Exception as e:
        print(f"[consensus] Action Network fetch failed: {e}")
        return {}


# ── Main comparison logic ─────────────────────────────────────────────────────

def _compare(platform_line: float, consensus: float, player: str, stat: str,
             league: str, source: str, platform: str = "pp") -> dict | None:
    """
    Return an edge dict if the platform line differs from consensus beyond thresholds.
    Returns None if within acceptable range.
    """
    diff    = platform_line - consensus            # positive = platform set too high
    abs_diff = abs(diff)
    pct_diff = abs_diff / consensus if consensus > 0 else 0

    if abs_diff < MIN_ABS_DIFF or pct_diff < MIN_PCT_DIFF:
        return None

    direction = "under" if diff > 0 else "over"
    return {
        "platform":       platform,
        "player":         player,
        "stat":           stat,
        "league":         league,
        "platform_line":  platform_line,
        "consensus":      round(consensus, 2),
        "diff":           round(diff, 2),
        "abs_diff":       round(abs_diff, 2),
        "pct_diff":       round(pct_diff * 100, 1),
        "direction":      direction,   # "over" or "under"
        "source":         source,      # "action_network" or "odds_api"
    }


def find_consensus_edges(
    projections: list[dict],
    players: dict,
    league_id_map: dict | None = None,
) -> list[dict]:
    """
    Compare PP projections to sportsbook consensus.

    Parameters
    ----------
    projections : list of PP projection dicts (from _fetch_all_projections)
    players     : player dict keyed by player_id
    league_id_map : {projection_id: league_id} — built from raw PP response

    Returns list of edge dicts, sorted by abs_diff descending.
    """
    # ── Build consensus maps ──────────────────────────────────────────────────
    print("[consensus] Fetching Action Network consensus (NBA/MLB/NHL/NFL/WNBA)...")
    an_consensus = _get_an_consensus()
    print(f"[consensus] {len(an_consensus)} (player, stat) consensus lines from Action Network")

    edges: list[dict] = []

    for proj in projections:
        attrs   = proj.get("attributes", {})
        rels    = proj.get("relationships", {})
        pid     = rels.get("new_player", {}).get("data", {}).get("id", "")
        p_info  = players.get(pid, {})
        pname   = p_info.get("name", "")
        stat    = attrs.get("stat_type", "")
        league  = p_info.get("league", "")
        lid     = p_info.get("league_id", 0)

        # Only standard lines for consensus comparison
        line_score = attrs.get("line_score")
        if line_score is None:
            continue
        try:
            pp_line = float(line_score)
        except (TypeError, ValueError):
            continue

        # Map PP stat → market key
        market = STAT_TO_MARKET.get(stat)
        if not market:
            continue

        if lid not in AN_SUPPORTED_LEAGUES or not an_consensus:
            continue
        norm_name = _normalize(pname)
        consensus_line = an_consensus.get((norm_name, market))
        if consensus_line is None:
            continue
        edge = _compare(pp_line, consensus_line, pname, stat, league,
                        source="action_network", platform="pp")
        if edge:
            edges.append(edge)

    edges.sort(key=lambda e: e["abs_diff"], reverse=True)
    return edges


def find_ud_consensus_edges(grouped: dict) -> list[dict]:
    """
    Compare Underdog balanced lines to sportsbook consensus.

    Parameters
    ----------
    grouped : output of data.underdog.get_grouped_lines()
              Keys are (player_name, stat, sport_id).

    Returns list of edge dicts sorted by abs_diff descending.
    """
    print("[consensus] Fetching Action Network consensus for Underdog comparison (NBA/MLB/NHL/WNBA)...")
    an_consensus = _get_an_consensus()
    if not an_consensus:
        return []

    edges: list[dict] = []
    for (name, stat, sport), entry in grouped.items():
        balanced = entry.get("balanced")
        if balanced is None:
            continue
        if sport.upper() != "NBA":
            continue

        market = STAT_TO_MARKET.get(stat)
        if not market:
            continue

        norm_name = _normalize(name)
        consensus_line = an_consensus.get((norm_name, market))
        if consensus_line is None:
            continue

        edge = _compare(balanced, consensus_line, name, stat, sport,
                        source="action_network", platform="ud")
        if edge:
            edges.append(edge)

    edges.sort(key=lambda e: e["abs_diff"], reverse=True)
    return edges


# ── Multi-leg correlation ─────────────────────────────────────────────────────

def find_correlated_legs(edges: list[dict], min_legs: int = 2) -> list[dict]:
    """
    Group edges by player. If a player has min_legs+ edges all in the same direction,
    return them as a correlated parlay opportunity.

    Returns list of {player, direction, legs: [edge, ...], note}.
    """
    from collections import defaultdict
    by_player: dict[str, list[dict]] = defaultdict(list)
    for e in edges:
        by_player[e["player"]].append(e)

    correlated = []
    for player, player_edges in by_player.items():
        for direction in ("over", "under"):
            legs = [e for e in player_edges if e["direction"] == direction]
            if len(legs) >= min_legs:
                avg_edge = sum(e["pct_diff"] for e in legs) / len(legs)
                correlated.append({
                    "player":    player,
                    "direction": direction,
                    "legs":      legs,
                    "avg_pct":   round(avg_edge, 1),
                    "note": (
                        f"All {len(legs)} stats point {direction.upper()} — "
                        f"sportsbooks expect a {'big' if direction == 'over' else 'quiet'} game"
                    ),
                })

    correlated.sort(key=lambda c: (-len(c["legs"]), -c["avg_pct"]))
    return correlated


# ── Printing ──────────────────────────────────────────────────────────────────

def print_consensus_edges(edges: list[dict], correlated: list[dict], label: str = ""):
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*65}")
    print(f"CONSENSUS EDGE SCAN{' — ' + label if label else ''}")
    print(f"{'='*65}")
    print(f"  Scan time: {ts}")

    if edges:
        print(f"\n  INDIVIDUAL EDGES ({len(edges)}):")
        for e in edges:
            arrow = "BET OVER" if e["direction"] == "over" else "BET UNDER"
            print(
                f"  [{e['league']}] {e['player']} {e['stat']}: "
                f"{e['platform'].upper()} line={e['platform_line']} "
                f"consensus={e['consensus']} "
                f"diff={e['diff']:+.1f} ({e['pct_diff']}%) → {arrow}"
            )
    else:
        print("\n  No consensus edges found.")

    if correlated:
        print(f"\n  CORRELATED PARLAYS ({len(correlated)}):")
        for c in correlated:
            print(f"\n  ★ {c['player']} — BET {c['direction'].upper()} all legs")
            print(f"    {c['note']}")
            for leg in c["legs"]:
                print(f"    - {leg['stat']}: line={leg['platform_line']} vs consensus={leg['consensus']} ({leg['pct_diff']}% off)")

    print(f"{'='*65}\n")
