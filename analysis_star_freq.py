"""True stars-per-day. Historical rows with NULL park_factor were wrongly
excluded (score_pick defaults park to 1.0 live), undercounting stars. Recount
treating missing park as neutral 1.0, and split all-history vs park-populated."""
from collections import defaultdict
from calibration_tracker import _sb_fetch
def f(r,k,default=0.0):
    v=r.get(k)
    if v in (None,""): return default
    try: return float(v)
    except: return default
def is_star(r, lenient_park=True):
    park=f(r,"park_factor", 1.0 if lenient_park else 0.0)
    return (r.get("stat_type")=="Hitter Fantasy Score" and r.get("direction")=="OVER"
            and 5.5<=f(r,"line")<=6.5 and 0.75<=f(r,"p_over")<=0.85
            and (r.get("pitcher_tier") or "") in ("weak","below_avg","average")
            and park>=1.0)
rows=_sb_fetch("select=stat_type,direction,line,p_over,pitcher_tier,park_factor,pick_date&resolved=eq.true")
days=defaultdict(int); allday=set()
for r in rows:
    d=r.get("pick_date"); allday.add(d)
    if is_star(r): days[d]+=1
dist=defaultdict(int)
for d in allday: dist[days[d]]+=1
n=len(allday)
print(f"Stars per day ({n} days, park-lenient count):")
for k in sorted(dist):
    print(f"  {k} stars: {dist[k]} days ({dist[k]/n:.0%})")
tot=sum(days.values())
print(f"\naverage: {tot/n:.1f} stars/day")
print(f"days with >=1 star: {sum(1 for d in allday if days[d]>=1)}/{n} = {sum(1 for d in allday if days[d]>=1)/n:.0%}")
# recent 14 days
recent=sorted(allday)[-14:]
rtot=sum(days[d] for d in recent)
print(f"\nlast 14 days: {rtot} stars, {rtot/len(recent):.1f}/day, "
      f"{sum(1 for d in recent if days[d]>=1)}/{len(recent)} days had >=1")
