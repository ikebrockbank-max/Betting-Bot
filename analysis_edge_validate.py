"""Era-split validation of top edge-hunt candidates. A real edge holds across
months; a curve-fit fluke doesn't."""
from collections import defaultdict
from calibration_tracker import _sb_fetch
def f(r,k,d=None):
    v=r.get(k)
    if v in (None,""): return d
    try: return float(v)
    except: return d
rows=[r for r in _sb_fetch("select=sport,stat_type,direction,p_over,pitcher_tier,park_factor,"
     "hit_rate,trend,line,result,pick_date&resolved=eq.true&result=neq.void") if r.get("result") in ("hit","miss")]
def rate(rs):
    if not rs: return "  -  "
    h=sum(x["result"]=="hit" for x in rs); return f"{h}/{len(rs)}={h/len(rs):.0%}"
def era(r):
    d=r.get("pick_date") or ""
    return d[:7]
cands=[
  ("WNBA PRA OVER hot", lambda r: r.get("sport")=="WNBA" and r.get("stat_type")=="Pts+Rebs+Asts" and r.get("direction")=="OVER" and (f(r,"trend") or 0)>0.15),
  ("MLB ERA-allowed UNDER (all parks)", lambda r: r.get("sport")=="MLB" and r.get("stat_type")=="Earned Runs Allowed" and r.get("direction")=="UNDER"),
  ("MLB ERA-allowed UNDER park .95-1.0", lambda r: r.get("sport")=="MLB" and r.get("stat_type")=="Earned Runs Allowed" and r.get("direction")=="UNDER" and 0.95<=(f(r,"park_factor") or 0)<1.0),
  ("MLB Hits UNDER vs below_avg pit", lambda r: r.get("sport")=="MLB" and r.get("stat_type")=="Hits" and r.get("direction")=="UNDER" and r.get("pitcher_tier")=="below_avg"),
  ("MLB Hits UNDER (all)", lambda r: r.get("sport")=="MLB" and r.get("stat_type")=="Hits" and r.get("direction")=="UNDER"),
  ("WNBA Pts+Rebs OVER hot", lambda r: r.get("sport")=="WNBA" and r.get("stat_type")=="Pts+Rebs" and r.get("direction")=="OVER" and (f(r,"trend") or 0)>0.15),
]
for name,pred in cands:
    seg=[r for r in rows if pred(r)]
    bym=defaultdict(list)
    for r in seg: bym[era(r)].append(r)
    months=" | ".join(f"{m}:{rate(bym[m])}" for m in sorted(bym))
    print(f"{name}:  OVERALL {rate(seg)}")
    print(f"    {months}\n")
