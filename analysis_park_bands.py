"""Hit rate of each park-factor band WITHIN the star context, so we can see
if lowering the park cutoff adds good picks or bad ones."""
from calibration_tracker import _sb_fetch
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=[r for r in _sb_fetch("select=stat_type,direction,line,p_over,pitcher_tier,"
     "park_factor,result,pick_date&resolved=eq.true&result=neq.void")
     if r.get("result") in ("hit","miss")]
for r in rows: r["line"]=f(r,"line"); r["pitcher_tier"]=r.get("pitcher_tier") or ""
# star context minus park: HFS OVER, line 5.5-6.5, soft pitcher, p_over 0.75-0.85
pool=[r for r in rows if r["stat_type"]=="Hitter Fantasy Score" and r["direction"]=="OVER"
      and 5.5<=r["line"]<=6.5 and r["pitcher_tier"] in ("weak","below_avg","average")
      and 0.75<=f(r,"p_over")<=0.85]
days=len({r["pick_date"] for r in pool})
def rate(rs):
    if not rs: return "0/0"
    h=sum(x["result"]=="hit" for x in rs); return f"{h}/{len(rs)} = {h/len(rs):.1%}"
print(f"Star pool (park-agnostic): {rate(pool)}  over {days} days")
print("\nHit rate by park band (the marginal picks each lower cutoff adds):")
for lo,hi,lbl in [(1.0,9,"park >= 1.0 (current)"),(0.98,1.0,"park 0.98-1.0 (added at 0.98)"),
                  (0.95,0.98,"park 0.95-0.98 (added at 0.95)"),(0,0.95,"park < 0.95")]:
    seg=[r for r in pool if lo<=f(r,"park_factor")<hi]
    print(f"  {lbl:<34} {rate(seg)}")
print("\nCumulative tier hit rate + volume by cutoff:")
for cut in (1.0,0.98,0.95,0.90):
    seg=[r for r in pool if f(r,"park_factor")>=cut]
    h=sum(x["result"]=="hit" for x in seg)
    print(f"  park>={cut}: {h}/{len(seg)} = {h/max(len(seg),1):.1%}   {len(seg)/days:.1f}/day")
