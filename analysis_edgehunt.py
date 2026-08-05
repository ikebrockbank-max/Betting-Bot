"""Comprehensive edge hunt. Enumerate stat x direction x single-feature
filters across all sports; report every cell with n>=10 and rate>=70%,
ranked by Wilson 95% lower bound (penalizes small n so flukes sink)."""
import math
from collections import defaultdict
from calibration_tracker import _sb_fetch
def f(r,k,d=None):
    v=r.get(k)
    if v in (None,""): return d
    try: return float(v)
    except: return d
def wilson_lb(h,n,z=1.96):
    if n==0: return 0
    p=h/n
    return (p+z*z/(2*n)-z*math.sqrt((p*(1-p)+z*z/(4*n))/n))/(1+z*z/n)

rows=[r for r in _sb_fetch("select=sport,stat_type,direction,p_over,p_under,pitcher_tier,"
     "park_factor,hit_rate,confidence,line,home_away,trend,batting_order,day_of_week,"
     "n_games,edge_pct,result,pick_date&resolved=eq.true&result=neq.void") if r.get("result") in ("hit","miss")]
rows=[r for r in rows if "(Goblin)" not in (r.get("stat_type") or "") and "(RunsWatch)" not in (r.get("stat_type") or "")]
days=len({r["pick_date"] for r in rows})

def bucket_features(r):
    out={}
    po=f(r,"p_over"); 
    if po is not None: out["p_over"]= "0.85+" if po>=0.85 else "0.80-0.85" if po>=0.80 else "0.75-0.80" if po>=0.75 else "0.70-0.75" if po>=0.70 else "<0.70"
    pk=f(r,"park_factor")
    if pk is not None: out["park"]= "1.05+" if pk>=1.05 else "1.0-1.05" if pk>=1.0 else "0.95-1.0" if pk>=0.95 else "<0.95"
    pt=r.get("pitcher_tier")
    if pt: out["pitcher"]=pt
    hr=f(r,"hit_rate")
    if hr is not None: out["season_hr"]= "0.9+" if hr>=0.9 else "0.8-0.9" if hr>=0.8 else "0.7-0.8" if hr>=0.7 else "0.6-0.7" if hr>=0.6 else "<0.6"
    ln=f(r,"line")
    if ln is not None: out["line"]= "0-1" if ln<=1 else "1.5-3" if ln<=3 else "3.5-5" if ln<=5 else "5.5-7" if ln<=7 else "7.5+"
    ha=r.get("home_away")
    if ha and ha!="unknown": out["home_away"]=ha
    tr=f(r,"trend")
    if tr is not None: out["trend"]= "hot" if tr>0.15 else "cold" if tr<-0.15 else "flat"
    bo=r.get("batting_order")
    if bo: out["bat_order"]= "1-3" if bo<=3 else "4-6" if bo<=6 else "7-9"
    dw=r.get("day_of_week")
    if dw is not None: out["dow"]=str(dw)
    ng=f(r,"n_games")
    if ng is not None: out["n_games"]= "5-8" if ng<8 else "8-11" if ng<11 else "11+"
    eg=f(r,"edge_pct")
    if eg is not None: out["edge"]= ">1.0" if eg>1.0 else "0.6-1.0" if eg>0.6 else "0.35-0.6" if eg>0.35 else "0.2-0.35" if eg>0.2 else "<0.2"
    return out

cells=defaultdict(lambda:[0,0])
for r in rows:
    key0=(r.get("sport"),r.get("stat_type"),r.get("direction"))
    for feat,val in bucket_features(r).items():
        k=(key0,feat,val)
        cells[k][0]+=r["result"]=="hit"; cells[k][1]+=1

found=[]
for (key0,feat,val),(h,n) in cells.items():
    if n>=10 and h/n>=0.70:
        found.append((wilson_lb(h,n),h,n,h/n,key0,feat,val))
found.sort(reverse=True)
print(f"EDGE HUNT — {len(rows)} picks, {days} days. Cells with n>=10 & rate>=70%,")
print(f"ranked by Wilson 95% lower bound (higher = more trustworthy):\n")
print(f"{'wLB':>5} {'rate':>6} {'h/n':>8}  sport|stat|dir | feature=value")
for wlb,h,n,rt,key0,feat,val in found[:45]:
    sp,st,d=key0
    print(f"{wlb:>5.0%} {rt:>6.0%} {h:>3d}/{n:<4d}  {sp}|{st}|{d} | {feat}={val}")
