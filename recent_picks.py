"""Recent picks by day (last 6 days), tagged by the tier they were sent under,
with result where resolved. Answers 'what were my picks today / last few days'."""
from datetime import datetime, timezone, timedelta
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite, _is_prime


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def _tier(r):
    st = r.get("stat_type") or ""
    if "(Goblin)" in st:    return "🔒 Lock"
    if "(WNBAhot)" in st:   return "🏀 WNBA"
    if "(RunsWatch)" in st: return "🧪 Runs"
    if _is_prime(r):        return "🎯 Prime"
    if _is_elite(r):        return "⭐ Star"
    return None


today = (datetime.now(timezone.utc) - timedelta(hours=4)).date()
since = (today - timedelta(days=5)).strftime("%Y-%m-%d")
rows = _sb_fetch(f"select=player,sport,stat_type,direction,line,p_over,pitcher_tier,"
                 f"park_factor,result,pick_date&pick_date=gte.{since}")
for r in rows:
    r["line"] = _f(r, "line"); r["park_factor"] = _f(r, "park_factor")
    r["pitcher_tier"] = r.get("pitcher_tier") or ""

by_day: dict = {}
for r in rows:
    t = _tier(r)
    if not t:
        continue
    by_day.setdefault(r["pick_date"], []).append((t, r))

for day in sorted(by_day, reverse=True):
    picks = by_day[day]
    res = [1 if r.get("result") == "hit" else 0 for _, r in picks if r.get("result") in ("hit", "miss")]
    score = f" — {sum(res)}/{len(res)} hit" if res else " — (not graded yet)"
    print(f"\n{day} ({len(picks)} sent){score}")
    for t, r in sorted(picks, key=lambda x: x[0]):
        mark = {"hit": "✅", "miss": "❌"}.get(r.get("result"), "·")
        st = (r["stat_type"].replace(" (Goblin)", "").replace(" (WNBAhot)", "")
              .replace(" (RunsWatch)", ""))
        print(f"  {mark} {t}  {r['player']} {r['direction']} {r['line']} {st}")
