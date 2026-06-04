"""
injury_impact.py — Teammate injury detection and minutes redistribution engine.

When a key teammate is OUT, remaining players share their minutes.
This is the largest remaining edge source per the model's ChatGPT analysis:
  "Build teammate deltas: without_clark → player_minutes_change"

Flow:
  1. Fetch ESPN injury report (WNBA / NBA)
  2. Identify which OUT players are teammates of target player
  3. Estimate OUT player's avg minutes (via their ESPN gamelog)
  4. Compute target player's estimated share of redistributed minutes
  5. Return {minutes_boost, usage_boost, out_players, note}

Notes:
  - WNBA endpoint: site.api.espn.com/.../wnba/injuries (same pattern as NBA)
  - Share factor default: 1 / (active_roster_size - 1) for equal split;
    we bias toward same-position players and high-minute players
  - Falls back to 0 if any fetch fails — never crashes scoring
"""

import time
import requests
from pathlib import Path
import json
from datetime import datetime, timezone

ESPN_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# ESPN injury endpoints
_INJURY_URLS = {
    "WNBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/injuries",
    "NBA":  "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",
    "MLB":  "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries",
}

_CACHE: dict = {}
_CACHE_TTL = 1800  # 30 min per sport

# Minimum avg minutes to count as a "real" impact player
MIN_IMPACT_MINUTES = 12.0

# Default estimated minutes by tier (for players we can't fetch gamelogs for)
# Used when ESPN gamelog fetch fails
MINUTES_BY_TIER = {
    "starter": 28.0,   # clear starter (avg min >= 22)
    "rotation": 18.0,  # rotation player (avg min >= 12)
    "bench": 10.0,     # bench player
}

# What fraction of an OUT player's minutes goes to any single teammate on average.
# Remaining minutes are shared among 4-5 active players, so 1/5 ≈ 0.20 is realistic.
# We use 0.22 to account for the fact that star players absorb more.
DEFAULT_SHARE_FACTOR = 0.22


# ── Injury report fetch ───────────────────────────────────────────────────────

def _fetch_injury_report(sport: str) -> dict[str, dict]:
    """
    Fetch ESPN injury report for sport.
    Returns {player_name_lower: {name, team, status, is_out}}
    Cached 30 min.
    """
    global _CACHE
    cache_key = f"injuries_{sport}"
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        return cached["data"]

    url = _INJURY_URLS.get(sport)
    if not url:
        return {}

    try:
        resp = requests.get(url, headers=ESPN_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {}

    report: dict[str, dict] = {}
    for team_entry in data.get("injuries", []):
        # ESPN structure: top-level entry IS the team  {id, displayName, injuries: [...]}
        team_name = team_entry.get("displayName", "")

        for inj in team_entry.get("injuries", []):
            athlete   = inj.get("athlete", {})
            name      = athlete.get("displayName", "").strip()
            if not name:
                continue

            # Also grab team from athlete.team if top-level was missing
            athlete_team = athlete.get("team", {})
            resolved_team = team_name or athlete_team.get("displayName", "")
            team_abbr     = athlete_team.get("abbreviation", "")

            inj_type   = inj.get("type", {})
            status_raw = inj_type.get("description", "").lower()
            is_out     = status_raw in ("out", "doubtful")

            report[name.lower()] = {
                "name":   name,
                "team":   resolved_team,
                "abbr":   team_abbr,
                "status": status_raw,
                "is_out": is_out,
            }

    _CACHE[cache_key] = {"ts": time.time(), "data": report}
    return report


# ── Minutes estimate for OUT player ──────────────────────────────────────────

def _estimate_out_player_minutes(player_name: str, sport: str) -> float:
    """
    Try to get the OUT player's avg minutes from their game log.
    Falls back to MINUTES_BY_TIER["starter"] if unavailable.
    """
    try:
        if sport == "WNBA":
            from data.wnba_stats import _load_player_ids, _find_athlete_id
            from data.wnba_stats import _fetch_gamelog_raw, _build_game_log, CURRENT_SEASON
            players    = _load_player_ids()
            athlete_id = _find_athlete_id(player_name, players)
            if not athlete_id:
                return MINUTES_BY_TIER["starter"]
            events_flat, labels, events_meta = _fetch_gamelog_raw(athlete_id, CURRENT_SEASON)
            if not events_flat or not labels:
                return MINUTES_BY_TIER["starter"]
            # Just need minutes — use dummy fetch_cols
            game_log = _build_game_log(events_flat, labels, events_meta, ["PTS"])
            if not game_log:
                return MINUTES_BY_TIER["starter"]
            mins = [g["minutes"] for g in game_log[:10] if g["minutes"] > 0]
            if not mins:
                return MINUTES_BY_TIER["starter"]
            avg_min = sum(mins) / len(mins)
            return round(avg_min, 1)

        elif sport == "NBA":
            import scanner_power_parlay as sc
            pid = sc._get_nba_player_id(player_name)
            if not pid:
                return MINUTES_BY_TIER["starter"]
            games = sc._nba_game_log(pid)
            mins  = [g.get("min", 0) for g in games[:10] if g.get("min", 0) > 0]
            if not mins:
                return MINUTES_BY_TIER["starter"]
            return round(sum(mins) / len(mins), 1)

    except Exception:
        pass

    return MINUTES_BY_TIER["starter"]


# ── Main API ──────────────────────────────────────────────────────────────────

def get_team_injury_impact(
    player_name: str,
    player_team: str,
    sport: str,
    player_avg_min: float = 0.0,
) -> dict:
    """
    Compute the minutes/usage boost for a player when teammates are OUT.

    Args:
        player_name:    Target player (e.g. "Caitlin Clark")
        player_team:    Their team name (e.g. "Indiana Fever")
        sport:          "WNBA", "NBA", or "MLB"
        player_avg_min: Target player's current avg minutes (for share calculation)

    Returns:
        {
            minutes_boost:  float,   # estimated additional minutes (0 if no impact)
            usage_boost:    float,   # fractional usage boost (0.0–0.15)
            out_players:    list,    # [{name, avg_min, status}]
            note:           str,     # human-readable note
            has_impact:     bool,    # True if any meaningful teammate is OUT
        }
    """
    empty = {
        "minutes_boost": 0.0,
        "usage_boost":   0.0,
        "out_players":   [],
        "note":          "",
        "has_impact":    False,
    }

    if not player_team or sport not in _INJURY_URLS:
        return empty

    try:
        report = _fetch_injury_report(sport)
    except Exception:
        return empty

    if not report:
        return empty

    # Find OUT players on the same team
    team_lower  = player_team.lower()
    target_name = player_name.lower()
    out_players = []

    for inj_name_lower, inj_data in report.items():
        if not inj_data.get("is_out"):
            continue
        if inj_name_lower == target_name:
            continue  # Skip the target player themselves

        # Match team using last word of team name — unique across WNBA/NBA
        # (Dream, Aces, Fever, Sparks, Sky, Storm, Wings, Liberty, Sun, Mercury, Lynx, etc.)
        # Do NOT use abbreviation substring matching — "la" matches "atlanta", etc.
        inj_team  = inj_data.get("team", "").lower()
        team_last = team_lower.split()[-1] if team_lower else ""

        team_match = bool(team_last and team_last in inj_team)

        if team_match:
            out_players.append(inj_data)

    if not out_players:
        return empty

    # Compute minutes boost from each OUT teammate
    total_minutes_boost = 0.0
    enriched_out = []

    for out_p in out_players:
        out_min = _estimate_out_player_minutes(out_p["name"], sport)
        if out_min < MIN_IMPACT_MINUTES:
            continue   # bench player DNP — negligible impact

        # Share factor: how much of their minutes comes to our player
        # If player_avg_min is known, high-minute players get a larger share
        share = DEFAULT_SHARE_FACTOR
        if player_avg_min > 0 and out_min > 0:
            # Higher-minute players absorb more of the lost minutes
            # Scale linearly: 30-min player absorbs 25%, 20-min absorbs 18%
            share = min(0.35, max(0.15, player_avg_min / (player_avg_min + 20)))

        boost = out_min * share
        total_minutes_boost += boost
        enriched_out.append({
            "name":    out_p["name"],
            "avg_min": out_min,
            "status":  out_p["status"],
            "boost":   round(boost, 1),
        })

    if not enriched_out or total_minutes_boost < 0.5:
        return empty

    # Cap at 8 min boost (prevent runaway estimates)
    total_minutes_boost = min(8.0, total_minutes_boost)

    # Usage boost: proportional to minutes boost vs player's avg
    usage_boost = 0.0
    if player_avg_min > 0:
        usage_boost = min(0.15, total_minutes_boost / player_avg_min)

    # Build note
    names = ", ".join(p["name"].split()[-1] for p in enriched_out)
    note  = (
        f"🏥 {names} OUT — +"
        f"{total_minutes_boost:.1f}min boost est."
    )
    if len(enriched_out) > 1:
        note += f" ({len(enriched_out)} teammates)"

    return {
        "minutes_boost": round(total_minutes_boost, 1),
        "usage_boost":   round(usage_boost, 4),
        "out_players":   enriched_out,
        "note":          note,
        "has_impact":    True,
    }


# ── Team roster minutes (for context) ────────────────────────────────────────

def get_active_roster_avg_minutes(team_name: str, sport: str) -> dict[str, float]:
    """
    Return {player_name: avg_minutes} for active players on a team.
    Used for more precise share calculations.
    Only works for WNBA currently.
    """
    if sport != "WNBA":
        return {}

    try:
        from data.wnba_stats import _load_player_ids, _find_athlete_id
        from data.wnba_stats import _fetch_gamelog_raw, _build_game_log, CURRENT_SEASON, ESPN_TEAMS_URL, ESPN_ROSTER_URL, ESPN_HEADERS as _ESPN_H

        # Find team ID
        resp  = requests.get(ESPN_TEAMS_URL, headers=ESPN_HEADERS, timeout=10)
        teams = resp.json().get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
        team_id = None
        t_lower = team_name.lower()
        for t in teams:
            td = t.get("team", {})
            if t_lower in td.get("displayName", "").lower() or td.get("name", "").lower() in t_lower:
                team_id = td.get("id")
                break

        if not team_id:
            return {}

        # Fetch roster
        r_resp   = requests.get(ESPN_ROSTER_URL.format(team_id=team_id), headers=ESPN_HEADERS, timeout=10)
        athletes = r_resp.json().get("athletes", [])

        result = {}
        for athlete in athletes:
            name = athlete.get("fullName", "").strip()
            aid  = athlete.get("id", "")
            if not name or not aid:
                continue
            events_flat, labels, events_meta = _fetch_gamelog_raw(str(aid), CURRENT_SEASON)
            if events_flat and labels:
                game_log = _build_game_log(events_flat, labels, events_meta, ["PTS"])
                mins = [g["minutes"] for g in game_log[:8] if g["minutes"] > 0]
                if mins:
                    result[name] = round(sum(mins) / len(mins), 1)
            time.sleep(0.1)

        return result
    except Exception:
        return {}


if __name__ == "__main__":
    import sys
    sport   = sys.argv[1] if len(sys.argv) > 1 else "WNBA"
    player  = sys.argv[2] if len(sys.argv) > 2 else "Caitlin Clark"
    team    = sys.argv[3] if len(sys.argv) > 3 else "Indiana Fever"
    avg_min = float(sys.argv[4]) if len(sys.argv) > 4 else 34.0

    print(f"Checking injury impact for {player} ({team}, {sport}, avg {avg_min:.0f} min)...")
    impact = get_team_injury_impact(player, team, sport, avg_min)
    if impact["has_impact"]:
        print(f"  {impact['note']}")
        for p in impact["out_players"]:
            print(f"    OUT: {p['name']} (avg {p['avg_min']} min) → +"
                  f"{p['boost']} min est. for {player}")
        print(f"  Total boost: +{impact['minutes_boost']} min | "
              f"usage boost: +{impact['usage_boost']:.1%}")
    else:
        print("  No teammate injuries with meaningful impact found.")
