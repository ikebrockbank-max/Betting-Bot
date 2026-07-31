"""Is there a star-quality edge in any pick type BESIDES HFS OVER? Scan every
stat_type x direction with the same context filters (soft pitcher, park>=1.0
where relevant) to find a second star source."""
from collections import defaultdict
from calibration_tracker import _sb_fetch
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=[r for r in _sb_fetch("select=stat_type,direction,p_over,pitcher_tier,park_factor,"
     "hit_rate,line,result,pick_date&resolved=eq.true&result=neq.void")
     if r.get("result") in ("hit","miss")]
days=len({r["pick_date"] for r in rows})
# soft pitcher + p_over sweet spot, exclude goblins
def ctx(r):
    return ((r.get("pitcher_tier") or "") in ("weak","below_avg","average")
            and 0.75<=f(r,"p_over")<=0.85 and "(Goblin)" not in (r.get("stat_type") or ""))
cells=defaultdict(list)
for r in rows:
    if ctx(r):
        cells[(r.get("stat_type"),r.get("direction"))].append(r)
print(f"stat x direction with soft pitcher + p_over 0.75-0.85 (min 15):")
out=[]
for (st,d),rs in cells.items():
    if len(rs)>=15:
        h=sum(x["result"]=="hit" for x in rs)
        out.append((h/len(rs),h,len(rs),st,d))
for rt,h,n,st,d in sorted(out,reverse=True):
    print(f"  {rt:.0%}  {h:3d}/{n:<4d}  {st} {d}   ({n/days:.1f}/day)")
