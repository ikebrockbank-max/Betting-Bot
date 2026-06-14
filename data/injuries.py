"""
ESPN injury report fetcher — covers WNBA, NBA, and MLB.

Uses ESPN's unofficial public API (no key required, sourced from RotoWire).
Covers Out, Doubtful, Questionable, and Day-To-Day designations.

Usage:
    report = get_injury_report("WNBA")
    entry  = check_player("Aneesah Morrow", report)
    if entry and entry["disqualified"]:
        # skip this pick
"""

import time
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json",
}

_ESPN_URLS = {
    "WNBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/injuries",
    "NBA":  "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",
    "MLB":  "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries",
}

# Status strings that mean the player won't play → skip pick entirely
DISQUALIFYING_STATUSES = {"out", "doubtful"}
# Status strings that mean risk but player might play → flag + reduce confidence
WARNING_STATUSES = {"questionable", "day-to-day", "dtd"}

# Process-level cache per sport — injury reports don't change by the minute
_CACHE: dict[str, dict] = {}       # sport → report dict
_CACHE_TS: dict[str, float] = {}   # sport → fetch timestamp
_CACHE_TTL = 900                   # 15 minutes


def get_injury_report(sport: str = "NBA") -> dict[str, dict]:
    """
    Fetch current injury report for the given sport from ESPN.

    Returns dict keyed by lowercase player name:
        {
          "name":         "Aneesah Morrow",
          "status":       "Questionable",       # raw designation
          "detail":       "Left Leg",           # body part / type
          "headline":     "Morrow (leg) listed as questionable for Saturday",
          "disqualified": bool,   # Out / Doubtful → skip this pick
          "warning":      bool,   # Questionable / DTD → flag but don't skip
        }

    Returns {} on error — never raises, so a failed fetch never blocks scoring.
    """
    sport = sport.upper()
    url   = _ESPN_URLS.get(sport)
    if not url:
        return {}

    # Return cached report if fresh
    if sport in _CACHE and (time.time() - _CACHE_TS.get(sport, 0)) < _CACHE_TTL:
        return _CACHE[sport]

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[injuries] ESPN {sport} fetch failed: {e}")
        return _CACHE.get(sport, {})   # return stale cache rather than empty

    report: dict[str, dict] = {}

    for team_entry in data.get("injuries", []):
        for inj in team_entry.get("injuries", []):
            athlete   = inj.get("athlete", {})
            name      = athlete.get("displayName", "").strip()
            if not name:
                continue

            inj_type   = inj.get("type", {})
            status_raw = inj_type.get("description", "").lower().strip()
            detail_obj = inj.get("details", {})
            detail     = detail_obj.get("type", "")

            short    = inj.get("shortComment", "") or inj.get("longComment", "")
            headline = short[:140].split(". ")[0] if short else status_raw.title()

            # MLB injured list ("10-day il", "60-day il", "7-day il") = can't play
            is_il = "il" in status_raw and "day" in status_raw
            disqualified = status_raw in DISQUALIFYING_STATUSES or is_il

            report[name.lower()] = {
                "name":         name,
                "status":       status_raw.title(),
                "detail":       detail,
                "headline":     headline,
                "disqualified": disqualified,
                "warning":      status_raw in WARNING_STATUSES and not disqualified,
            }

    _CACHE[sport]    = report
    _CACHE_TS[sport] = time.time()
    return report


def check_player(name: str, report: dict[str, dict]) -> dict | None:
    """
    Look up a player in the injury report by name (case-insensitive).
    Tries exact match first, then last-name-only match as fallback.
    Returns their injury entry or None if healthy / not listed.
    """
    if not report:
        return None

    key = name.lower().strip()

    # Exact match
    if key in report:
        return report[key]

    # Last-name match — catches "A. Morrow" style abbreviations
    last = key.split()[-1] if key else ""
    for k, v in report.items():
        if k.split()[-1] == last:
            return v

    return None
