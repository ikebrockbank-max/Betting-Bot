"""Live-grade yesterday's (8/25) pending locks from box scores, and recompute
the last-7d lock rate INCLUDING them (resolved + live-graded), so the number
isn't blind to unresolved days."""
from datetime import datetime, timezone, timedelta
from calibration_tracker import _sb_fetch, _fetch_actual_mlb


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def base(st):
    for s in (" (Goblin)", " (RunsWatch)", " (WNBAhot)", " (ERAunder)"):
        st = st.replace(s, "")
    return st


since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
rows = _sb_fetch(f"select=player,stat_type,direction,line,result,resolved,pick_date"
                 f"&sport=eq.MLB&pick_date=gte.{since}")
locks = [r for r in rows if "(Goblin)" in (r.get("stat_type") or "")]

print("=== yesterday 8/25 locks (live-graded) ===")
yday = "2026-08-25"
yh = yn = 0
for r in [r for r in locks if r.get("pick_date") == yday]:
    a = _fetch_actual_mlb(r["player"], base(r["stat_type"]), yday)
    if a in (None, "DNP"):
        verdict = f"VOID/pending (actual={a})"
    else:
        hit = (a > _f(r, "line")) if r["direction"] == "OVER" else (a < _f(r, "line"))
        yh += 1 if hit else 0; yn += 1
        verdict = f"actual={a} -> {'HIT' if hit else 'MISS'}"
    print(f"  {r['player'][:20]:20} {r['direction']} {r['line']} {base(r['stat_type'])}: {verdict}")
print(f"  yesterday locks: {yh}/{yn}")

# 7d rate: resolved rows + live grade any pending
rh = rn = 0
for r in locks:
    res = r.get("result")
    if res in ("hit", "miss"):
        rh += 1 if res == "hit" else 0; rn += 1
    elif r.get("pick_date") >= since:
        a = _fetch_actual_mlb(r["player"], base(r["stat_type"]), r["pick_date"])
        if a not in (None, "DNP"):
            hit = (a > _f(r, "line")) if r["direction"] == "OVER" else (a < _f(r, "line"))
            rh += 1 if hit else 0; rn += 1
print(f"\n=== last 7d locks INCLUDING live-graded pending: {rh}/{rn} = {rh/rn:.0%} ===")
