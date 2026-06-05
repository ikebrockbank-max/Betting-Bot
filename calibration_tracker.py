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

def _sb_upsert(row: dict) -> bool:
    """Insert or update a row (conflict on player+stat_type+pick_date)."""
    req = urllib.request.Request(
        f"{_SB_URL}/rest/v1/{_TABLE}",
        data=json.dumps(row).encode(),
        headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
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
    """Fetch rows from pick_log."""
    result = _sb_request("GET", _TABLE, params=params)
    return result if isinstance(result, list) else []


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
        "opp_team":      result.get("opp_team", ""),
        "home_away":     result.get("home_away", ""),
        "game_id":       result.get("game_id", ""),
        "pp_id":         result.get("pp_id", ""),
        "projected_stat":result.get("projected_stat"),
        "resolved":      False,
    }

    if _sb_available():
        _sb_upsert(row)
    else:
        # Local fallback
        entries = _local_load()
        uid = f"{result['player']}|{result['stat_type']}|{today}"
        if not any(f"{e['player']}|{e['stat_type']}|{e['pick_date']}" == uid for e in entries):
            entries.append(row)
            _local_save(entries)


def update_results(target_date: str = None):
    """
    Fetch actual box-score results for all unresolved picks on target_date.
    Marks each pick hit/miss and stores the actual value.

    Run this the day AFTER picks are made (next morning).
    GitHub Actions: add a daily 8am ET run of this function.
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

        if actual is not None:
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
    return resolved_count


def _load_resolved() -> list[dict]:
    """Load all resolved picks — from Supabase or local file."""
    if _sb_available():
        rows = _sb_fetch("select=*&resolved=eq.true&order=pick_date.desc&limit=2000")
        # Normalise column names from DB to internal names
        for r in rows:
            r.setdefault("avg",       r.pop("avg_val", 0))
            r.setdefault("actual",    r.pop("actual_value", None))
            r.setdefault("hit",       r.get("result") == "hit" if r.get("result") else None)
            r.setdefault("date",      r.get("pick_date", ""))
        return rows
    else:
        return [e for e in _local_load()
                if e.get("resolved") and e.get("actual") is not None]


def calibration_report(min_picks: int = 5):
    entries = _load_resolved()
    if not entries:
        print("No resolved picks yet. Run update_results() first.")
        return

    print(f"\n{'='*60}")
    print(f"CALIBRATION REPORT  ({len(entries)} resolved picks)")
    print(f"{'='*60}\n")

    hits = sum(1 for e in entries if e.get("hit"))
    print(f"Overall hit rate: {hits}/{len(entries)} = {hits/len(entries):.1%}\n")

    print("Hit rate by confidence bucket:")
    for lo, hi in [(60,70),(70,75),(75,80),(80,85),(85,90),(90,100)]:
        bucket = [e for e in entries if lo <= (e.get("conf_pct") or 0) < hi]
        if len(bucket) >= min_picks:
            b_hits  = sum(1 for e in bucket if e.get("hit"))
            ideal   = (lo + hi) / 2 / 100
            actual_r = b_hits / len(bucket)
            delta   = actual_r - ideal
            flag    = ("✅" if abs(delta) < 0.05
                       else ("🔴 OVER-CONFIDENT" if delta < 0 else "🟢 UNDER-CONFIDENT"))
            print(f"  {lo}-{hi}%: {b_hits}/{len(bucket)} = {actual_r:.1%}  "
                  f"(model said ~{ideal:.0%}) {flag}")

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


def get_calibration_adjustments(min_n: int = 15) -> dict[str, dict]:
    """Per-bucket calibration so score_pick() can recalibrate confidence live."""
    entries = _load_resolved()
    if not entries:
        return {}
    result = {}
    for lo, hi in [(60,70),(70,75),(75,80),(80,85),(85,90),(90,100)]:
        bucket = [e for e in entries if lo <= (e.get("conf_pct") or 0) < hi]
        if len(bucket) < min_n:
            continue
        hits = sum(1 for e in bucket if e.get("hit"))
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
        for season in [CURRENT_SEASON, PRIOR_SEASON]:
            ev_flat, labels, ev_meta = _fetch_gamelog_raw(aid, season)
            if not ev_flat:
                continue
            for g in _build_game_log(ev_flat, labels, ev_meta, fetch_cols):
                if g.get("date") == target_date:
                    return g["value"]
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
        return None
    except Exception:
        return None


def _fetch_actual_mlb(player: str, stat_type: str, target_date: str):
    try:
        import json as _j, urllib.request as _ur
        from data.mlb_batter_stats import find_player_id, PITCHER_STAT_TYPES
        pid = find_player_id(player)
        if not pid:
            return None
        group = "pitching" if stat_type in PITCHER_STAT_TYPES else "hitting"
        url   = (f"https://statsapi.mlb.com/api/v1/people/{pid}/stats"
                 f"?stats=gameLog&group={group}&season=2026")
        data  = _j.loads(_ur.urlopen(url, timeout=10).read())
        for s in data.get("stats", [{}])[0].get("splits", []):
            if s.get("date") == target_date:
                st = s["stat"]
                if stat_type in ("Strikeouts","Pitcher Strikeouts"): return st.get("strikeOuts")
                if stat_type == "Hits Allowed":          return st.get("hits")
                if stat_type == "Earned Runs Allowed":   return st.get("earnedRuns")
                if stat_type == "Walks Allowed":         return st.get("baseOnBalls")
                if stat_type == "Pitching Outs":         return st.get("outs")
                if stat_type == "Hits":                  return st.get("hits")
                if stat_type == "Singles":               return st.get("singles")
                if stat_type == "Home Runs":             return st.get("homeRuns")
                if stat_type == "Walks":                 return st.get("baseOnBalls")
                if stat_type == "Runs":                  return st.get("runs")
                if stat_type == "RBI":                   return st.get("rbi")
                if stat_type == "Total Bases":           return st.get("totalBases")
                if stat_type == "Hitter Strikeouts":     return st.get("strikeOuts")
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
        adj = get_calibration_adjustments(min_n=10)
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
