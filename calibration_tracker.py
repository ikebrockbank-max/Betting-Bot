"""
calibration_tracker.py — Track every scored pick and its actual result.

Two jobs:
  1. log_pick(result)    — called when a pick is scored, stores projection info
  2. update_results()    — called next day, fetches actual box score results
  3. calibration_report() — prints hit rate by confidence bucket, MAE, RMSE, bias

Storage: logs/calibration_log.json
  [{date, player, sport, stat_type, line, direction, confidence, projected_stat,
    projected_minutes, avg, edge_pct, actual, hit, error}, ...]
"""

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

LOG_PATH = Path("logs/calibration_log.json")


# ── Storage ───────────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    if LOG_PATH.exists():
        try:
            return json.loads(LOG_PATH.read_text())
        except Exception:
            pass
    return []

def _save(entries: list[dict]):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(entries, indent=2))


# ── Log a pick ────────────────────────────────────────────────────────────────

def log_pick(result: dict):
    """
    Store a scored pick for later result lookup.
    Call this for every pick that gets sent in the daily notification.
    """
    today = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")
    line  = float(result.get("line", 0))
    avg   = float(result.get("avg", 0))
    proj  = result.get("projected_stat")

    entry = {
        "id":                 f"{result['player']}|{result['stat_type']}|{today}",
        "date":               today,
        "player":             result["player"],
        "sport":              result.get("sport", ""),
        "stat_type":          result["stat_type"],
        "line":               line,
        "direction":          result.get("direction", ""),
        "confidence":         result.get("confidence", 0),
        "conf_pct":           result.get("conf_pct", 0),
        "projected_stat":     proj,
        "projected_minutes":  result.get("projected_minutes"),
        "avg":                avg,
        "edge_pct":           round(abs(avg - line) / (line + 1e-9), 4),
        "hit_rate":           result.get("hit_rate", 0),
        "n_games":            result.get("n_games", 0),
        "opp_team":           result.get("opp_team", ""),
        "home_away":          result.get("home_away", ""),
        "actual":             None,   # filled in by update_results()
        "hit":                None,
        "error":              None,   # projected_stat - actual
        "proj_error":         None,
        "logged_at":          datetime.now(timezone.utc).isoformat(),
        "resolved":           False,
    }

    entries = _load()
    # Avoid duplicate logging for same player+stat+date
    existing_ids = {e["id"] for e in entries}
    if entry["id"] not in existing_ids:
        entries.append(entry)
        _save(entries)


# ── Resolve results ───────────────────────────────────────────────────────────

def _fetch_actual_wnba(player: str, stat_type: str, target_date: str) -> float | None:
    """Fetch actual WNBA stat for player on target_date via ESPN gamelog."""
    try:
        from data.wnba_stats import get_player_stats, _load_player_ids, _find_athlete_id
        from data.wnba_stats import _fetch_gamelog_raw, _build_game_log, CURRENT_SEASON, PRIOR_SEASON
        from data.wnba_stats import STAT_COL, COMBINED_STAT_COLS

        col  = STAT_COL.get(stat_type)
        cols = COMBINED_STAT_COLS.get(stat_type)
        if not col and not cols:
            return None
        fetch_cols = cols if cols else [col]

        players    = _load_player_ids()
        athlete_id = _find_athlete_id(player, players)
        if not athlete_id:
            return None

        for season in [CURRENT_SEASON, PRIOR_SEASON]:
            events_flat, labels, events_meta = _fetch_gamelog_raw(athlete_id, season)
            if not events_flat:
                continue
            game_log = _build_game_log(events_flat, labels, events_meta, fetch_cols)
            for g in game_log:
                if g.get("date") == target_date:
                    return g["value"]
        return None
    except Exception:
        return None


def _fetch_actual_nba(player: str, stat_type: str, target_date: str) -> float | None:
    """Fetch actual NBA stat for player on target_date via NBA game logs."""
    try:
        import scanner_power_parlay as s
        pid = s._get_nba_player_id(player)
        if not pid:
            return None
        games = s._nba_game_log(pid)

        def val(g):
            st = stat_type
            if st == "Points":              return g["pts"]
            if st == "Rebounds":            return g["reb"]
            if st == "Assists":             return g["ast"]
            if st == "3-Pointers Made":     return g["fg3m"]
            if st == "Steals":              return g["stl"]
            if st == "Blocks":              return g["blk"]
            if st == "Turnovers":           return g["tov"]
            if st == "Pts+Rebs+Asts":       return g["pts"] + g["reb"] + g["ast"]
            if st == "Pts+Rebs":            return g["pts"] + g["reb"]
            if st == "Pts+Asts":            return g["pts"] + g["ast"]
            return None

        for g in games:
            date_part = g.get("date", "")[:10]
            if date_part == target_date:
                return val(g)
        return None
    except Exception:
        return None


def _fetch_actual_mlb(player: str, stat_type: str, target_date: str) -> float | None:
    """Fetch actual MLB stat for player on target_date via MLB Stats API."""
    try:
        import scanner_power_parlay as s
        pid = s._get_mlb_pitcher_id(player)
        if not pid:
            return None

        import urllib.request, json as _json
        url  = f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=hitting&season=2026"
        data = _json.loads(urllib.request.urlopen(url, timeout=10).read())
        splits = data.get("stats", [{}])[0].get("splits", [])

        _PITCHER_STAT_TYPES = {
            "Pitcher Strikeouts", "Strikeouts", "Pitcher Fantasy Score",
            "Pitching Outs", "Earned Runs Allowed", "Hits Allowed",
            "Walks Allowed", "Pitches Thrown",
        }
        if stat_type in _PITCHER_STAT_TYPES:
            url  = f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=pitching&season=2026"
            data = _json.loads(urllib.request.urlopen(url, timeout=10).read())
            splits = data.get("stats", [{}])[0].get("splits", [])

        for s_entry in splits:
            if s_entry.get("date") == target_date:
                st = s_entry["stat"]
                if stat_type in ("Strikeouts", "Pitcher Strikeouts"): return st.get("strikeOuts")
                if stat_type == "Hits Allowed":    return st.get("hits")
                if stat_type == "Earned Runs Allowed": return st.get("earnedRuns")
                if stat_type == "Walks Allowed":   return st.get("baseOnBalls")
                if stat_type == "Hits":            return st.get("hits")
                if stat_type == "Singles":         return st.get("singles")
                if stat_type == "Home Runs":       return st.get("homeRuns")
                if stat_type == "Walks":           return st.get("baseOnBalls")
                if stat_type == "Hitter Strikeouts": return st.get("strikeOuts")
                if stat_type == "RBIs":            return st.get("rbi")
                if stat_type == "Runs":            return st.get("runs")
        return None
    except Exception:
        return None


def update_results(target_date: str = None):
    """
    Fetch actual results for all unresolved picks on target_date.
    Default: yesterday (ET).
    Prints a summary of what was resolved.
    """
    if target_date is None:
        yesterday = datetime.now(timezone.utc) - timedelta(hours=4) - timedelta(days=1)
        target_date = yesterday.strftime("%Y-%m-%d")

    entries = _load()
    pending = [e for e in entries if e["date"] == target_date and not e["resolved"]]
    print(f"Resolving {len(pending)} picks for {target_date}...")

    resolved = 0
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
            line      = e["line"]
            direction = e["direction"]
            hit       = (actual > line) if direction == "OVER" else (actual < line)
            proj      = e.get("projected_stat")

            e["actual"]     = actual
            e["hit"]        = hit
            e["error"]      = round(actual - line, 3)          # positive = over the line
            e["proj_error"] = round(actual - proj, 3) if proj is not None else None
            e["resolved"]   = True
            resolved += 1
            status = "✅ HIT" if hit else "❌ MISS"
            print(f"  {status} {player} {direction} {line} {stat}: actual={actual}"
                  + (f" (proj={proj}, err={e['proj_error']:+.1f})" if proj is not None else ""))
        else:
            print(f"  ⚠️  Could not resolve: {player} {stat} ({sport})")

        time.sleep(0.1)

    _save(entries)
    print(f"Resolved {resolved}/{len(pending)} picks.")
    return resolved


# ── Calibration report ────────────────────────────────────────────────────────

def calibration_report(min_picks: int = 5):
    """
    Print full calibration analysis.
    """
    entries = [e for e in _load() if e.get("resolved") and e.get("actual") is not None]
    if not entries:
        print("No resolved picks yet. Run update_results() first.")
        return

    print(f"\n{'='*60}")
    print(f"CALIBRATION REPORT  ({len(entries)} resolved picks)")
    print(f"{'='*60}\n")

    # Overall hit rate
    hits = sum(1 for e in entries if e["hit"])
    print(f"Overall hit rate: {hits}/{len(entries)} = {hits/len(entries):.1%}\n")

    # By confidence bucket
    print("Hit rate by confidence bucket:")
    buckets = [(60,70), (70,75), (75,80), (80,85), (85,90), (90,100)]
    for lo, hi in buckets:
        bucket = [e for e in entries if lo <= e["conf_pct"] < hi]
        if len(bucket) >= min_picks:
            b_hits = sum(1 for e in bucket if e["hit"])
            ideal  = (lo + hi) / 2 / 100
            actual_rate = b_hits / len(bucket)
            delta  = actual_rate - ideal
            flag   = "✅" if abs(delta) < 0.05 else ("🔴 OVER-CONFIDENT" if delta < 0 else "🟢 UNDER-CONFIDENT")
            print(f"  {lo}-{hi}%: {b_hits}/{len(bucket)} = {actual_rate:.1%}  "
                  f"(model said ~{ideal:.0%}) {flag}")

    # By sport
    print("\nHit rate by sport:")
    for sport in ["MLB", "NBA", "WNBA"]:
        sport_entries = [e for e in entries if e["sport"] == sport]
        if len(sport_entries) >= min_picks:
            s_hits = sum(1 for e in sport_entries if e["hit"])
            print(f"  {sport}: {s_hits}/{len(sport_entries)} = {s_hits/len(sport_entries):.1%}")

    # Projection accuracy (where projected_stat exists)
    proj_entries = [e for e in entries if e.get("proj_error") is not None]
    if proj_entries:
        import statistics
        errors = [e["proj_error"] for e in proj_entries]
        mae  = sum(abs(x) for x in errors) / len(errors)
        rmse = (sum(x**2 for x in errors) / len(errors)) ** 0.5
        bias = sum(errors) / len(errors)
        print(f"\nProjection accuracy ({len(proj_entries)} picks with projections):")
        print(f"  MAE:  {mae:.2f}  (avg absolute error)")
        print(f"  RMSE: {rmse:.2f}  (penalizes large misses)")
        print(f"  Bias: {bias:+.2f}  ({'over-projecting' if bias > 0 else 'under-projecting'})")

    # Edge calibration
    print("\nHit rate by edge size:")
    edge_buckets = [(0.08, 0.15), (0.15, 0.25), (0.25, 0.40), (0.40, 1.0)]
    for lo, hi in edge_buckets:
        bucket = [e for e in entries if lo <= e.get("edge_pct", 0) < hi]
        if len(bucket) >= min_picks:
            b_hits = sum(1 for e in bucket if e["hit"])
            print(f"  {lo:.0%}–{hi:.0%} edge: {b_hits}/{len(bucket)} = {b_hits/len(bucket):.1%}")

    # By stat type (top/bottom 3)
    stat_perf = {}
    for e in entries:
        st = e["stat_type"]
        if st not in stat_perf:
            stat_perf[st] = {"hits": 0, "total": 0}
        stat_perf[st]["total"] += 1
        if e["hit"]:
            stat_perf[st]["hits"] += 1
    stat_rates = [(st, d["hits"]/d["total"], d["total"])
                  for st, d in stat_perf.items() if d["total"] >= min_picks]
    stat_rates.sort(key=lambda x: -x[1])
    if stat_rates:
        print("\nBest stat types:")
        for st, rate, n in stat_rates[:3]:
            print(f"  {st}: {rate:.1%} ({n} picks)")
        print("Worst stat types:")
        for st, rate, n in stat_rates[-3:]:
            print(f"  {st}: {rate:.1%} ({n} picks)")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "update":
        date_arg = sys.argv[2] if len(sys.argv) > 2 else None
        update_results(date_arg)
    elif len(sys.argv) > 1 and sys.argv[1] == "report":
        calibration_report()
    else:
        print("Usage:")
        print("  python3 calibration_tracker.py update [YYYY-MM-DD]")
        print("  python3 calibration_tracker.py report")
