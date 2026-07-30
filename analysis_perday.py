"""How many stars per day does the strict tier produce? And do Locks cover
the 0-star days?"""
from collections import defaultdict
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=[r for r in _sb_fetch("select=stat_type,direction,line,p_over,pitcher_tier,"
     "park_factor,result,pick_date&resolved=eq.true&result=neq.void")
     if r.get("result") in ("hit","miss")]
for r in rows: r["line"]=f(r,"line")
star_days=defaultdict(int); lock_days=defaultdict(int); all_days=set()
for r in rows:
    d=r.get("pick_date"); all_days.add(d)
    if _is_elite(r): star_days[d]+=1
    if "(Goblin)" in (r.get("stat_type") or ""): lock_days[d]+=1
days=sorted(all_days)
dist=defaultdict(int)
for d in days: dist[star_days[d]]+=1
print(f"Stars per day across {len(days)} days with any resolved pick:")
for k in sorted(dist):
    print(f"  {k} stars: {dist[k]} days ({dist[k]/len(days):.0%})")
zero_star=[d for d in days if star_days[d]==0]
zero_both=[d for d in zero_star if lock_days[d]==0]
print(f"\n0-star days: {len(zero_star)}/{len(days)} ({len(zero_star)/len(days):.0%})")
print(f"  of those, ALSO 0 locks (truly nothing): {len(zero_both)}")
print(f"avg stars/day: {sum(star_days.values())/len(days):.1f}  avg locks/day: {sum(lock_days.values())/len(days):.1f}")
