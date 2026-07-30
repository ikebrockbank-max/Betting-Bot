"""Define a per-pick reliability score from the proven signals, then VALIDATE
it: bucket historical HFS OVER picks by score and show actual hit rate.
Labels are only useful if higher score = higher actual rate (monotonic)."""
from calibration_tracker import _sb_fetch
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0

def reliability(p):
    """Points from each proven signal -> label. Returns (score,label)."""
    s=0
    po=f(p,"p_over"); line=f(p,"line"); pt=p.get("pitcher_tier") or ""; pk=f(p,"park_factor")
    # p_over: sweet spot best, 0.85+ trap is the worst single signal
    if 0.75<=po<=0.85: s+=2
    elif po>0.85: s-=2
    elif po>=0.70: s+=0
    else: s-=1
    # park
    if pk>=1.0: s+=2
    elif pk>=0.95: s-=1
    else: s-=1
    # pitcher
    if pt in ("weak","below_avg"): s+=2
    elif pt=="average": s+=1
    else: s-=2   # ace/above_avg/unknown
    # line
    s += 1 if line>=5.5 else -1
    if s>=6: return s,"HIGH"
    if s>=3: return s,"MED"
    if s>=0: return s,"LOW"
    return s,"FADE"

rows=[r for r in _sb_fetch("select=stat_type,direction,line,p_over,pitcher_tier,"
     "park_factor,result,pick_date&resolved=eq.true&result=neq.void")
     if r.get("result") in ("hit","miss")]
hfs=[r for r in rows if r.get("stat_type")=="Hitter Fantasy Score" and r.get("direction")=="OVER"]
for r in hfs: r["line"]=f(r,"line")
days=len({r["pick_date"] for r in hfs})
from collections import defaultdict
buckets=defaultdict(list)
for r in hfs:
    _,lbl=reliability(r); buckets[lbl].append(r)
print(f"Reliability validation on {len(hfs)} HFS OVER picks:")
for lbl in ("HIGH","MED","LOW","FADE"):
    rs=buckets[lbl]
    if rs:
        h=sum(x["result"]=="hit" for x in rs)
        print(f"  {lbl:<5} {h:3d}/{len(rs):<4d} = {h/len(rs):.1%}   {len(rs)/days:.1f}/day")
