"""
injury_impact.py — Teammate injury detection with "already priced in" protection.

The critical insight (per ChatGPT review):

  If Jones has been OUT for 10 games, Howard's L3/L5 minutes ALREADY reflect
  life without Jones. Applying an additional boost is double-counting and
  silently inflates projections.

Three-state model:
  State A: Teammate active    → no adjustment (baseline)
  State B: Teammate new OUT   → apply adjustment (new information)
  State C: Teammate long-term → skip (already priced into recent data)

Threshold: if injured teammate missed >= PRICED_IN_THRESHOLD of the target
player's most recent games, the absence is considered already priced in.

Adjustment methods (in priority order):
  1. WOWY (With/Without You): compare target player's avg minutes in games
     WITH the teammate vs games WITHOUT. Requires ≥ 3 games in each bucket.
  2. Redistribution estimate: share of OUT player's avg minutes (fallback).

Every result includes injury_adjustment_source so it's transparent which
method was used and whether the boost is evidence-based or estimated.
"""

import time
import requests
from datetime import datetime, timezone

ESPN_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

_INJURY_URLS = {
    "WNBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/injuries",
    "NBA":  "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",
    "MLB":  "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries",
}

_CACHE: dict = {}
_CACHE_TTL   = 1800   # 30 min

# Injury is "already priced in" if teammate missed this many of target's recent games
PRICED_IN_THRESHOLD = 5

# Minimum avg minutes to count as a meaningful impact player
MIN_IMPACT_MINUTES = 12.0

# Share of OUT player's minutes that flows to any single high-minute teammate
# (roughly 1/4 since ~4 players share those minutes)
DEFAULT_SHARE_FACTOR = 0.22

# Fallback minute estimate when we can't fetch the OUT player's gamelog
STARTER_FALLBACK_MIN = 28.0


# ── Injury report ─────────────────────────────────────────────────────────────

def _fetch_injury_report(sport: str) -> dict[str, dict]:
    """
    Returns {player_name_lower: {name, team, status, is_out}}.
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
    except Exception:
        return {}

    report: dict[str, dict] = {}
    for team_entry in data.get("injuries", []):
        team_name = team_entry.get("displayName", "")
        for inj in team_entry.get("injuries", []):
            athlete      = inj.get("athlete", {})
            name         = athlete.get("displayName", "").strip()
            if not name:
                continue
            athlete_team  = athlete.get("team", {})
            resolved_team = team_name or athlete_team.get("displayName", "")
            inj_type      = inj.get("type", {})
            status_raw    = inj_type.get("description", "").lower()
            report[name.lower()] = {
                "name":   name,
                "team":   resolved_team,
                "status": status_raw,
                "is_out": status_raw in ("out", "doubtful"),
            }

    _CACHE[cache_key] = {"ts": time.time(), "data": report}
    return report


# ── OUT player game log ───────────────────────────────────────────────────────

def _get_out_player_game_data(player_name: str, sport: str) -> dict:
    """
    Fetch the OUT player's recent game log.
    Returns {played_dates: set[str], avg_min: float, n_games: int}

    played_dates: ISO date strings (YYYY-MM-DD) when they played this season.
    Used to:
      1. Count how many of the target player's games they missed (priced-in check)
      2. WOWY: classify each of the target player's games as with/without
    """
    result = {"played_dates": set(), "avg_min": STARTER_FALLBACK_MIN, "n_games": 0}

    try:
        if sport == "WNBA":
            from data.wnba_stats import (_load_player_ids, _find_athlete_id,
                                         _fetch_gamelog_raw, _build_game_log, CURRENT_SEASON)
            players    = _load_player_ids()
            athlete_id = _find_athlete_id(player_name, players)
            if not athlete_id:
                return result
            events_flat, labels, events_meta = _fetch_gamelog_raw(athlete_id, CURRENT_SEASON)
            if not events_flat or not labels:
                return result
            game_log = _build_game_log(events_flat, labels, events_meta, ["PTS"])
            if not game_log:
                return result

            mins   = [g["minutes"] for g in game_log if g["minutes"] > 0]
            dates  = {g["date"] for g in game_log if g.get("date")}
            result["played_dates"] = dates
            result["avg_min"]      = round(sum(mins) / len(mins), 1) if mins else STARTER_FALLBACK_MIN
            result["n_games"]      = len(game_log)

        elif sport == "NBA":
            import scanner_power_parlay as sc
            pid = sc._get_nba_player_id(player_name)
            if not pid:
                return result
            games = sc._nba_game_log(pid)
            mins  = [g.get("min", 0) for g in games[:10] if g.get("min", 0) > 0]
            dates = {g.get("date", "")[:10] for g in games if g.get("date")}
            result["played_dates"] = dates
            result["avg_min"]      = round(sum(mins) / len(mins), 1) if mins else STARTER_FALLBACK_MIN
            result["n_games"]      = len(games)

    except Exception:
        pass

    return result


# ── Priced-in detection ───────────────────────────────────────────────────────

def _count_games_missed(out_player_dates: set[str], target_game_log: list) -> int:
    """
    Count how many of the target player's recent games the OUT player missed.

    Logic: find the OUT player's most recent game date. Any target game played
    AFTER that date is a game the OUT player missed.

    Special case: if the OUT player has NO games this season (out_dates is empty),
    they've been out the entire season. The target player's recent stats already
    fully reflect life without them → return PRICED_IN_THRESHOLD immediately.
    """
    valid_dates = [d for d in out_player_dates if d]

    if not valid_dates:
        # No games played this season at all — the entire season is without them.
        # This is the maximum "already priced in" scenario.
        # Only apply if the target player has actually played some games.
        if target_game_log and len(target_game_log) >= 3:
            return PRICED_IN_THRESHOLD  # triggers the skip
        return 0  # too early to say, be conservative

    if not target_game_log:
        return 0

    last_played = max(valid_dates)

    # Count target player's recent games that happened after OUT player's last game
    missed = sum(
        1 for g in target_game_log[:10]
        if g.get("date", "") > last_played
    )
    return missed


# ── WOWY (With/Without You) ───────────────────────────────────────────────────

def _compute_wowy(target_game_log: list, out_player_dates: set[str]) -> dict | None:
    """
    Split the target player's game log into games WITH and WITHOUT the teammate.
    Requires at least 3 games in each bucket to be reliable.

    Returns:
        {with_avg_min, without_avg_min, diff_min, n_with, n_without}
    or None if insufficient data.
    """
    if not target_game_log or not out_player_dates:
        return None

    with_games    = [g for g in target_game_log if g.get("date") in out_player_dates]
    without_games = [g for g in target_game_log if g.get("date") and
                     g["date"] not in out_player_dates]

    if len(with_games) < 3 or len(without_games) < 3:
        return None

    with_mins    = [g["minutes"] for g in with_games    if g.get("minutes", 0) > 0]
    without_mins = [g["minutes"] for g in without_games if g.get("minutes", 0) > 0]

    if not with_mins or not without_mins:
        return None

    with_avg    = sum(with_mins)    / len(with_mins)
    without_avg = sum(without_mins) / len(without_mins)

    return {
        "with_avg_min":    round(with_avg,    1),
        "without_avg_min": round(without_avg, 1),
        "diff_min":        round(without_avg - with_avg, 1),  # positive = plays more without
        "n_with":          len(with_mins),
        "n_without":       len(without_mins),
    }


# ── Main API ──────────────────────────────────────────────────────────────────

def get_team_injury_impact(
    player_name:    str,
    player_team:    str,
    sport:          str,
    player_avg_min: float = 0.0,
    player_game_log: list  = None,   # target player's full game log for WOWY
) -> dict:
    """
    Compute the minutes boost for a player when teammates are OUT.

    Implements double-counting protection:
      - If OUT teammate missed >= 5 of the target's recent games, the absence
        is already priced into the target's L3/L5/season minutes → skip.
      - If the absence is new, attempt WOWY first (historical evidence),
        then fall back to roster redistribution estimate.

    Returns:
        {
            minutes_boost:             float,
            usage_boost:               float,
            out_players:               list[dict],
            out_players_skipped:       list[dict],   # priced-in teammates
            note:                      str,
            has_impact:                bool,
            injury_adjustment_source:  str,           # "WOWY" | "Redistribution" | ""
        }
    """
    empty = {
        "minutes_boost":            0.0,
        "usage_boost":              0.0,
        "out_players":              [],
        "out_players_skipped":      [],
        "note":                     "",
        "has_impact":               False,
        "injury_adjustment_source": "",
    }

    if not player_team or sport not in _INJURY_URLS:
        return empty

    try:
        report = _fetch_injury_report(sport)
    except Exception:
        return empty

    if not report:
        return empty

    # ── Identify OUT teammates ────────────────────────────────────────────────
    team_lower  = player_team.lower()
    target_name = player_name.lower()
    team_last   = team_lower.split()[-1] if team_lower else ""

    raw_out = []
    for inj_name_lower, inj_data in report.items():
        if not inj_data.get("is_out"):
            continue
        if inj_name_lower == target_name:
            continue
        inj_team = inj_data.get("team", "").lower()
        if team_last and team_last in inj_team:
            raw_out.append(inj_data)

    if not raw_out:
        return empty

    # ── Three-state classification ────────────────────────────────────────────
    total_minutes_boost = 0.0
    enriched_out        = []
    skipped_priced_in   = []
    methods_used        = []

    for out_p in raw_out:
        # Fetch OUT player's game data (played dates + avg minutes)
        out_data   = _get_out_player_game_data(out_p["name"], sport)
        out_dates  = out_data["played_dates"]
        out_avg_min = out_data["avg_min"]

        if out_avg_min < MIN_IMPACT_MINUTES:
            continue  # bench player — negligible impact

        # ── STATE CHECK: is this absence already priced in? ───────────────────
        games_missed = _count_games_missed(out_dates, player_game_log or [])

        if games_missed >= PRICED_IN_THRESHOLD:
            # The target player's recent sample was built WITHOUT this teammate.
            # L3/L5/season numbers already reflect this reality — no boost needed.
            skipped_priced_in.append({
                "name":         out_p["name"],
                "status":       out_p["status"],
                "games_missed": games_missed,
                "reason":       f"already priced in ({games_missed} games missed)",
            })
            continue

        # ── STATE B: new or recent absence — apply adjustment ─────────────────
        # Try WOWY first (evidence-based)
        wowy = _compute_wowy(player_game_log or [], out_dates)

        if wowy and wowy["diff_min"] > 0:
            # Historical evidence: player plays MORE minutes without this teammate
            boost  = wowy["diff_min"]
            method = "WOWY"
            wowy_detail = wowy
        else:
            # Fallback: redistribute a share of OUT player's minutes
            share  = min(0.30, max(0.15, player_avg_min / (player_avg_min + 20))) if player_avg_min > 0 else DEFAULT_SHARE_FACTOR
            boost  = out_avg_min * share
            method = "Redistribution estimate"
            wowy_detail = None

        total_minutes_boost += boost
        methods_used.append(method)

        enriched_out.append({
            "name":         out_p["name"],
            "avg_min":      out_avg_min,
            "status":       out_p["status"],
            "boost":        round(boost, 1),
            "games_missed": games_missed,
            "method":       method,
            "wowy":         wowy_detail,
        })

    if not enriched_out or total_minutes_boost < 0.5:
        result = dict(empty)
        result["out_players_skipped"] = skipped_priced_in
        # Add a note if everything was priced in
        if skipped_priced_in:
            names = ", ".join(p["name"].split()[-1] for p in skipped_priced_in)
            result["note"] = f"📊 {names} OUT but already priced in ({skipped_priced_in[0]['games_missed']} games)"
        return result

    # Cap at 8 min to prevent runaway estimates
    total_minutes_boost = min(8.0, total_minutes_boost)

    usage_boost = min(0.15, total_minutes_boost / player_avg_min) if player_avg_min > 0 else 0.0

    # Determine primary method
    primary_method = "WOWY" if any(m == "WOWY" for m in methods_used) else "Redistribution estimate"

    # Build note
    names      = ", ".join(p["name"].split()[-1] for p in enriched_out)
    method_tag = "📈 WOWY" if primary_method == "WOWY" else "📊 Est."
    note       = f"🏥 {names} OUT — {method_tag} +{total_minutes_boost:.1f}min"
    if len(enriched_out) > 1:
        note += f" ({len(enriched_out)} teammates)"
    if skipped_priced_in:
        skip_names = ", ".join(p["name"].split()[-1] for p in skipped_priced_in)
        note += f" | {skip_names} already priced in"

    return {
        "minutes_boost":            round(total_minutes_boost, 1),
        "usage_boost":              round(usage_boost, 4),
        "out_players":              enriched_out,
        "out_players_skipped":      skipped_priced_in,
        "note":                     note,
        "has_impact":               True,
        "injury_adjustment_source": primary_method,
    }


if __name__ == "__main__":
    import sys
    sport   = sys.argv[1] if len(sys.argv) > 1 else "WNBA"
    player  = sys.argv[2] if len(sys.argv) > 2 else "Rhyne Howard"
    team    = sys.argv[3] if len(sys.argv) > 3 else "Atlanta Dream"
    avg_min = float(sys.argv[4]) if len(sys.argv) > 4 else 32.0

    print(f"\nChecking injury impact for {player} ({team}, {sport})...")

    # For a real test, load game log
    game_log = []
    try:
        from data.wnba_stats import _load_player_ids, _find_athlete_id, _fetch_gamelog_raw, _build_game_log, CURRENT_SEASON
        players = _load_player_ids()
        aid = _find_athlete_id(player, players)
        if aid:
            ef, labels, em = _fetch_gamelog_raw(aid, CURRENT_SEASON)
            if ef and labels:
                game_log = _build_game_log(ef, labels, em, ["PTS"])
                print(f"  Loaded {len(game_log)} games for {player}")
    except Exception as e:
        print(f"  Could not load game log: {e}")

    impact = get_team_injury_impact(player, team, sport, avg_min, game_log)

    print(f"\n  has_impact: {impact['has_impact']}")
    print(f"  minutes_boost: +{impact['minutes_boost']}")
    print(f"  method: {impact['injury_adjustment_source']}")
    print(f"  note: {impact['note']}")

    if impact["out_players"]:
        print(f"\n  Active adjustments:")
        for p in impact["out_players"]:
            wowy = p.get("wowy")
            if wowy:
                print(f"    {p['name']}: +{p['boost']} min via WOWY "
                      f"(with={wowy['with_avg_min']} → without={wowy['without_avg_min']}, "
                      f"n={wowy['n_with']}/{wowy['n_without']})")
            else:
                print(f"    {p['name']}: +{p['boost']} min via redistribution "
                      f"(avg={p['avg_min']} min, {p['games_missed']} games missed)")

    if impact["out_players_skipped"]:
        print(f"\n  Skipped (already priced in):")
        for p in impact["out_players_skipped"]:
            print(f"    {p['name']}: {p['reason']}")
