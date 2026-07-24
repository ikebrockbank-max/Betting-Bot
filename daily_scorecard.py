"""
Daily scorecard — grades yesterday's logged picks on the corrected
PrizePicks scale and pushes a concise summary via ntfy.

Runs at 8 AM local (14:00 UTC) from daily_scorecard.yml, AFTER all of
yesterday's games are final. Grades live from box scores (via
_fetch_actual_mlb), so it doesn't depend on the resolver having run yet.

  arg (optional) = date YYYY-MM-DD (default: yesterday ET)
"""
import sys
from datetime import datetime, timezone, timedelta

from calibration_tracker import _sb_fetch, _fetch_actual_mlb
from parlay_builder import _passes_direction_gate
from daily_top_picks import _is_elite


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_lock(r):
    return "(Goblin)" in (r.get("stat_type") or "")


def run(date=None):
    if date is None:
        date = (datetime.now(timezone.utc) - timedelta(hours=4)
                - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = _sb_fetch(f"select=player,sport,stat_type,direction,line,confidence,"
                     f"p_over,result,pick_date&pick_date=eq.{date}")
    for r in rows:
        r["line"] = _f(r, "line")

    def grade(r):
        if r.get("sport") == "MLB":
            look = r["stat_type"].replace(" (Goblin)", "")
            a = _fetch_actual_mlb(r["player"], look, date)
            if a in (None, "DNP"):
                return None
            return (a > r["line"]) if r["direction"] == "OVER" else (a < r["line"])
        if r.get("result") in ("hit", "miss"):
            return r["result"] == "hit"
        return None

    tiers = {"STAR": [], "LOCK": [], "gate": [], "all": []}
    star_detail = []
    for r in rows:
        hit = grade(r)
        if hit is None:
            continue
        tiers["all"].append(hit)
        if _is_elite(r):
            tiers["STAR"].append(hit)
            star_detail.append((r["player"].split()[-1], hit))
        if _is_lock(r):
            tiers["LOCK"].append(hit)
        if r.get("sport") == "MLB" and not _is_lock(r) and _passes_direction_gate(r):
            tiers["gate"].append(hit)

    def line(label, b):
        if not b:
            return None
        return f"{label}: {sum(b)}/{len(b)} ({sum(b)/len(b):.0%})"

    parts = [f"📊 Scorecard {date[5:]}"]
    for k, lbl in (("STAR", "⭐ Stars"), ("LOCK", "🔒 Locks"),
                   ("gate", "Gate"), ("all", "All")):
        ln = line(lbl, tiers[k])
        if ln:
            parts.append(ln)
    if star_detail:
        misses = [n for n, h in star_detail if not h]
        hits = [n for n, h in star_detail if h]
        if hits:
            parts.append("✅ " + ", ".join(hits))
        if misses:
            parts.append("❌ " + ", ".join(misses))
    body = "\n".join(parts)
    print(body)

    try:
        from notify import send_push
        send_push(body, title=f"📊 Daily Scorecard {date[5:]}")
        print("\n[pushed via ntfy]")
    except Exception as e:
        print(f"\n[push failed: {e}]")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
