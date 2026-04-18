"""
ESPN injury report fetcher.

Uses ESPN's unofficial public API — no key required, updated in near real-time
(sourced from RotoWire). Covers Out, Doubtful, Questionable, and Day-To-Day.
"""

import requests

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json",
}

# Status IDs that should disqualify a pick
DISQUALIFYING_STATUSES = {"out", "doubtful"}
# Status IDs worth flagging as a warning
WARNING_STATUSES = {"questionable", "day-to-day"}


def get_injury_report() -> dict[str, dict]:
    """
    Fetch current NBA injury report from ESPN.

    Returns dict keyed by lowercase player name:
        {
          "status": "Out" | "Doubtful" | "Questionable" | "Day-To-Day",
          "detail": "knee" / "illness" / etc,
          "headline": short news snippet,
          "disqualified": bool,   # Out or Doubtful → skip this pick
          "warning": bool,        # Questionable / DTD → flag but don't skip
        }
    """
    try:
        resp = requests.get(ESPN_URL, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[injuries] ESPN fetch failed: {e}")
        return {}

    data = resp.json()
    report = {}

    for team_entry in data.get("injuries", []):
        for inj in team_entry.get("injuries", []):
            athlete = inj.get("athlete", {})
            name = athlete.get("displayName", "").strip()
            if not name:
                continue

            inj_type = inj.get("type", {})
            status_raw = inj_type.get("description", "").lower()   # e.g. "out", "questionable"
            detail_obj = inj.get("details", {})
            detail = detail_obj.get("type", "")                    # e.g. "Knee", "Illness"

            short = inj.get("shortComment", "") or inj.get("longComment", "")
            # Truncate to a useful one-liner
            headline = short[:120].split(". ")[0] if short else status_raw.title()

            report[name.lower()] = {
                "name": name,
                "status": status_raw.title(),
                "detail": detail,
                "headline": headline,
                "disqualified": status_raw in DISQUALIFYING_STATUSES,
                "warning": status_raw in WARNING_STATUSES,
            }

    return report


def check_player(name: str, report: dict[str, dict]) -> dict | None:
    """
    Look up a player in the injury report by name.
    Returns their injury entry or None if healthy/not listed.
    """
    return report.get(name.lower().strip())
