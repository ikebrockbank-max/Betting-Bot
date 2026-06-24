"""
calibration_tracker.py — Track every scored pick and its actual result.

Storage: Supabase (persistent across GitHub Actions runs) with local JSON fallback.

  log_pick(result)          — called when a pick is scored
  update_results()          — called next day, fetches box-score results and resolves
  calibration_report()      — prints hit rate by bucket, MAE, bias
  get_calibration_adjustments() — returns per-bucket data for live recalibration
  get_stat_mae()            — returns MAE for a sport/stat for dynamic sigma

Supabase env vars (add to GitHub Actions secrets):
  SUPABASE_URL       = https://gggozciyvjeqjnmufigp.supabase.co
  SUPABASE_ANON_KEY  = eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
"""

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Supabase config ────────────────────────────────────────────────────────────
_SB_URL  = os.getenv("SUPABASE_URL",  "https://gggozciyvjeqjnmufigp.supabase.co")
_SB_KEY  = os.getenv("SUPABASE_ANON_KEY", "")
_TABLE   = "pick_log"

# Local fallback path (used when SUPABASE_ANON_KEY not set, e.g. local dev)
_LOCAL   = Path("logs/calibration_log.json")

# ── Calibration thresholds ─────────────────────────────────────────────────────
# Week 1: react fast with small samples — better to over-correct than learn nothing.
# After ~2 weeks (100+ resolved picks) the model has enough data to be more precise.
# These constants are used everywhere: bucket calibration, stat-type calibration,
# and the blend weight formula.
BOOTSTRAP_MIN_N  = 5    # start adjusting after just 5 resolved picks
STEADY_MIN_N     = 15   # normal threshold once data is sufficient
# Blend formula: how much weight to give historical data vs current model score.
# Ramps from 0% at MIN_N picks to MAX_BLEND at BLEND_FULL_N picks.
# Fast early ramp: reach 50% blend at 20 picks (within first 2-3 days).
BLEND_MAX        = 0.70  # never override model more than 70%
BLEND_FULL_N     = 50    # reach max blend at 50 picks (~1 week of data)


# ── Supabase REST helpers ──────────────────────────────────────────────────────

def _sb_available() -> bool:
    return bool(_SB_KEY)

def _sb_headers(extra: dict = None) -> dict:
    h = {
        "apikey":        _SB_KEY,
        "Authorization": f"Bearer {_SB_KEY}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    if extra:
        h.update(extra)
    return h

def _sb_request(method: str, path: str, body=None, params: str = "") -> list | dict | None:
    """Minimal Supabase REST call using stdlib urllib (no extra deps)."""
    url = f"{_SB_URL}/rest/v1/{path}{('?' + params) if params else ''}"
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(url, data=data, headers=_sb_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        print(f"[calibration] Supabase {method} {path}: HTTP {e.code} — {e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"[calibration] Supabase {method} {path}: {e}")
        return None

def _sb_upsert_table(table: str, row: dict, on_conflict: str = "") -> bool:
    """Generic upsert to any table.

    on_conflict: comma-separated column(s) that form the unique constraint,
    e.g. "sport,stat_type". Required for Supabase to perform merge-on-conflict
    rather than raising a 409.
    """
    qs = f"?on_conflict={on_conflict}" if on_conflict else ""
    req = urllib.request.Request(
        f"{_SB_URL}/rest/v1/{table}{qs}",
        data=json.dumps(row).encode(),
        headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        print(f"[calibration] upsert to {table} failed: {e}")
        return False

def _sb_patch_table(table: str, params: str, updates: dict) -> bool:
    """Generic PATCH to any table."""
    req = urllib.request.Request(
        f"{_SB_URL}/rest/v1/{table}?{params}",
        data=json.dumps(updates).encode(),
        headers=_sb_headers(),
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        print(f"[calibration] patch {table} failed: {e}")
        return False

def _sb_fetch_table(table: str, params: str = "select=*") -> list[dict]:
    """Generic fetch from any table."""
    result = _sb_request("GET", table, params=params)
    return result if isinstance(result, list) else []

def _sb_upsert(row: dict) -> bool:
    """Insert or update a pick_log row.

    Supabase requires on_conflict to be specified in the URL for
    resolution=merge-duplicates to work (otherwise returns 409).
    Unique constraint on pick_log is (pick_date, player, stat_type).
    """
    req = urllib.request.Request(
        f"{_SB_URL}/rest/v1/{_TABLE}?on_conflict=pick_date,player,stat_type",
        data=json.dumps(row).encode(),
        headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        print(f"[calibration] upsert failed: {e}")
        return False

def _sb_patch(pick_date: str, player: str, stat_type: str, updates: dict) -> bool:
    """Update specific columns for a pick."""
    params = (f"pick_date=eq.{pick_date}"
              f"&player=eq.{urllib.parse.quote(player)}"
              f"&stat_type=eq.{urllib.parse.quote(stat_type)}")
    req = urllib.request.Request(
        f"{_SB_URL}/rest/v1/{_TABLE}?{params}",
        data=json.dumps(updates).encode(),
        headers=_sb_headers(),
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        print(f"[calibration] patch failed: {e}")
        return False

def _sb_fetch(params: str = "select=*") -> list[dict]:
    """Fetch ALL rows from pick_log, paginating past the 1000-row server cap."""
    import re
    clean = re.sub(r"&?limit=\d+", "", params).lstrip("&")
    all_rows: list[dict] = []
    offset = 0
    page = 1000
    while True:
        batch = _sb_request("GET", _TABLE, params=f"{clean}&limit={page}&offset={offset}")
        if not isinstance(batch, list):
            break
        all_rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return all_rows


# ── Local JSON fallback ───────────────────────────────────────────────────────

def _local_load() -> list[dict]:
    if _LOCAL.exists():
        try:
            return json.loads(_LOCAL.read_text())
        except Exception:
            pass
    return []

def _local_save(entries: list[dict]):
    _LOCAL.parent.mkdir(parents=True, exist_ok=True)
    _LOCAL.write_text(json.dumps(entries, indent=2))


# Need urllib.parse for URL encoding
import urllib.parse


# ── Public API ─────────────────────────────────────────────────────────────────

def log_pick(result: dict):
    """
    Store a scored pick for later result lookup.
    Called for every qualified pick in the daily run.

    Uses Supabase when SUPABASE_ANON_KEY is set (GitHub Actions).
    Falls back to local JSON file in dev.
    """
    today = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")
    line  = float(result.get("line", 0))
    avg   = float(result.get("avg", 0))

    row = {
        "pick_date":     today,
        "player":        result["player"],
        "sport":         result.get("sport", ""),
        "stat_type":     result["stat_type"],
        "line":          line,
        "direction":     result.get("direction", ""),
        "confidence":    result.get("confidence", 0),
        "conf_pct":      result.get("conf_pct", 0),
        "hit_rate":      result.get("hit_rate", 0),
        "p_over":        result.get("p_over"),
        "p_under":       result.get("p_under"),
        "avg_val":       avg,
        "edge_pct":      round(abs(avg - line) / (line + 1e-9), 4),
        "n_games":       result.get("n_games", 0),
        "opp_team":      result.get("opp_team", "") or "",
        "home_away":     result.get("home_away", "") or "",
        "game_id":       result.get("game_id", ""),
        "pp_id":         result.get("pp_id", ""),
        "projected_stat":result.get("projected_stat"),
        "was_qualified": bool(result.get("was_qualified", False)),
        "resolved":      False,
        # Extended signals for future mining — added 2026-06-15
        "adj_hit_rate":  result.get("adj_hit_rate"),          # Bayesian-shrunk hit rate
        "trend":         result.get("trend"),                  # L3 vs L8 momentum
        "rest_days":     result.get("rest_days"),              # days since last game
        "batting_order": result.get("batting_order"),          # MLB lineup position
        "player_team":   result.get("player_team", ""),        # player's team
        "park_factor":   result.get("park_factor"),            # ballpark run factor
        "pitcher_tier":  result.get("pitcher_tier", ""),       # elite/good/average/weak
        "day_of_week":   datetime.now(timezone.utc).weekday(), # 0=Mon, 6=Sun
    }

    if _sb_available():
        _sb_upsert(row)
    else:
        # Local fallback
        entries = _local_load()
        uid = f"{result['player']}|{result['stat_type']}|{today}"
        if not any(f"{e['player']}|{e['stat_type']}|{e.get('pick_date', e.get('date',''))}" == uid for e in entries):
            entries.append(row)
            _local_save(entries)


def log_parlay(parlay: dict, parlay_num: int, parlay_date: str = None):
    """
    Store a parlay from the Kelly portfolio for P&L tracking.
    Called after build_diverse_parlays() in scanner_power_parlay.run().
    """
    if parlay_date is None:
        parlay_date = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")

    legs_data = []
    for leg in parlay.get("leg_summary", []):
        legs_data.append({
            "player":    leg["player"],
            "stat_type": leg["stat_type"],
            "direction": leg["direction"],
            "line":      leg["line"],
            "p_hit":     leg.get("p_hit", 0),
            "hit_rate":  leg.get("hit_rate", 0),
        })

    row = {
        "parlay_date":      parlay_date,
        "parlay_num":       parlay_num,
        "n_legs":           parlay["n_legs"],
        "payout_multiple":  parlay["payout"],
        "bet_size":         parlay.get("bet_size", 0),
        "win_amount":       parlay.get("win_amount", 0),
        "p_win":            parlay.get("p_win", 0),
        "ev_pct":           parlay.get("ev_pct", 0),
        "legs":             json.dumps(legs_data),
        "resolved":         False,
    }

    if _sb_available():
        _sb_upsert_table("parlay_log", row, on_conflict="parlay_date,parlay_num")


def resolve_parlays(target_date: str) -> list[dict]:
    """
    After individual picks are resolved, check each parlay.
    A parlay hits if ALL legs hit. Returns list of resolved parlay summaries.
    """
    if not _sb_available():
        return []

    parlays = _sb_fetch_table("parlay_log",
                               f"select=*&parlay_date=eq.{target_date}&resolved=eq.false")
    if not parlays:
        return []

    resolved_parlays = []
    for p in parlays:
        raw_legs = p.get("legs")
        legs = json.loads(raw_legs) if isinstance(raw_legs, str) else (raw_legs or [])
        if not legs:
            continue

        all_hit   = True
        all_known = True
        killed_by = None

        for leg in legs:
            player    = leg["player"]
            stat_type = leg["stat_type"]
            # Look up the resolved result from pick_log
            picks = _sb_fetch_table("pick_log",
                f"select=result,actual_value&pick_date=eq.{target_date}"
                f"&player=eq.{urllib.parse.quote(player)}"
                f"&stat_type=eq.{urllib.parse.quote(stat_type)}")
            if not picks or picks[0].get("result") is None:
                all_known = False
                break
            if picks[0]["result"] == "miss":
                all_hit = False
                if not killed_by:
                    actual = picks[0].get("actual_value", "?")
                    killed_by = f"{player} {leg['direction']} {leg['line']} {stat_type} (got {actual})"

        if not all_known:
            continue  # some legs unresolved — skip for now

        result       = "hit" if all_hit else "miss"
        bet          = float(p.get("bet_size") or 0)
        win          = float(p.get("win_amount") or 0)
        actual_profit = round(win - bet, 2) if all_hit else round(-bet, 2)

        _sb_patch_table("parlay_log",
            f"parlay_date=eq.{target_date}&parlay_num=eq.{p['parlay_num']}",
            {"result": result, "resolved": True,
             "actual_profit": actual_profit, "killed_by": killed_by})

        resolved_parlays.append({
            "num":     p["parlay_num"],
            "n_legs":  p["n_legs"],
            "bet":     bet,
            "win":     win,
            "result":  result,
            "profit":  actual_profit,
            "killed_by": killed_by,
            "p_win":   p.get("p_win", 0),
        })

    return resolved_parlays


def update_stat_calibration():
    """
    Recompute per-sport/stat-type accuracy from all resolved picks.
    Writes to stat_calibration table. Called after update_results().
    Used by score_pick() to apply stat-specific confidence corrections.
    """
    if not _sb_available():
        return

    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    # result=neq.void — DNPs/scratches were inflating the denominator here
    # without ever counting as a hit, which silently dragged down real_rate
    # for every stat type (this feeds score_pick()'s per-stat confidence
    # correction directly, so the leak wasn't just cosmetic).
    #
    # Use _sb_fetch (paginated), NOT _sb_fetch_table (single request, capped
    # at Supabase's default 1000-row limit). At current volume (500+ MLB
    # lines/day) a 90-day window is tens of thousands of rows — the capped
    # version was silently stuck recomputing the same ~1000-row slice every
    # day since 2026-06-09, blind to everything since. _sb_fetch is already
    # hardcoded to the pick_log table, which is exactly what's needed here.
    resolved = _sb_fetch(
        f"select=sport,stat_type,confidence,result&resolved=eq.true"
        f"&result=neq.void&pick_date=gte.{cutoff}")
    if not resolved:
        return

    # Group by sport + stat_type
    by_stat: dict[tuple, dict] = {}
    for r in resolved:
        key = (r.get("sport", ""), r.get("stat_type", ""))
        if not all(key):
            continue
        by_stat.setdefault(key, {"hits": 0, "total": 0, "conf_sum": 0.0})
        by_stat[key]["total"] += 1
        by_stat[key]["conf_sum"] += float(r.get("confidence") or 0)
        if r.get("result") == "hit":
            by_stat[key]["hits"] += 1

    for (sport, stat_type), d in by_stat.items():
        if d["total"] < BOOTSTRAP_MIN_N:
            continue
        real_rate   = round(d["hits"] / d["total"], 4)
        avg_conf    = round(d["conf_sum"] / d["total"], 4)
        overconf    = round(avg_conf - real_rate, 4)
        _sb_upsert_table("stat_calibration", {
            "sport":          sport,
            "stat_type":      stat_type,
            "n_picks":        d["total"],
            "n_hits":         d["hits"],
            "real_hit_rate":  real_rate,
            "avg_confidence": avg_conf,
            "overconfidence": overconf,
        }, on_conflict="sport,stat_type")


def get_stat_calibration(sport: str, stat_type: str) -> dict | None:
    """
    Return calibration data for a specific sport/stat_type.
    Used in score_pick() to apply a stat-specific confidence correction.

    Returns: {"real_hit_rate": 0.52, "avg_confidence": 0.74, "overconfidence": 0.22, "n": 34}
    Returns None if insufficient data (< BOOTSTRAP_MIN_N picks).
    """
    if not _sb_available():
        return None
    rows = _sb_fetch_table("stat_calibration",
        f"select=*&sport=eq.{sport}&stat_type=eq.{urllib.parse.quote(stat_type)}")
    if not rows or rows[0].get("n_picks", 0) < BOOTSTRAP_MIN_N:
        return None
    r = rows[0]
    return {
        "real_hit_rate":  r.get("real_hit_rate"),
        "avg_confidence": r.get("avg_confidence"),
        "overconfidence": r.get("overconfidence"),
        "n":              r.get("n_picks"),
    }


def _send_results_notification(target_date: str, pick_results: list[dict],
                                parlay_results: list[dict]):
    """
    Push a results summary via ntfy after the morning resolve job.
    Shows exactly which picks hit/missed, why parlays missed, and P&L.
    """
    try:
        from notify import send_push
    except Exception:
        return

    hits    = sum(1 for p in pick_results if p.get("hit"))
    total   = len(pick_results)
    p_hits  = sum(1 for p in parlay_results if p.get("result") == "hit")
    p_total = len(parlay_results)
    net_pnl = sum(p.get("profit", 0) for p in parlay_results)
    pnl_str = f"+${net_pnl:.2f}" if net_pnl >= 0 else f"-${abs(net_pnl):.2f}"

    # Format date nicely
    try:
        dt   = datetime.strptime(target_date, "%Y-%m-%d")
        date_str = dt.strftime("%b %-d")
    except Exception:
        date_str = target_date

    title = (f"📊 {date_str} Results — "
             f"{p_hits}/{p_total} parlays hit, {pnl_str}")

    lines = []

    # Parlay breakdown
    if parlay_results:
        for p in sorted(parlay_results, key=lambda x: x["num"]):
            icon   = "✅" if p["result"] == "hit" else "❌"
            profit = f"+${p['profit']:.2f}" if p["profit"] >= 0 else f"-${abs(p['profit']):.2f}"
            lines.append(f"{icon} P{p['num']} ({p['n_legs']}-pick ${p['bet']:.0f}): {profit}")
            if p["result"] == "miss" and p.get("killed_by"):
                lines.append(f"   💀 {p['killed_by']}")
        lines.append("")

    # Individual pick summary
    lines.append(f"Picks: {hits}/{total} hit ({int(hits/total*100) if total else 0}%)")
    for p in pick_results:
        if p.get("hit") is None:
            continue
        icon   = "✅" if p["hit"] else "❌"
        actual = f" (got {p.get('actual', '?')})" if not p["hit"] else ""
        lines.append(
            f"  {icon} {p['player']} {p['direction']} {p['line']} {p['stat_type']}{actual}"
        )

    body = "\n".join(lines)
    try:
        send_push(body, title=title)
        print(f"[calibration] Results notification sent: {title}")
    except Exception as e:
        print(f"[calibration] Results push failed: {e}")


def update_results(target_date: str = None):
    """
    Fetch actual box-score results for all unresolved picks on target_date.
    Marks each pick hit/miss, resolves parlays, updates stat calibration,
    and sends a push notification with the full P&L summary.

    Run this the morning AFTER picks are made (9 AM ET via GitHub Actions).
    """
    if target_date is None:
        yesterday = datetime.now(timezone.utc) - timedelta(hours=4) - timedelta(days=1)
        target_date = yesterday.strftime("%Y-%m-%d")

    print(f"Resolving picks for {target_date}...")

    if _sb_available():
        pending = _sb_fetch(
            f"select=*&pick_date=eq.{target_date}&resolved=eq.false"
        )
    else:
        all_entries = _local_load()
        pending     = [e for e in all_entries if e.get("pick_date") == target_date
                       and not e.get("resolved")]

    if not pending:
        print(f"  No unresolved picks for {target_date}.")
        return 0

    print(f"  Found {len(pending)} unresolved picks.")
    resolved_count = 0

    for e in pending:
        sport  = e["sport"]
        player = e["player"]
        stat   = e["stat_type"]

        actual = None
        if sport == "WNBA":
            actual = _fetch_actual_wnba(player, stat, target_date)
        elif sport == "NBA":
            actual = _fetch_actual_nba(player, stat, target_date)
        elif sport == "MLB":
            actual = _fetch_actual_mlb(player, stat, target_date)

        if actual == "DNP":
            # Confirmed scratch/no-play day — resolve as void (matches how
            # PrizePicks itself handles a DNP: refunded, not graded as a
            # loss) instead of leaving it "pending" forever, which silently
            # excluded these from every hit-rate calculation.
            updates = {
                "actual_value": None,
                "result":       "void",
                "resolved":     True,
                "proj_error":   None,
            }
            print(f"  ⬜ VOID (DNP) {player} {e['direction']} {e['line']} {stat}")

            if _sb_available():
                _sb_patch(target_date, player, stat, updates)
                e.update(updates)
            else:
                for entry in all_entries:
                    if (entry.get("player") == player
                            and entry.get("stat_type") == stat
                            and entry.get("pick_date") == target_date):
                        entry.update(updates)
                        break

            resolved_count += 1
        elif actual is not None:
            line      = float(e["line"])
            direction = e["direction"]
            hit       = (actual > line) if direction == "OVER" else (actual < line)
            proj      = e.get("projected_stat")
            proj_err  = round(actual - proj, 3) if proj is not None else None

            updates = {
                "actual_value": actual,
                "result":       "hit" if hit else "miss",
                "resolved":     True,
                "proj_error":   proj_err,
            }

            status = "✅ HIT" if hit else "❌ MISS"
            print(f"  {status} {player} {direction} {line} {stat}: actual={actual}"
                  + (f" (proj={proj}, err={proj_err:+.1f})" if proj_err is not None else ""))

            if _sb_available():
                _sb_patch(target_date, player, stat, updates)
                e.update(updates)   # Mirror into local dict so notification has real results
            else:
                # Update local entry
                for entry in all_entries:
                    if (entry.get("player") == player
                            and entry.get("stat_type") == stat
                            and entry.get("pick_date") == target_date):
                        entry.update(updates)
                        break

            resolved_count += 1
        else:
            print(f"  ⚠️  Could not resolve: {player} {stat} ({sport})")

        time.sleep(0.1)

    if not _sb_available():
        _local_save(all_entries)

    print(f"Resolved {resolved_count}/{len(pending)} picks for {target_date}.")

    # ── Step 2: Resolve parlays (check if all legs hit) ───────────────────────
    parlay_results = []
    if _sb_available() and resolved_count > 0:
        print("Resolving parlays...")
        parlay_results = resolve_parlays(target_date)
        p_hits = sum(1 for p in parlay_results if p.get("result") == "hit")
        net    = sum(p.get("profit", 0) for p in parlay_results)
        pnl_s  = f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}"
        print(f"  Parlays: {p_hits}/{len(parlay_results)} hit  |  Net P&L: {pnl_s}")

    # ── Step 3: Update per-stat calibration table ─────────────────────────────
    if _sb_available():
        try:
            update_stat_calibration()
            print("Stat calibration table updated.")
        except Exception as e:
            print(f"Stat calibration update failed: {e}")

    # ── Step 4: Send results push notification ────────────────────────────────
    # Build pick_results list for the notification
    pick_results_for_notif = []
    for e in pending:
        if e.get("result") == "void":
            continue   # DNP/scratch — exclude from the hit/miss recap entirely
        if e.get("result") is not None or e.get("actual") is not None:
            # Use `is not None` (not `or`) so actual=0 (zero hits, zero walks, etc.)
            # is preserved correctly — `0 or fallback` would silently drop the zero.
            _actual = e.get("actual_value") if e.get("actual_value") is not None else e.get("actual")
            pick_results_for_notif.append({
                "player":    e["player"],
                "direction": e["direction"],
                "line":      e["line"],
                "stat_type": e["stat_type"],
                "hit":       e.get("result") == "hit" if e.get("result") else e.get("hit"),
                "actual":    _actual,
            })

    if resolved_count > 0:
        _send_results_notification(target_date, pick_results_for_notif, parlay_results)

    return resolved_count


_resolved_cache: list[dict] | None = None
_resolved_cache_days: int | None = None

def _load_resolved(days_back: int = 60) -> list[dict]:
    """Load resolved picks — last N days to limit egress. Pass 0 for full history.
    Results are cached for the lifetime of the process to avoid redundant fetches."""
    global _resolved_cache, _resolved_cache_days
    if _resolved_cache is not None and _resolved_cache_days == days_back:
        return _resolved_cache
    # result=neq.void excludes confirmed DNPs/scratches — they're resolved
    # (so they stop retrying) but shouldn't count as a loss in any hit-rate
    # or calibration math, same as PrizePicks voiding a scratched pick.
    if _sb_available():
        if days_back:
            from datetime import date, timedelta
            cutoff = (date.today() - timedelta(days=days_back)).isoformat()
            params = (f"select=*&resolved=eq.true&result=neq.void"
                      f"&pick_date=gte.{cutoff}&order=pick_date.desc")
        else:
            params = "select=*&resolved=eq.true&result=neq.void&order=pick_date.desc"
        rows = _sb_fetch(params)
        # Normalise column names from DB to internal names
        for r in rows:
            r.setdefault("avg",       r.pop("avg_val", 0))
            r.setdefault("actual",    r.pop("actual_value", None))
            r.setdefault("hit",       r.get("result") == "hit" if r.get("result") else None)
            r.setdefault("date",      r.get("pick_date", ""))
        _resolved_cache = rows
        _resolved_cache_days = days_back
        return rows
    else:
        return [e for e in _local_load()
                if e.get("resolved") and e.get("actual") is not None
                and e.get("result") != "void"]


def calibration_report(min_picks: int = 5):
    all_entries = _load_resolved()
    if not all_entries:
        print("No resolved picks yet. Run update_results() first.")
        return

    qualified = [e for e in all_entries if e.get("was_qualified")]
    watched   = [e for e in all_entries if not e.get("was_qualified")]

    print(f"\n{'='*60}")
    print(f"CALIBRATION REPORT")
    print(f"  {len(all_entries)} total resolved  |  "
          f"{len(qualified)} bet  |  {len(watched)} watched")
    print(f"{'='*60}\n")

    # Overall — bet picks only
    entries = all_entries   # use all for calibration curves
    hits = sum(1 for e in entries if e.get("hit"))
    q_hits = sum(1 for e in qualified if e.get("hit"))
    if qualified:
        print(f"Bet picks hit rate:   {q_hits}/{len(qualified)} = {q_hits/len(qualified):.1%}")
    print(f"All picks hit rate:   {hits}/{len(entries)} = {hits/len(entries):.1%}\n")

    print("Is the confidence score real? (model % vs actual hit rate per bucket)")
    print(f"  {'Bucket':<10} {'Model':<8} {'Real':<8} {'Delta':<10} {'N picks':<10} {'N bet'}")
    print(f"  {'-'*58}")
    for lo, hi in [(40,50),(50,60),(60,65),(65,70),(70,75),(75,80),(80,85),(85,100)]:
        # conf_pct may come back from Supabase as string or float — coerce to int
        bucket = [e for e in entries if lo <= int(e.get("conf_pct") or 0) < hi]
        if len(bucket) >= min_picks:
            b_hits  = sum(1 for e in bucket if e.get("hit"))
            ideal   = (lo + hi) / 2 / 100
            actual_r = b_hits / len(bucket)
            delta   = actual_r - ideal
            n_bet   = sum(1 for e in bucket if e.get("was_qualified"))
            flag    = ("✅" if abs(delta) < 0.05
                       else ("🔴 OVER" if delta < 0 else "🟢 UNDER"))
            print(f"  {lo:2d}-{hi:3d}%  {ideal:.0%}      {actual_r:.0%}      "
                  f"{delta:+.0%}  {flag:<8}   {len(bucket):<10} {n_bet}")

    print("\nHit rate by sport:")
    for sport in ["MLB", "NBA", "WNBA"]:
        se = [e for e in entries if e.get("sport") == sport]
        if len(se) >= min_picks:
            s_hits = sum(1 for e in se if e.get("hit"))
            print(f"  {sport}: {s_hits}/{len(se)} = {s_hits/len(se):.1%}")

    # Projection MAE
    proj_entries = [e for e in entries if e.get("proj_error") is not None]
    if proj_entries:
        errors = [e["proj_error"] for e in proj_entries]
        mae    = sum(abs(x) for x in errors) / len(errors)
        rmse   = (sum(x**2 for x in errors) / len(errors)) ** 0.5
        bias   = sum(errors) / len(errors)
        print(f"\nProjection accuracy ({len(proj_entries)} picks with projections):")
        print(f"  MAE:  {mae:.2f}  RMSE: {rmse:.2f}  "
              f"Bias: {bias:+.2f} ({'over' if bias > 0 else 'under'}-projecting)")

    print("\nHit rate by edge size:")
    for lo, hi in [(0.08,0.15),(0.15,0.25),(0.25,0.40),(0.40,1.0)]:
        bucket = [e for e in entries if lo <= (e.get("edge_pct") or 0) < hi]
        if len(bucket) >= min_picks:
            b_hits = sum(1 for e in bucket if e.get("hit"))
            print(f"  {lo:.0%}–{hi:.0%} edge: {b_hits}/{len(bucket)} = {b_hits/len(bucket):.1%}")

    stat_perf: dict[str, dict] = {}
    for e in entries:
        st = e.get("stat_type", "")
        stat_perf.setdefault(st, {"hits":0,"total":0})
        stat_perf[st]["total"] += 1
        if e.get("hit"):
            stat_perf[st]["hits"] += 1
    stat_rates = [(st, d["hits"]/d["total"], d["total"])
                  for st, d in stat_perf.items() if d["total"] >= min_picks]
    stat_rates.sort(key=lambda x: -x[1])
    if stat_rates:
        print("\nBest/worst stat types:")
        for st, rate, n in stat_rates[:3]:
            print(f"  ✅ {st}: {rate:.1%} ({n})")
        for st, rate, n in stat_rates[-3:]:
            print(f"  ❌ {st}: {rate:.1%} ({n})")

    print(f"\n{'='*60}\n")


def get_calibration_adjustments(min_n: int = None) -> dict[str, dict]:
    """
    Per-bucket calibration so score_pick() can recalibrate confidence live.

    Uses BOOTSTRAP_MIN_N (5) in the first week so the model starts learning
    immediately rather than waiting for 15+ resolved picks per bucket.
    """
    if min_n is None:
        min_n = BOOTSTRAP_MIN_N
    entries = _load_resolved()
    if not entries:
        return {}
    result = {}
    for lo, hi in [(60,70),(70,75),(75,80),(80,85),(85,90),(90,100)]:
        bucket = [e for e in entries if lo <= int(e.get("conf_pct") or 0) < hi]
        if len(bucket) < min_n:
            continue
        hits      = sum(1 for e in bucket if e.get("hit"))
        hist_rate = hits / len(bucket)
        result[f"{lo}-{hi}"] = {
            "hist_rate": round(hist_rate, 4),
            "n":         len(bucket),
            "delta":     round(hist_rate - (lo+hi)/2/100, 4),
        }
    return result


def get_stat_mae(sport: str = None, stat_type: str = None, min_n: int = 8) -> float | None:
    entries = [
        e for e in _load_resolved()
        if e.get("proj_error") is not None
        and (sport     is None or e.get("sport",     "") == sport)
        and (stat_type is None or e.get("stat_type", "") == stat_type)
    ]
    if len(entries) < min_n:
        return None
    return round(sum(abs(e["proj_error"]) for e in entries) / len(entries), 3)


def get_all_stat_maes(sport: str = None, min_n: int = 8) -> dict[str, float]:
    entries = [
        e for e in _load_resolved()
        if e.get("proj_error") is not None
        and (sport is None or e.get("sport", "") == sport)
    ]
    by_stat: dict[str, list] = {}
    for e in entries:
        st = e.get("stat_type", "")
        if st:
            by_stat.setdefault(st, []).append(abs(e["proj_error"]))
    return {st: round(sum(v)/len(v), 3) for st, v in by_stat.items() if len(v) >= min_n}


# ── Result fetchers (one per sport) ───────────────────────────────────────────

def _fetch_actual_wnba(player: str, stat_type: str, target_date: str):
    try:
        from data.wnba_stats import (get_player_stats, _load_player_ids,
                                      _find_athlete_id, _fetch_gamelog_raw,
                                      _build_game_log, CURRENT_SEASON,
                                      PRIOR_SEASON, STAT_COL, COMBINED_STAT_COLS)
        col  = STAT_COL.get(stat_type)
        cols = COMBINED_STAT_COLS.get(stat_type)
        if not col and not cols:
            return None
        fetch_cols = cols if cols else [col]
        players    = _load_player_ids()
        aid        = _find_athlete_id(player, players)
        if not aid:
            return None
        found_any_games = False
        for season in [CURRENT_SEASON, PRIOR_SEASON]:
            ev_flat, labels, ev_meta = _fetch_gamelog_raw(aid, season)
            if not ev_flat:
                continue
            game_log = _build_game_log(ev_flat, labels, ev_meta, fetch_cols)
            if game_log:
                found_any_games = True
            for g in game_log:
                if g.get("date") == target_date:
                    return g["value"]
        # Same DNP logic as MLB: a season log exists but no entry for this
        # date means a confirmed scratch/no-play day, not a transient
        # fetch failure — resolve as void instead of retrying forever.
        if found_any_games:
            return "DNP"
        return None
    except Exception:
        return None


def _fetch_actual_nba(player: str, stat_type: str, target_date: str):
    try:
        import scanner_power_parlay as s
        pid   = s._get_nba_player_id(player)
        games = s._nba_game_log(pid) if pid else []
        def val(g):
            st = stat_type
            if st == "Points":        return g["pts"]
            if st == "Rebounds":      return g["reb"]
            if st == "Assists":       return g["ast"]
            if st == "3-Pointers Made": return g["fg3m"]
            if st == "Steals":        return g["stl"]
            if st == "Blocks":        return g["blk"]
            if st == "Turnovers":     return g["tov"]
            if st == "Pts+Rebs+Asts": return g["pts"]+g["reb"]+g["ast"]
            if st == "Pts+Rebs":      return g["pts"]+g["reb"]
            if st == "Pts+Asts":      return g["pts"]+g["ast"]
            return None
        for g in games:
            if g.get("date", "")[:10] == target_date:
                return val(g)
        # Same DNP logic as MLB/WNBA: a game log exists but no entry for
        # this date means a confirmed scratch/no-play day — resolve as
        # void instead of retrying forever.
        if games:
            return "DNP"
        return None
    except Exception:
        return None


def _fetch_actual_mlb(player: str, stat_type: str, target_date: str):
    """
    Returns the actual stat value, the sentinel "DNP" if the player has a
    season game log but no entry for target_date (confirmed scratch/DNP —
    resolve as void, don't keep retrying), or None if we genuinely can't
    tell yet (player lookup failed, API error, etc).
    """
    try:
        import json as _j, urllib.request as _ur
        from data.mlb_batter_stats import find_player_id, PITCHER_STAT_TYPES
        pid = find_player_id(player)
        if not pid:
            return None

        # Fetch both hitting and pitching when needed for composite stats
        _PITCHER_STATS = {"Strikeouts", "Pitcher Strikeouts", "Hits Allowed",
                          "Earned Runs Allowed", "Walks Allowed", "Pitching Outs",
                          "Pitcher Fantasy Score", "Pitches Thrown"}
        group = "pitching" if stat_type in _PITCHER_STATS else "hitting"

        url  = (f"https://statsapi.mlb.com/api/v1/people/{pid}/stats"
                f"?stats=gameLog&group={group}&season=2026")
        data   = _j.loads(_ur.urlopen(url, timeout=10).read())
        splits = data.get("stats", [{}])[0].get("splits", [])

        date_found = False
        for s in splits:
            if s.get("date") == target_date:
                date_found = True
                st = s["stat"]

                # ── Pitcher stats ──────────────────────────────────────────────
                if stat_type in ("Strikeouts", "Pitcher Strikeouts"):
                    return st.get("strikeOuts")
                if stat_type == "Hits Allowed":
                    return st.get("hits")
                if stat_type == "Earned Runs Allowed":
                    return st.get("earnedRuns")
                if stat_type == "Walks Allowed":
                    return st.get("baseOnBalls")
                if stat_type == "Pitching Outs":
                    return st.get("outs")
                if stat_type == "Pitches Thrown":
                    return st.get("numberOfPitches")

                # ── Pitcher Fantasy Score (PrizePicks official formula) ────────
                # Outs×0.75 + K×2 + W×4 - ER×2 - H×0.6 - BB×0.6 - HBP×0.6
                if stat_type == "Pitcher Fantasy Score":
                    outs = st.get("outs") or 0
                    ks   = st.get("strikeOuts") or 0
                    wins = st.get("wins") or 0
                    er   = st.get("earnedRuns") or 0
                    h    = st.get("hits") or 0
                    bb   = st.get("baseOnBalls") or 0
                    hbp  = st.get("hitBatsmen") or 0
                    score = (outs * 0.75 + ks * 2.0 + wins * 4.0
                             - er * 2.0 - h * 0.6 - bb * 0.6 - hbp * 0.6)
                    return round(score, 2)

                # ── Hitter stats ───────────────────────────────────────────────
                if stat_type == "Hits":
                    return st.get("hits")
                if stat_type == "Home Runs":
                    return st.get("homeRuns")
                if stat_type == "Walks":
                    return st.get("baseOnBalls")
                if stat_type == "Runs":
                    return st.get("runs")
                if stat_type == "RBI":
                    return st.get("rbi")
                if stat_type == "Hits+Runs+RBIs":
                    return (st.get("hits") or 0) + (st.get("runs") or 0) + (st.get("rbi") or 0)
                if stat_type == "Total Bases":
                    return st.get("totalBases")
                if stat_type == "Hitter Strikeouts":
                    return st.get("strikeOuts")
                if stat_type == "Stolen Bases":
                    return st.get("stolenBases")

                # Singles = Hits - Doubles - Triples - HRs  (API has no singles field)
                if stat_type == "Singles":
                    h  = st.get("hits") or 0
                    d  = st.get("doubles") or 0
                    t  = st.get("triples") or 0
                    hr = st.get("homeRuns") or 0
                    return max(0, h - d - t - hr)

                # ── Hitter Fantasy Score (PrizePicks official formula) ─────────
                # 1B×3 + 2B×6 + 3B×9 + HR×12 + RBI×3.5 + R×3.5
                # + BB×3 + HBP×3 + SB×6 - K×1
                if stat_type == "Hitter Fantasy Score":
                    h   = st.get("hits") or 0
                    d   = st.get("doubles") or 0
                    t   = st.get("triples") or 0
                    hr  = st.get("homeRuns") or 0
                    singles = max(0, h - d - t - hr)
                    rbi = st.get("rbi") or 0
                    r   = st.get("runs") or 0
                    bb  = st.get("baseOnBalls") or 0
                    hbp = st.get("hitByPitch") or 0
                    sb  = st.get("stolenBases") or 0
                    ks  = st.get("strikeOuts") or 0
                    score = (singles * 3.0 + d * 6.0 + t * 9.0 + hr * 12.0
                             + rbi * 3.5 + r * 3.5
                             + bb * 3.0 + hbp * 3.0 + sb * 6.0
                             - ks * 1.0)
                    return round(score, 2)

        # Only conclude DNP when no split matched target_date at all. If a
        # split DID match but fell through here, that means stat_type isn't
        # handled above (a real code gap, e.g. a composite stat) — NOT a
        # no-play day. Treating that as DNP was a bug: it voided Zack
        # Gelof's Hits+Runs+RBIs pick on 6/23 even though he demonstrably
        # played that day (his Runs prop resolved fine, same date, same
        # game log). Return None so it stays pending and gets surfaced as
        # "could not resolve" instead of silently voided.
        if date_found:
            return None
        if splits:
            return "DNP"
        return None
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "update":
        update_results(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "report":
        calibration_report()
    elif cmd == "calibration":
        adj = get_calibration_adjustments()
        if adj:
            for bucket, d in adj.items():
                delta_str = f"+{d['delta']:.1%}" if d['delta'] > 0 else f"{d['delta']:.1%}"
                flag = ("🟢 under-confident" if d['delta'] > 0.05
                        else ("🔴 over-confident" if d['delta'] < -0.05 else "✅ calibrated"))
                print(f"  {bucket}%: actual {d['hist_rate']:.1%}  (n={d['n']}, {delta_str}) {flag}")
        else:
            print("Not enough data yet.")
        for st, mae in sorted(get_all_stat_maes().items(), key=lambda x: -x[1]):
            print(f"  MAE {st}: ±{mae:.2f}")
    elif cmd == "status":
        if _sb_available():
            rows = _sb_fetch("select=count&resolved=eq.true")
            print(f"Supabase connected. Resolved picks: {rows}")
        else:
            print("Supabase key not set — using local file.")
            print(f"Local picks: {len(_local_load())}")
    else:
        print("Usage:")
        print("  python3 calibration_tracker.py update [YYYY-MM-DD]")
        print("  python3 calibration_tracker.py report")
        print("  python3 calibration_tracker.py calibration")
        print("  python3 calibration_tracker.py status")
