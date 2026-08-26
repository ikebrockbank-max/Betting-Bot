"""Yesterday's (and last few days') LOCK picks with result + resolved status,
to reconcile 'locks 84% last 7d' vs 'we went 1/3 on locks yesterday'. If
yesterday's rows are result=None/resolved=False, they're just not graded yet
(resolver lag) and the 7d number excludes them."""
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from calibration_tracker import _sb_fetch

since = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%Y-%m-%d")
rows = _sb_fetch(f"select=player,stat_type,direction,line,result,resolved,pick_date"
                 f"&pick_date=gte.{since}")
locks = [r for r in rows if "(Goblin)" in (r.get("stat_type") or "")]

byday = defaultdict(list)
for r in locks:
    byday[r.get("pick_date")].append(r)

for d in sorted(byday):
    day = byday[d]
    graded = [r for r in day if r.get("result") in ("hit", "miss")]
    h = sum(1 for r in graded if r["result"] == "hit")
    pend = sum(1 for r in day if not r.get("resolved") or r.get("result") not in ("hit", "miss", "void"))
    print(f"\n{d}: {len(day)} locks | graded {h}/{len(graded)} | "
          f"{pend} pending/ungraded")
    for r in day:
        st = (r.get("stat_type") or "").replace(" (Goblin)", "")
        print(f"   {r['result'] or 'PENDING':7} {r['player'][:20]:20} "
              f"{r['direction']} {r['line']} {st}  (resolved={r.get('resolved')})")
