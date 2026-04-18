"""
PrizePicks bug/mispricing detector.

Finds cases where PrizePicks sets a demon line that is EASIER than (or equal to)
the standard line, or a goblin line that is HARDER than the standard line.

When this happens you get the demon/goblin multiplier for a line that shouldn't
have it — essentially free money. These bugs appear most often on:
  - Future games (lines set days in advance with less care)
  - Niche / low-volume sports (tennis, darts, esports, cricket, table tennis, etc.)
  - Late-night updates where lines get patched incorrectly

Run:
  python3 scanner_bugs.py                     # NBA only
  python3 scanner_bugs.py --sport nhl         # single sport
  python3 scanner_bugs.py --all-sports        # NBA/NHL/MLB/NFL
  python3 scanner_bugs.py --all-leagues       # EVERY league PP offers (recommended)
  python3 scanner_bugs.py --all-leagues --days 7   # scan 7 days ahead for future-game bugs
"""

import argparse
import csv
import json
import time
from collections import defaultdict
from datetime import datetime, UTC, timezone, timedelta
from pathlib import Path

import requests

LINE_SNAPSHOT_PATH = Path("logs/.line_snapshot.json")

BUGS_LOG = Path("logs/pp_bugs.csv")
BUGS_FIELDS = [
    "timestamp", "league", "player", "stat_type", "game_id", "start_time",
    "standard_line", "demon_line", "goblin_line",
    "bug_type",       # "demon_easy" | "goblin_hard" | "demon_eq_standard"
    "gap",            # abs(demon - standard) — bigger = more exploitable
    "payout_note",
]

PP_HEADERS = {
    # Mobile UA bypasses PerimeterX bot protection on the partner API endpoint
    "User-Agent": "PrizePicks/2.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
    "Accept": "application/json",
}
PP_BASE = "https://partner-api.prizepicks.com"

# ── League registry ───────────────────────────────────────────────────────────
# All known PP league IDs. Used for --sport shortcut and display names.
LEAGUE_IDS = {
    # Major sports
    "nba":      7,
    "nhl":      8,
    "nfl":      9,
    "mlb":      2,
    "wnba":     3,
    # Other ball sports
    "soccer":   82,
    "afl":      165,   # Australian Football League
    "kbo":      135,   # Korean Baseball
    "npb":      298,   # Japanese Baseball
    "cricket":  162,
    "handball": 284,
    "pwhl":     273,   # Women's hockey
    # Racket / paddle
    "tennis":   5,
    "darts":    269,
    # Motorsport
    "nascar":   4,
    "f1":       125,
    "indycar":  270,
    # Golf
    "pga":      1,
    # Combat sports
    "mma":      12,
    "boxing":   42,
    # Esports
    "lol":      121,
    "valorant": 159,
    "cs2":      265,
    "cod":      145,
    "dota2":    174,
    "r6":       274,
    "apex":     268,
    "halo":     267,
    "rocketleague": 161,
}

# Human-readable names for league IDs (used in all-leagues scan)
LEAGUE_NAMES = {
    1: "PGA", 2: "MLB", 3: "WNBA", 4: "NASCAR", 5: "TENNIS",
    7: "NBA", 8: "NHL", 9: "NFL", 11: "CFL", 12: "MMA",
    42: "BOXING", 82: "SOCCER", 84: "NBA 1H", 121: "LoL",
    125: "F1", 135: "KBO", 145: "COD", 159: "VALORANT",
    161: "Rocket League", 162: "CRICKET", 165: "AFL",
    174: "Dota2", 192: "NBA 1Q", 227: "NHL 1P", 231: "NHL SERIES",
    237: "NBA PLAYOFFS", 244: "TENNIS LIVE", 250: "NBA SERIES",
    252: "WNBA", 265: "CS2", 267: "HALO", 268: "APEX",
    269: "DARTS", 270: "INDYCAR", 273: "PWHL", 274: "R6",
    282: "NHL PLAYOFFS", 283: "BEACH VB", 284: "HANDBALL",
    285: "BADMINTON", 286: "TABLE TENNIS", 298: "NPB",
    299: "BBL", 300: "GBL", 301: "KBL", 302: "BCL",
    307: "TNC", 345: "NBB",
}

# Default scan window
HOURS_AHEAD_DEFAULT = 72


# ── API fetch (paginated) ─────────────────────────────────────────────────────

def _fetch_all_projections(league_id: int | None = None, hours_ahead: int = HOURS_AHEAD_DEFAULT) -> tuple[list, dict]:
    """
    Fetch projections from PrizePicks.
    - With league_id: fetches that league only.
    - Without league_id (all-leagues mode): the partner API returns ALL projections
      in a single response (30k+), so no pagination is needed.
    Returns (projections, players_dict).
    """
    params: dict = {"single_stat": "true"}
    if league_id is not None:
        params["league_id"] = league_id
        params["per_page"] = 500

    for attempt in range(3):
        resp = requests.get(f"{PP_BASE}/projections", headers=PP_HEADERS, params=params, timeout=30)

        if resp.status_code == 429:
            wait = int(resp.json().get("retry_after", 60))
            print(f"  [warn] Rate limited — waiting {wait}s (attempt {attempt+1}/3)...")
            time.sleep(wait + 2)
            continue

        resp.raise_for_status()
        data = resp.json()
        break
    else:
        raise RuntimeError("Rate limited 3 times — try again later")

    all_players = {
        i["id"]: i["attributes"].get("name", "")
        for i in data.get("included", [])
        if i.get("type") == "new_player"
    }
    return data.get("data", []), all_players


# ── Grouping ─────────────────────────────────────────────────────────────────

def _group_lines(projections: list, players: dict, hours_ahead: int) -> tuple[dict, dict]:
    """
    Group projection lines by (player, stat_type, game_id).

    PP offers multiple demon/goblin thresholds per stat ("ladder").
    We store all of them so _find_bugs can detect any threshold that overlaps the standard.

    Returns:
      groups:    {(player, stat, game_id): {standard, demon[], goblin[], league_id, start_time}}
      game_info: {game_id: start_time_str}
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)

    groups: dict = defaultdict(lambda: {
        "standard": None, "demon": [], "goblin": [],
        "standard_adjusted": False,   # True if PP flagged the standard line with adjusted_odds
        "demon_adjusted": [],         # parallel list: adjusted_odds value per demon line
        "goblin_adjusted": [],        # parallel list: adjusted_odds value per goblin line
        "league_id": None, "start_time": "",
    })
    game_info: dict = {}

    for proj in projections:
        attrs = proj.get("attributes", {})

        if attrs.get("status") != "pre_game":
            continue

        # Skip Fantasy Score / combo projection types
        proj_type = attrs.get("projection_type", "Single Stat")
        if proj_type and proj_type != "Single Stat":
            continue

        # Time window filter
        start_str = attrs.get("start_time", "")
        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if start > cutoff or start < now - timedelta(hours=2):
                continue
        except (ValueError, TypeError):
            continue

        ot = attrs.get("odds_type")
        if ot not in ("standard", "demon", "goblin"):
            continue

        try:
            line = float(attrs["line_score"])
        except (ValueError, KeyError):
            continue

        pid = proj["relationships"]["new_player"]["data"]["id"]
        pname = players.get(pid, "")
        if not pname or "+" in pname:
            continue

        stat = attrs.get("stat_type", "")
        game_id = attrs.get("game_id", "")
        lid = proj["relationships"].get("league", {}).get("data", {}).get("id")

        # Include lid in key — sub-leagues (NBA 1H/1Q, NHL 1P) share game_ids
        # with the parent league but have different thresholds. Without lid in
        # the key they would merge, producing phantom "bugs" from cross-league noise.
        key = (pname, stat, game_id, lid)
        entry = groups[key]

        adj = attrs.get("adjusted_odds")
        if ot == "standard":
            entry["standard"] = line
            # Track whether PP flagged this standard line with a non-baseline multiplier
            if adj is True:
                entry["standard_adjusted"] = True
        else:
            entry[ot].append(line)
            entry[f"{ot}_adjusted"].append(adj)

        if entry["league_id"] is None:
            entry["league_id"] = lid
        if not entry["start_time"]:
            entry["start_time"] = start_str
        game_info[game_id] = start_str

    return dict(groups), game_info


# ── Bug detection ─────────────────────────────────────────────────────────────

def _find_bugs(groups: dict) -> list[dict]:
    """
    Identify mispriced lines across demon/goblin ladders.

    PP intentionally offers a *ladder* of demon/goblin thresholds at varying multipliers.
    True bugs occur when the MINIMUM demon threshold crosses below the standard — meaning
    a "harder" label is applied to an easier-than-standard bet.

    To avoid flagging the legitimate ladder (e.g., Jokic demons at [4.5, 5.5, 6.5, 14.5…]
    where the lower thresholds intentionally price at lower demon multipliers), we apply:
      - demon_eq_standard: demon == standard (always a bug — same line, wrong multiplier)
      - demon_easy (small): min demon is 0.5–2 below standard (likely a mistake, not a ladder)
      - demon_easy (large): min demon is 2+ below standard AND no demon exists above standard
        (the whole demon set is upside down — clear bug)

    Bug types:
      demon_easy:        exploitable — demon line is easier than standard
      demon_eq_standard: exploitable — demon == standard, wrong multiplier
      goblin_hard:       trap — goblin line is harder than standard, avoid
    """
    bugs = []

    for (player, stat, game_id, _lid), entry in groups.items():
        s = entry.get("standard")
        demon_lines = sorted(entry.get("demon", []))
        goblin_lines = sorted(entry.get("goblin", []))
        lid = entry.get("league_id")
        league = LEAGUE_NAMES.get(lid, f"league_{lid}")
        start_time = entry.get("start_time", "")

        base = {
            "player": player, "stat": stat, "game_id": game_id,
            "league": league, "league_id": lid, "start_time": start_time,
            "standard": s, "goblin_ladder": goblin_lines, "demon_ladder": demon_lines,
        }

        is_ladder = len(demon_lines) > 1  # multiple demon thresholds = NBA/NHL ladder

        for d in demon_lines:
            if s is None:
                continue
            # Skip if a goblin at the same threshold handles this line in the app
            if d in goblin_lines:
                continue

            gap = round(s - d, 2)

            if d == s:
                # Always a bug: "demon" label on the SAME threshold as standard.
                # App shows it as a demon pick but it's standard difficulty → free multiplier.
                # Works for all sports regardless of ladder presence.
                bugs.append({**base, "bug_line": d, "bug_type": "demon_eq_standard", "gap": 0.0,
                              "description": f"Demon {d} == Standard {s} — same difficulty, demon payout!"})

            elif d < s:
                # For SIMPLE sports (one demon line): any demon < standard is a real bug.
                # e.g. soccer "2.5 demon, 3.0 standard" with nothing else — app shows it as demon.
                simple_bug = not is_ladder

                # For LADDER sports (NBA/NHL): only flag if gap is tiny (0.5) —
                # classic half-unit mismatch like the goalie saves case.
                # Larger gaps in ladders are shadow/backend entries the app doesn't show.
                ladder_bug = is_ladder and gap <= 0.5

                if simple_bug or ladder_bug:
                    bugs.append({**base, "bug_line": d, "bug_type": "demon_easy", "gap": gap,
                                  "description": f"Demon {d} < Standard {s} (gap={gap}) — easier with demon payout!"})

        for g in goblin_lines:
            if s is not None and g > s:
                if g in demon_lines:
                    continue
                is_goblin_ladder = len(goblin_lines) > 1
                gap = round(g - s, 2)
                # Simple sports: any goblin > standard is a trap
                # Ladder sports: only flag small gaps (0.5)
                if (not is_goblin_ladder) or gap <= 0.5:
                    bugs.append({**base, "bug_line": g, "bug_type": "goblin_hard", "gap": gap,
                                  "description": f"Goblin {g} > Standard {s} (gap={gap}) — harder, avoid!"})

    return bugs


# ── Adjusted-odds detector ───────────────────────────────────────────────────
# PP standard lines normally have adjusted_odds=False (baseline multiplier).
# When a standard line has adjusted_odds=True, PP has manually tweaked its
# multiplier — could be UP (exploit: normal-looking pick, better-than-standard
# payout) or DOWN (trap: easy pick, penalized payout). User must verify in app.
# Most useful when the standard line is ALSO easy (no demon lines above it),
# suggesting PP bumped a lazy line's multiplier upward.

def find_adjusted_standard_lines(groups: dict) -> list[dict]:
    """
    Find standard lines where PP has flagged adjusted_odds=True.
    These have a non-baseline multiplier — could be higher OR lower than standard.
    Returns a list for manual review; direction must be confirmed in the PP app.
    """
    results = []
    for (player, stat, game_id, _lid), entry in groups.items():
        if not entry.get("standard_adjusted"):
            continue
        s = entry.get("standard")
        if s is None:
            continue
        lid = entry.get("league_id")
        league = LEAGUE_NAMES.get(lid, f"league_{lid}")

        # Context: is this an easy pick (no demon line above standard, lots of goblins)?
        demon_lines = sorted(entry.get("demon", []))
        goblin_lines = sorted(entry.get("goblin", []))
        demons_above = [d for d in demon_lines if d > s]
        goblins_below = [g for g in goblin_lines if g < s]

        # Mark as "likely boosted" if no demons above standard exist
        # (PP may have boosted the standard to act as the demon tier)
        likely_boosted = len(demons_above) == 0 and len(goblins_below) > 0

        results.append({
            "player":         player,
            "stat":           stat,
            "league":         league,
            "league_id":      lid,
            "line":           s,
            "game_id":        game_id,
            "start_time":     entry.get("start_time", ""),
            "demon_ladder":   demon_lines,
            "goblin_ladder":  goblin_lines,
            "likely_boosted": likely_boosted,
            "note": (
                "No demons above — standard may be acting as demon tier (likely UP)"
                if likely_boosted else
                "Has demon lines above — standard multiplier adjusted, direction unknown"
            ),
        })

    # Sort: likely-boosted first, then by league+player
    results.sort(key=lambda x: (not x["likely_boosted"], x["league"], x["player"]))
    return results


# ── Multiplier value bug detector ────────────────────────────────────────────
# Every demon/goblin line SHOULD have adjusted_odds=True. If it doesn't, the
# multiplier adjustment wasn't loaded — you have the label without the payout:
#   goblin + no adjustment = easy line paying STANDARD rates  → EXPLOIT
#   demon  + no adjustment = hard line paying STANDARD rates  → TRAP (avoid)

def find_multiplier_value_bugs(groups: dict) -> list[dict]:
    """
    Find demon/goblin lines where adjusted_odds is not True.
    These have the odds_type label but the multiplier value wasn't applied:
      - goblin without adjustment: easier pick at standard payout → exploit
      - demon without adjustment:  harder pick at standard payout → trap/avoid
    """
    results = []
    for (player, stat, game_id, _lid), entry in groups.items():
        lid = entry.get("league_id")
        league = LEAGUE_NAMES.get(lid, f"league_{lid}")
        s = entry.get("standard")
        start_time = entry.get("start_time", "")

        demon_lines    = entry.get("demon", [])
        demon_adjusted = entry.get("demon_adjusted", [])
        goblin_lines   = entry.get("goblin", [])
        goblin_adjusted = entry.get("goblin_adjusted", [])

        # Check each goblin line
        for i, (g_line, g_adj) in enumerate(zip(goblin_lines, goblin_adjusted)):
            if g_adj is not True:
                gap = round(s - g_line, 2) if s is not None else None
                results.append({
                    "player":     player,
                    "stat":       stat,
                    "league":     league,
                    "league_id":  lid,
                    "game_id":    game_id,
                    "start_time": start_time,
                    "bug_type":   "goblin_no_adjustment",
                    "bug_line":   g_line,
                    "standard":   s,
                    "gap":        gap,
                    "adjusted_odds_value": g_adj,
                    "description": (
                        f"Goblin {g_line} has adjusted_odds={g_adj!r} — "
                        f"goblin label but standard multiplier applied! Easy pick at standard payout."
                    ),
                })

        # Check each demon line
        for i, (d_line, d_adj) in enumerate(zip(demon_lines, demon_adjusted)):
            if d_adj is not True:
                gap = round(d_line - s, 2) if s is not None else None
                results.append({
                    "player":     player,
                    "stat":       stat,
                    "league":     league,
                    "league_id":  lid,
                    "game_id":    game_id,
                    "start_time": start_time,
                    "bug_type":   "demon_no_adjustment",
                    "bug_line":   d_line,
                    "standard":   s,
                    "gap":        gap,
                    "adjusted_odds_value": d_adj,
                    "description": (
                        f"Demon {d_line} has adjusted_odds={d_adj!r} — "
                        f"demon label but standard multiplier applied! Hard pick at standard payout."
                    ),
                })

    # Goblins first (exploitable), then demons (traps), sorted by gap
    results.sort(key=lambda x: (
        0 if x["bug_type"] == "goblin_no_adjustment" else 1,
        -(x["gap"] or 0),
    ))
    return results


# ── Flash sale detector ───────────────────────────────────────────────────────

def find_flash_sales(projections: list, players: dict, hours_ahead: int) -> list[dict]:
    """
    Find lines where PP has set a flash_sale_line_score.
    Flash sales temporarily lower the threshold on a standard line — free edge
    for the duration of the sale (usually 15–60 min).

    Returns list of dicts with player, stat, normal_line, sale_line, discount, etc.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)
    sales = []

    for proj in projections:
        attrs = proj.get("attributes", {})
        flash = attrs.get("flash_sale_line_score")
        if flash is None:
            continue
        if attrs.get("status") != "pre_game":
            continue
        if attrs.get("odds_type") != "standard":
            continue
        proj_type = attrs.get("projection_type", "Single Stat")
        if proj_type and proj_type != "Single Stat":
            continue

        start_str = attrs.get("start_time", "")
        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if start > cutoff or start < now - timedelta(hours=2):
                continue
        except (ValueError, TypeError):
            continue

        pid = proj["relationships"]["new_player"]["data"]["id"]
        pname = players.get(pid, "")
        if not pname or "+" in pname:
            continue

        try:
            normal = float(attrs["line_score"])
            sale   = float(flash)
        except (ValueError, KeyError, TypeError):
            continue

        if sale >= normal:
            continue   # sale line isn't actually lower — skip

        lid = proj["relationships"].get("league", {}).get("data", {}).get("id")
        league = LEAGUE_NAMES.get(lid, f"league_{lid}")
        discount = round(normal - sale, 2)

        sales.append({
            "player":      pname,
            "stat":        attrs.get("stat_type", ""),
            "league":      league,
            "league_id":   lid,
            "normal_line": normal,
            "sale_line":   sale,
            "discount":    discount,
            "game_id":     attrs.get("game_id", ""),
            "start_time":  start_str,
        })

    return sorted(sales, key=lambda x: -x["discount"])


# ── Promo detector ────────────────────────────────────────────────────────────

def find_promos(projections: list, players: dict, hours_ahead: int) -> list[dict]:
    """
    Find lines where PP has set is_promo=True.
    Promo lines have boosted payouts — higher multipliers for the same threshold.
    Best used on lines that are also favorable vs sportsbook consensus.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)
    promos = []

    for proj in projections:
        attrs = proj.get("attributes", {})
        if not attrs.get("is_promo"):
            continue
        if attrs.get("status") != "pre_game":
            continue
        proj_type = attrs.get("projection_type", "Single Stat")
        if proj_type and proj_type != "Single Stat":
            continue

        start_str = attrs.get("start_time", "")
        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if start > cutoff or start < now - timedelta(hours=2):
                continue
        except (ValueError, TypeError):
            continue

        pid = proj["relationships"]["new_player"]["data"]["id"]
        pname = players.get(pid, "")
        if not pname or "+" in pname:
            continue

        try:
            line = float(attrs["line_score"])
        except (ValueError, KeyError):
            continue

        lid = proj["relationships"].get("league", {}).get("data", {}).get("id")
        league = LEAGUE_NAMES.get(lid, f"league_{lid}")
        ot = attrs.get("odds_type", "standard")

        promos.append({
            "player":     pname,
            "stat":       attrs.get("stat_type", ""),
            "league":     league,
            "league_id":  lid,
            "line":       line,
            "odds_type":  ot,
            "game_id":    attrs.get("game_id", ""),
            "start_time": start_str,
        })

    return promos


# ── Line movement tracker ─────────────────────────────────────────────────────

def _load_snapshot() -> dict:
    if LINE_SNAPSHOT_PATH.exists():
        try:
            return json.loads(LINE_SNAPSHOT_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_snapshot(snapshot: dict):
    LINE_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LINE_SNAPSHOT_PATH.write_text(json.dumps(snapshot))


def find_line_movement_bugs(groups: dict) -> list[dict]:
    """
    Compare current line groupings against the last saved snapshot.
    Flags cases where the standard line MOVED UP but a demon threshold
    stayed put — creating a new demon_easy gap that didn't exist before.

    Also flags cases where standard moved DOWN past a goblin — creating
    a goblin_hard trap that appeared after line was initially set.

    Returns list of newly-created bug dicts.
    """
    old = _load_snapshot()
    new_snapshot = {}
    movement_bugs = []

    for (player, stat, game_id, lid), entry in groups.items():
        s   = entry.get("standard")
        key = f"{player}|{stat}|{game_id}|{lid}"
        new_snapshot[key] = {
            "standard": s,
            "demon":    entry.get("demon", []),
            "goblin":   entry.get("goblin", []),
        }

        if s is None:
            continue

        prev = old.get(key)
        if not prev or prev.get("standard") is None:
            continue   # no history to compare

        prev_std = prev["standard"]
        if prev_std == s:
            continue   # line didn't move

        move = round(s - prev_std, 2)
        league = LEAGUE_NAMES.get(lid, f"league_{lid}")

        # Standard moved UP — check if any demon is now below the new standard
        # but was already below the old standard (so it wasn't flagged before).
        # New bug: standard rose but PP forgot to lift the demon with it.
        if move > 0:
            for d in sorted(entry.get("demon", [])):
                if d >= s:
                    continue  # demon is still above standard, fine
                if d in entry.get("goblin", []):
                    continue  # goblin covers this slot in app
                gap = round(s - d, 2)
                # Only flag if the gap is NEW (demon was >= old standard before)
                was_already_below = d < prev_std
                if was_already_below:
                    continue  # was already a bug before the move, not new
                movement_bugs.append({
                    "player":      player,
                    "stat":        stat,
                    "league":      league,
                    "league_id":   lid,
                    "game_id":     game_id,
                    "start_time":  entry.get("start_time", ""),
                    "standard":    s,
                    "bug_line":    d,
                    "gap":         gap,
                    "prev_std":    prev_std,
                    "move":        move,
                    "bug_type":    "line_moved_demon_easy",
                    "goblin_ladder": entry.get("goblin", []),
                    "demon_ladder":  entry.get("demon", []),
                    "description": (f"Standard moved {prev_std}→{s} (+{move}) "
                                    f"but demon stayed at {d} — gap={gap}!"),
                })

        # Standard moved DOWN — check if any goblin is now above the new standard
        # and wasn't above the old standard.
        elif move < 0:
            for g in sorted(entry.get("goblin", [])):
                if g <= s:
                    continue
                if g in entry.get("demon", []):
                    continue
                gap = round(g - s, 2)
                was_already_above = g > prev_std
                if was_already_above:
                    continue
                movement_bugs.append({
                    "player":      player,
                    "stat":        stat,
                    "league":      league,
                    "league_id":   lid,
                    "game_id":     game_id,
                    "start_time":  entry.get("start_time", ""),
                    "standard":    s,
                    "bug_line":    g,
                    "gap":         gap,
                    "prev_std":    prev_std,
                    "move":        move,
                    "bug_type":    "line_moved_goblin_hard",
                    "goblin_ladder": entry.get("goblin", []),
                    "demon_ladder":  entry.get("demon", []),
                    "description": (f"Standard dropped {prev_std}→{s} ({move}) "
                                    f"but goblin stayed at {g} — now harder by {gap}!"),
                })

    _save_snapshot(new_snapshot)
    return movement_bugs


# ── Scan functions ─────────────────────────────────────────────────────────────

def scan_bugs(sport: str = "nba", show_all_lines: bool = False, hours_ahead: int = HOURS_AHEAD_DEFAULT) -> list[dict]:
    """Scan a single sport/league for bugs."""
    league_id = LEAGUE_IDS.get(sport.lower())
    if not league_id:
        print(f"[ERROR] Unknown sport '{sport}'. Options: {sorted(LEAGUE_IDS.keys())}")
        return []

    league_name = LEAGUE_NAMES.get(league_id, sport.upper())

    print(f"\n{'='*65}")
    print(f"PRIZEPICKS BUG SCAN — {league_name}")
    print(f"{'='*65}")
    print(f"[pp] Scanning next {hours_ahead}h of {league_name} projections...")

    try:
        projections, players = _fetch_all_projections(league_id, hours_ahead)
        print(f"[pp] {len(projections)} projections fetched, {len(players)} players")
    except Exception as e:
        print(f"[ERROR] PrizePicks fetch failed: {e}")
        return []

    groups, game_info = _group_lines(projections, players, hours_ahead)
    print(f"[pp] {len(groups)} (player, stat, game) combos with line types")

    bugs = _find_bugs(groups)
    actionable = [b for b in bugs if b["bug_type"] in ("demon_easy", "demon_eq_standard")]
    avoid = [b for b in bugs if b["bug_type"] == "goblin_hard"]

    move_bugs     = find_line_movement_bugs(groups)
    flash_sales   = find_flash_sales(projections, players, hours_ahead)
    promos        = find_promos(projections, players, hours_ahead)
    adj_standards = find_adjusted_standard_lines(groups)
    mult_bugs     = find_multiplier_value_bugs(groups)

    _print_bugs(actionable + move_bugs, avoid, league_name, game_info)
    _print_multiplier_value_bugs(mult_bugs)
    _print_flash_sales(flash_sales)
    _print_promos(promos)
    _print_adjusted_standards(adj_standards)
    if show_all_lines:
        _print_all_lines(groups, league_name, actionable + move_bugs)

    _log_bugs(bugs + move_bugs, sport)

    print(f"\n  Log: {BUGS_LOG}")
    print(f"{'='*65}\n")
    return actionable + move_bugs


def scan_all_leagues(show_all_lines: bool = False, hours_ahead: int = HOURS_AHEAD_DEFAULT) -> list[dict]:
    """
    Scan EVERY active PP league in a single paginated sweep.
    Most efficient — only hits the API ~2-4 times total regardless of how many leagues exist.
    """
    print(f"\n{'='*65}")
    print(f"PRIZEPICKS BUG SCAN — ALL LEAGUES")
    print(f"{'='*65}")
    print(f"[pp] Fetching all projections (next {hours_ahead}h)...")

    try:
        projections, players = _fetch_all_projections(league_id=None, hours_ahead=hours_ahead)
        print(f"[pp] {len(projections)} total projections, {len(players)} players")
    except Exception as e:
        print(f"[ERROR] PrizePicks fetch failed: {e}")
        return []

    groups, game_info = _group_lines(projections, players, hours_ahead)
    print(f"[pp] {len(groups)} (player, stat, game) combos across all leagues")

    bugs = _find_bugs(groups)
    actionable = [b for b in bugs if b["bug_type"] in ("demon_easy", "demon_eq_standard")]
    avoid = [b for b in bugs if b["bug_type"] == "goblin_hard"]

    # New detectors
    move_bugs     = find_line_movement_bugs(groups)
    flash_sales   = find_flash_sales(projections, players, hours_ahead)
    promos        = find_promos(projections, players, hours_ahead)
    adj_standards = find_adjusted_standard_lines(groups)
    mult_bugs     = find_multiplier_value_bugs(groups)

    _print_bugs(actionable + move_bugs, avoid, "ALL LEAGUES", game_info)
    _print_multiplier_value_bugs(mult_bugs)
    _print_flash_sales(flash_sales)
    _print_promos(promos)
    _print_adjusted_standards(adj_standards)
    if show_all_lines:
        _print_all_lines(groups, "ALL LEAGUES", actionable + move_bugs)

    _log_bugs(bugs + move_bugs, "all")
    print(f"\n  Log: {BUGS_LOG}")
    print(f"{'='*65}\n")
    return actionable + move_bugs


# ── Output helpers ─────────────────────────────────────────────────────────────

def _print_bugs(actionable: list, avoid: list, label: str, game_info: dict):
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    print(f"  Scan time: {ts}")

    if actionable:
        print(f"\n  🚨 EXPLOITABLE BUGS ({len(actionable)}) — demon easier/equal to standard:")
        for b in sorted(actionable, key=lambda x: (-x["gap"], x["league"], x["player"])):
            start = b.get("start_time", "")[:16]
            gap = b["gap"]
            icon = "★" if gap > 0 else "="
            print(f"\n    {icon} [{b['league']}] {b['player']} — {b['stat']}")
            print(f"      {b['description']}")
            print(f"      start: {start}  game: {b['game_id'][:30]}")
            print(f"      goblin={b['goblin_ladder']}  std={b['standard']}  demon={b['demon_ladder']}")
            if gap > 0:
                print(f"      → BET DEMON OVER {b['bug_line']} — {gap} easier than standard with demon payout!")
            else:
                print(f"      → BET DEMON OVER {b['bug_line']} — same as standard but higher payout!")
    else:
        print(f"\n  ✅ No exploitable demon bugs found.")

    if avoid:
        print(f"\n  ⚠️  GOBLIN TRAPS ({len(avoid)}) — goblin harder than standard, avoid:")
        for b in sorted(avoid, key=lambda x: (-x["gap"], x["league"])):
            print(f"    [{b['league']}] {b['player']} {b['stat']}: goblin={b['bug_line']} > std={b['standard']} (+{b['gap']})")


def _print_flash_sales(sales: list):
    if not sales:
        return
    print(f"\n  ⚡ FLASH SALES ({len(sales)}) — limited-time lower thresholds:")
    for s in sales:
        start = s.get("start_time", "")[:16]
        print(f"    [{s['league']}] {s['player']} {s['stat']}: "
              f"normal={s['normal_line']} → SALE={s['sale_line']} "
              f"(−{s['discount']}) | {start}")
        print(f"      → BET OVER {s['sale_line']} at standard payout — {s['discount']} easier than usual!")


def _print_promos(promos: list):
    if not promos:
        return
    # Group by player for cleaner display
    from collections import defaultdict as dd
    by_player: dict = dd(list)
    for p in promos:
        by_player[p["player"]].append(p)
    print(f"\n  🎯 PROMO LINES ({len(promos)}) — boosted payout multipliers:")
    for player, lines in sorted(by_player.items()):
        for ln in lines:
            start = ln.get("start_time", "")[:16]
            print(f"    [{ln['league']}] {player} {ln['stat']}: "
                  f"{ln['odds_type']} line={ln['line']} (PROMO — boosted payout) | {start}")


def _print_multiplier_value_bugs(bugs: list):
    if not bugs:
        return
    exploits = [b for b in bugs if b["bug_type"] == "goblin_no_adjustment"]
    traps    = [b for b in bugs if b["bug_type"] == "demon_no_adjustment"]
    if exploits:
        print(f"\n  💥 MULTIPLIER VALUE BUG — GOBLIN AT STANDARD PAYOUT ({len(exploits)}):")
        print(f"     Goblin label but multiplier wasn't applied → easy pick pays standard rate!")
        for b in exploits:
            start = b.get("start_time", "")[:16]
            print(f"    [{b['league']}] {b['player']} {b['stat']}: goblin={b['bug_line']} std={b['standard']} gap={b['gap']}")
            print(f"      → BET GOBLIN LESS {b['bug_line']} — goblin difficulty, standard payout! | {start}")
    if traps:
        print(f"\n  ⛔ MULTIPLIER VALUE BUG — DEMON AT STANDARD PAYOUT ({len(traps)}):")
        print(f"     Demon label but multiplier wasn't applied → hard pick pays standard rate. AVOID.")
        for b in traps:
            start = b.get("start_time", "")[:16]
            print(f"    [{b['league']}] {b['player']} {b['stat']}: demon={b['bug_line']} std={b['standard']} | {start}")


def _print_adjusted_standards(lines: list):
    if not lines:
        return
    boosted = [l for l in lines if l["likely_boosted"]]
    unknown = [l for l in lines if not l["likely_boosted"]]
    if boosted:
        print(f"\n  💰 LIKELY BOOSTED STANDARD LINES ({len(boosted)}) — no demon above, multiplier adjusted UP:")
        for l in boosted:
            start = l.get("start_time", "")[:16]
            print(f"    [{l['league']}] {l['player']} {l['stat']}: standard={l['line']} "
                  f"goblins={l['goblin_ladder']} | {start}")
            print(f"      → VERIFY IN APP: standard pick with adjusted (likely higher) multiplier")
    if unknown:
        print(f"\n  ⚙  ADJUSTED STANDARD LINES ({len(unknown)}) — multiplier modified, direction unknown:")
        for l in unknown[:10]:  # cap to avoid spam
            start = l.get("start_time", "")[:16]
            print(f"    [{l['league']}] {l['player']} {l['stat']}: standard={l['line']} "
                  f"demons={l['demon_ladder']} | {start}")


def _print_all_lines(groups: dict, label: str, actionable: list):
    bug_keys = {(b["player"], b["stat"]) for b in actionable}
    print(f"\n  All {label} line sets (goblin / std / demon):")
    for (player, stat, game_id, _lid), entry in sorted(groups.items(), key=lambda x: (x[0][0], x[0][1])):
        g_str = "/".join(str(x) for x in sorted(entry.get("goblin", []))) or "—"
        s_val = entry.get("standard", "—")
        d_str = "/".join(str(x) for x in sorted(entry.get("demon", []))) or "—"
        league = entry.get("league_id")
        league_name = LEAGUE_NAMES.get(league, f"lg{league}")
        flag = " ← BUG" if (player, stat) in bug_keys else ""
        print(f"    [{league_name:<12}] {player:<24} {stat:<20} {g_str:<12} / {s_val:<6} / {d_str}{flag}")


def _log_bugs(bugs: list, sport: str):
    if not bugs:
        return
    ts = datetime.now(UTC).isoformat()
    BUGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    write_header = not BUGS_LOG.exists()
    with open(BUGS_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BUGS_FIELDS)
        if write_header:
            writer.writeheader()
        for b in bugs:
            demon_val  = b["bug_line"] if b["bug_type"] != "goblin_hard" else ""
            goblin_val = b["bug_line"] if b["bug_type"] == "goblin_hard" else ""
            writer.writerow({
                "timestamp":     ts,
                "league":        b.get("league", sport),
                "player":        b["player"],
                "stat_type":     b["stat"],
                "game_id":       b["game_id"],
                "start_time":    b.get("start_time", "")[:16],
                "standard_line": b["standard"],
                "demon_line":    demon_val,
                "goblin_line":   goblin_val,
                "bug_type":      b["bug_type"],
                "gap":           b["gap"],
                "payout_note":   b["description"],
            })


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PrizePicks bug/mispricing scanner")
    parser.add_argument("--sport", default="nba",
                        help=f"Single sport to scan. Options: {sorted(LEAGUE_IDS.keys())}")
    parser.add_argument("--all-sports", action="store_true",
                        help="Scan NBA, NHL, MLB, NFL")
    parser.add_argument("--all-leagues", action="store_true",
                        help="Scan EVERY PP league (recommended — catches all niche sports)")
    parser.add_argument("--all-lines", action="store_true",
                        help="Print every line set, not just bugs")
    parser.add_argument("--days", type=float, default=3.0,
                        help="How many days ahead to scan (default 3, use 7 for future-game bugs)")
    args = parser.parse_args()

    hours = int(args.days * 24)

    if args.all_leagues:
        scan_all_leagues(show_all_lines=args.all_lines, hours_ahead=hours)
    elif args.all_sports:
        for sport in ["nba", "nhl", "mlb", "soccer", "tennis", "mma"]:
            scan_bugs(sport, show_all_lines=args.all_lines, hours_ahead=hours)
            time.sleep(3)
    else:
        scan_bugs(args.sport, show_all_lines=args.all_lines, hours_ahead=hours)
