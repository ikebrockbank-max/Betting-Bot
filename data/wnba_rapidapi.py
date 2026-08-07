"""
WNBA data via RapidAPI (wnba-api.p.rapidapi.com) — the working source after
ESPN began IP-blocking GitHub Actions runners (403) and stats.wnba.com started
timing out from datacenter IPs. RapidAPI's servers fetch ESPN for us, so the
runner just needs the key (RAPIDAPI_KEY secret).

Confirmed endpoints (2026-08-07):
  wnbaschedule?year=&month=&day=   -> games w/ competitors[].isHome + team ids
  wnbateamplayers?teamid=<id>      -> team.athletes[] (fullName + espn id)
  player-gamelog?playerId=<id>     -> seasonTypes[].categories[].events[].stats
                                       aligned to labels [MIN,PTS,REB,AST,...]

Free tier: 100 requests/day, throttles per-second. We resolve the cheap
home/away gate FIRST (1 schedule + a few rosters) and only spend the pricier
per-player game-log calls on players who are HOME tonight — keeping a full
scan around ~25-30 calls/day.
"""
import os
import json
import time
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

_HOST = "wnba-api.p.rapidapi.com"


def _key() -> str:
    return os.getenv("RAPIDAPI_KEY", "")


def available() -> bool:
    return bool(_key())


# PTS/REB/AST live at these indices in every gamelog event's `stats` array,
# per the confirmed `labels` = [MIN, PTS, REB, AST, STL, BLK, TO, ...].
_IDX = {"MIN": 0, "PTS": 1, "REB": 2, "AST": 3}

_mem: dict = {}           # in-process cache (one scan reuses schedule/rosters)
_last_call = [0.0]


def _throttle():
    dt = time.time() - _last_call[0]
    if dt < 1.6:
        time.sleep(1.6 - dt)
    _last_call[0] = time.time()


def _get(path: str, retries: int = 4):
    """GET a RapidAPI path with per-second throttle + 429 backoff. Memoised
    for the life of the process so repeated lookups in one scan are free."""
    if path in _mem:
        return _mem[path]
    hdrs = {"x-rapidapi-host": _HOST, "x-rapidapi-key": _key()}
    for a in range(retries):
        _throttle()
        try:
            req = urllib.request.Request(f"https://{_HOST}/{path}", headers=hdrs)
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read())
                _mem[path] = d
                return d
        except urllib.error.HTTPError as e:
            if e.code == 429:          # throttled — back off and retry
                time.sleep(3 + 2 * a)
                continue
            _mem[path] = None
            return None
        except Exception:
            time.sleep(2)
    _mem[path] = None
    return None


def _norm(name: str) -> str:
    """Normalise a player name for matching: strip accents, punctuation, lower."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum() or c == " ").strip()


def _today_et_parts():
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    return et.strftime("%Y"), et.strftime("%m"), et.strftime("%d")


def _season_year() -> str:
    """Current WNBA season year (ET). player-gamelog defaults to an OLD season
    (2024) unless &year is passed — omitting it silently served 12-month-stale
    stats, which is what made the tier compute 'hot' on last-year's numbers."""
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y")


def build_player_index(date_parts=None) -> dict:
    """Map every player on a team playing TODAY to their home/away + id.

    Returns { normalized_name: {player_id, team_abbr, opp_abbr, home_away} }.
    Cheap: 1 schedule call + one roster call per team playing (~6-12).
    """
    y, m, d = date_parts or _today_et_parts()
    ck = f"_idx_{y}{m}{d}"
    if ck in _mem:
        return _mem[ck]

    idx: dict = {}
    sched = _get(f"wnbaschedule?year={y}&month={m}&day={d}")
    if not sched:
        _mem[ck] = idx
        return idx
    games = sched.get(f"{y}{m}{d}", [])
    for g in games:
        comps = g.get("competitors", [])
        if len(comps) < 2:
            continue
        for i, team in enumerate(comps):
            opp = comps[1 - i]
            tid = team.get("id")
            if not tid:
                continue
            ha = "home" if team.get("isHome") else "away"
            roster = _get(f"wnbateamplayers?teamid={tid}") or {}
            for ath in (roster.get("team", {}) or {}).get("athletes", []) or []:
                nm = ath.get("fullName") or ath.get("displayName")
                pid = ath.get("id")
                if nm and pid:
                    idx[_norm(nm)] = {
                        "player_id": str(pid),
                        "team_abbr": team.get("abbrev", ""),
                        "opp_abbr":  opp.get("abbrev", ""),
                        "home_away": ha,
                    }
    _mem[ck] = idx
    return idx


def resolve(player_name: str, date_parts=None) -> dict | None:
    """Look up a PP player in today's index (exact norm, then last-name fallback)."""
    idx = build_player_index(date_parts)
    n = _norm(player_name)
    if n in idx:
        return idx[n]
    # last-name fallback (unique last names only, to avoid mismatches)
    last = n.split()[-1] if n else ""
    hits = [v for k, v in idx.items() if k.split()[-1] == last] if last else []
    return hits[0] if len(hits) == 1 else None


def recent_combo_values(player_id: str, stat_type: str, n: int = 10) -> list[float]:
    """Most-recent-first list of the combo stat for this player.

    stat_type: 'Pts+Rebs+Asts' or 'Pts+Rebs'. Events are gathered across all
    seasonTypes and sorted by numeric ESPN gameId (increases over time) so the
    newest games come first — no season-boundary parsing needed.
    """
    # &year is REQUIRED — without it the endpoint returns the 2024 season.
    d = _get(f"player-gamelog?playerId={player_id}&year={_season_year()}")
    if not d:
        return []
    gl = d.get("player_gamelog", {})
    seen: dict[int, list] = {}
    for stype in gl.get("seasonTypes", []) or []:
        for cat in stype.get("categories", []) or []:
            for ev in cat.get("events", []) or []:
                eid = ev.get("eventId") or ev.get("id")
                stats = ev.get("stats")
                if not eid or not stats:
                    continue
                try:
                    seen[int(eid)] = stats
                except (TypeError, ValueError):
                    continue
    if not seen:
        return []

    out, _mins = _series_from_events(seen, stat_type, n)
    return out


def _combo(stats, stat_type) -> float | None:
    try:
        pts = float(stats[_IDX["PTS"]])
        reb = float(stats[_IDX["REB"]])
        ast = float(stats[_IDX["AST"]])
    except (IndexError, ValueError, TypeError):
        return None
    if stat_type == "Pts+Rebs+Asts":
        return pts + reb + ast
    if stat_type == "Pts+Rebs":
        return pts + reb
    return None


def _series_from_events(seen: dict, stat_type: str, n: int):
    """Return (combo_values, minutes) most-recent-first, aligned per game."""
    vals, mins = [], []
    for eid in sorted(seen, reverse=True):          # newest first (eventId ↑ over time)
        stats = seen[eid]
        v = _combo(stats, stat_type)
        if v is None:
            continue
        try:
            mn = float(stats[_IDX["MIN"]])
        except (IndexError, ValueError, TypeError):
            mn = 0.0
        vals.append(v)
        mins.append(mn)
        if len(vals) >= n:
            break
    return vals, mins


def recent_combo_series(player_id: str, stat_type: str, n: int = 12):
    """(combo_values, minutes) most-recent-first — the inputs the minutes/
    efficiency projection needs to judge a hot player's true OVER direction."""
    d = _get(f"player-gamelog?playerId={player_id}&year={_season_year()}")
    if not d:
        return [], []
    gl = d.get("player_gamelog", {})
    seen: dict[int, list] = {}
    for stype in gl.get("seasonTypes", []) or []:
        for cat in stype.get("categories", []) or []:
            for ev in cat.get("events", []) or []:
                eid = ev.get("eventId") or ev.get("id")
                stats = ev.get("stats")
                if not eid or not stats:
                    continue
                try:
                    seen[int(eid)] = stats
                except (TypeError, ValueError):
                    continue
    return _series_from_events(seen, stat_type, n)


def project(values: list, minutes: list) -> float | None:
    """Faithful reconstruction of data/wnba_stats.py's projected_stat: a
    minutes-and-efficiency projection that captures a hot player's rising
    trajectory (recent minutes × recent per-minute rate). Values/minutes are
    most-recent-first and aligned. Omits the original's injury-minute boost and
    usage-noise discount (refinements), so treat it as a close approximation."""
    if len(values) < 3 or len(minutes) < 3:
        return None
    n = len(values)
    season_avg = sum(values) / n
    season_min = sum(minutes) / n
    if season_min <= 0:
        return None
    n3, n5 = min(3, n), min(5, n)
    l3_min = sum(minutes[:n3]) / n3
    l5_min = sum(minutes[:n5]) / n5
    l5_avg = sum(values[:n5]) / n5
    projected_minutes = min(38.0, l3_min * 0.50 + l5_min * 0.30 + season_min * 0.20)
    stat_per_min = season_avg / season_min
    l5_per_min = (l5_avg / l5_min) if l5_min > 0 else stat_per_min
    blended_rate = l5_per_min * 0.60 + stat_per_min * 0.40
    return round(blended_rate * projected_minutes, 2)


def injured_names() -> set:
    """Best-effort set of normalised names on the WNBA injury report
    (out/questionable/doubtful). Empty set if the endpoint is unavailable —
    the caller then proceeds without the screen rather than blocking."""
    out = set()
    d = _get("wnbainjuries") or _get("injuries")
    if not d:
        return out
    try:
        blob = json.dumps(d).lower()
        # cheap presence check keeps this resilient to shape changes
        _ = blob  # noqa
        items = d if isinstance(d, list) else d.get("injuries", d.get("data", []))
        for it in items or []:
            nm = (it.get("player", {}) or {}).get("displayName") if isinstance(it.get("player"), dict) else it.get("player")
            status = (it.get("status") or "").lower()
            if nm and any(s in status for s in ("out", "doubtful", "question")):
                out.add(_norm(nm))
    except Exception:
        pass
    return out
