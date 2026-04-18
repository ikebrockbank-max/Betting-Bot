"""
data/underdog.py — Underdog Fantasy API client.

Fetches over/under lines from the public Underdog Fantasy endpoint and groups
them by (player_name, stat, sport_id) for downstream bug detection.
"""

import time
from datetime import datetime, timezone

import requests

UNDERDOG_URL = "https://api.underdogfantasy.com/beta/v5/over_under_lines"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT = 15
_MAX_RETRIES = 3
_RATE_LIMIT_WAIT = 60


def _fetch_raw() -> dict:
    """Fetch the raw JSON from Underdog. Retries up to 3x on 429."""
    for attempt in range(_MAX_RETRIES):
        resp = requests.get(UNDERDOG_URL, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code == 429:
            print(f"  [ud] Rate limited — waiting {_RATE_LIMIT_WAIT}s (attempt {attempt + 1}/{_MAX_RETRIES})...")
            time.sleep(_RATE_LIMIT_WAIT)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Underdog API: rate limited 3 times — try again later")


def get_grouped_lines(sport_filter: str | None = None) -> tuple[dict, list]:
    """
    Fetch Underdog over/under lines and group them by (player_name, stat, sport_id).

    Parameters
    ----------
    sport_filter : str, optional
        Case-insensitive sport_id to restrict results (e.g. "NBA").

    Returns
    -------
    grouped : dict
        Keys are (player_name, stat, sport_id).
        Values are:
          {
            "balanced":   float | None,          # standard line value
            "alternates": [(value, higher_mult), ...],  # sorted ascending
            "expires_at": str | None,             # from balanced line if set
            "sport":      str,                    # sport_id
          }
    raw_lines : list
        The raw over_under_lines list from the API response.
    """
    data = _fetch_raw()

    # Build player lookup: id → {name, sport}
    # Sport is parsed from the player image URL path (e.g. /player-images/nba/...)
    players: dict[str, dict] = {}
    for p in data.get("players", []):
        pid = p.get("id", "")
        first = p.get("first_name") or ""
        last  = p.get("last_name")  or ""
        name  = f"{first} {last}".strip()
        # Extract sport from image URL: .../player-images/<sport>/...
        img   = p.get("image_url") or p.get("dark_image_url") or ""
        sport = ""
        if "/player-images/" in img:
            try:
                sport = img.split("/player-images/")[1].split("/")[0].upper()
            except IndexError:
                pass
        players[pid] = {"name": name, "sport": sport}

    # Build appearance lookup: appearance_id → {name, sport}
    appearances: dict[str, dict] = {}
    for a in data.get("appearances", []):
        aid = a.get("id", "")
        pid = a.get("player_id", "")
        appearances[aid] = players.get(pid, {"name": "", "sport": ""})

    raw_lines: list = data.get("over_under_lines", [])

    # Filter to active lines only (API returns "active" for live lines)
    visible = [ln for ln in raw_lines if ln.get("status") == "active"]

    # Optionally filter by sport
    if sport_filter:
        sf_lower = sport_filter.lower()
        filtered = []
        for ln in visible:
            ou = ln.get("over_under", {})
            app_stat = ou.get("appearance_stat", {})
            aid = app_stat.get("appearance_id", "")
            sport = appearances.get(aid, {}).get("sport_id", "")
            if sport.lower() == sf_lower:
                filtered.append(ln)
        visible = filtered

    # Group: (name, stat, sport) → accumulated data
    grouped: dict = {}

    for ln in visible:
        line_type = ln.get("line_type", "")  # "balanced" or "alternate"
        if line_type not in ("balanced", "alternate"):
            continue

        try:
            stat_value = float(ln.get("stat_value", 0))
        except (TypeError, ValueError):
            continue

        expires_at = ln.get("expires_at")  # str or None

        ou = ln.get("over_under", {})
        app_stat = ou.get("appearance_stat", {})
        aid = app_stat.get("appearance_id", "")
        display_stat = app_stat.get("display_stat", "")

        app_info = appearances.get(aid, {})
        name = app_info.get("name", "")
        sport_id = app_info.get("sport", "")

        if not name or not display_stat or not sport_id:
            continue

        key = (name, display_stat, sport_id)
        if key not in grouped:
            grouped[key] = {
                "balanced": None,
                "alternates": [],
                "expires_at": None,
                "sport": sport_id,
            }

        entry = grouped[key]

        if line_type == "balanced":
            entry["balanced"] = stat_value
            if expires_at:
                entry["expires_at"] = expires_at

        elif line_type == "alternate":
            # Find the payout_multiplier for the "higher" option
            higher_mult: float | None = None
            for opt in ln.get("options", []):
                if opt.get("choice") == "higher":
                    try:
                        higher_mult = float(opt.get("payout_multiplier", 0))
                    except (TypeError, ValueError):
                        pass
                    break
            if higher_mult is not None:
                entry["alternates"].append((stat_value, higher_mult))

    # Sort alternates ascending by value
    for entry in grouped.values():
        entry["alternates"].sort(key=lambda x: x[0])

    return grouped, raw_lines
