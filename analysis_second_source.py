"""Hunt for a 2nd 70% source across ALL sports/stats. For each stat x
direction with enough data, find the best-filtered sub-slice and its volume."""
from collections import defaultdict
from calibration_tracker import _sb_fetch
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=[r for r in _sb_fetch("select=sport,stat_type,direction,p_over,pitcher_tier,"
     "park_factor,hit_rate,confidence,line,result,pick_date&resolved=eq.true&result=neq.void")
     if r.get("result") in ("hit","miss")]
rows=[r for r in rows if "(Goblin)" not in (r.get("stat_type") or "")]
days=len({r["pick_date"] for r in rows})

# group by sport+stat+dir
groups=defaultdict(list)
for r in rows:
    groups[(r.get("sport"),r.get("stat_type"),r.get("direction"))].append(r)

def rate(rs):
    h=sum(x["result"]=="hit" for x in rs); return h,len(rs),(h/len(rs) if rs else 0)

# candidate tightening dials (best of these = how high can we push it)
def best_slice(rs):
    soft=lambda r:(r.get("pitcher_tier") or "") in ("weak","below_avg","average")
    cands=[
        ("base", lambda r:True),
        ("p_over>=0.80", lambda r:f(r,"p_over")>=0.80),
        ("season_hr>=0.85", lambda r:f(r,"hit_rate")>=0.85),
        ("conf>=0.80", lambda r:f(r,"confidence")>=0.80),
        ("soft pit + p_over>=0.80", lambda r:soft(r) and f(r,"p_over")>=0.80),
        ("soft pit + hr>=0.85", lambda r:soft(r) and f(r,"hit_rate")>=0.85),
        ("soft + p_over>=0.80 + park>=1", lambda r:soft(r) and f(r,"p_over")>=0.80 and f(r,"park_factor")>=1.0),
    ]
    best=None
    for name,pred in cands:
        seg=[r for r in rs if pred(r)]
        h,n,rt=rate(seg)
        if n>=15 and (best is None or rt>best[2]):
            best=(name,h,n,rt)
    return best

print(f"Second-source hunt ({len(rows)} non-goblin picks, {days} days):\n")
results=[]
for (sp,st,d),rs in groups.items():
    if len(rs)<40: continue
    _,_,base=rate(rs)
    bs=best_slice(rs)
    if bs:
        results.append((bs[3],sp,st,d,bs[0],bs[1],bs[2],base,len(rs)))
for rt,sp,st,d,fname,h,n,base,tot in sorted(results,reverse=True):
    flag="  <<< 70%+" if rt>=0.70 else ""
    print(f"{rt:.0%} {h}/{n} [{fname}]  {sp} {st} {d}  (base {base:.0%}, {n/days:.1f}/day){flag}")
