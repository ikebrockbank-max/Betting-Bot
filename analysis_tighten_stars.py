"""Backtest tightened star definitions (corrected grades, full history) +
report pitcher-info coverage per day since the fix."""
from collections import defaultdict
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite

def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0

rows=_sb_fetch("select=player,sport,stat_type,direction,line,p_over,hit_rate,"
               "pitcher_tier,result,pick_date&resolved=eq.true&result=neq.void")
rows=[r for r in rows if r.get("result") in ("hit","miss")]
for r in rows:
    r["line"]=f(r,"line"); r["pitcher_tier"]=r.get("pitcher_tier") or ""
stars=[r for r in rows if _is_elite(r)]
days=len({r["pick_date"] for r in stars})

def rep(name, pred):
    seg=[r for r in stars if pred(r)]
    if len(seg)<12:
        print(f"  {name:<48} n={len(seg)} (too few)"); return
    h=sum(x["result"]=="hit" for x in seg)
    print(f"  {name:<48} {h:3d}/{len(seg):<4d} = {h/len(seg):.1%}   {len(seg)/days:.1f}/day")

known=lambda r: r["pitcher_tier"] not in ("","unknown")
po=lambda r: f(r,"p_over")
print("TIGHTENED STAR BACKTEST (corrected grades, all history):")
rep("current star (line 4.5-6.5, p_over>=0.75)", lambda r: True)
rep("cap trap: p_over 0.75-0.85", lambda r: po(r)<=0.85)
rep("line>=5.5", lambda r: r["line"]>=5.5)
rep("line>=6.0", lambda r: r["line"]>=6.0)
rep("line>=5.5 & p_over<=0.85", lambda r: r["line"]>=5.5 and po(r)<=0.85)
rep("line>=6.0 & p_over 0.80-0.85", lambda r: r["line"]>=6.0 and 0.80<=po(r)<=0.85)
rep("known pitcher only", known)
rep("known pitcher & p_over<=0.85", lambda r: known(r) and po(r)<=0.85)
rep("known pitcher & line>=5.5 & p_over<=0.85", lambda r: known(r) and r["line"]>=5.5 and po(r)<=0.85)

print("\nPITCHER-INFO COVERAGE by day (stars only), since 2026-07-20:")
byday=defaultdict(lambda:[0,0])
for r in stars:
    if (r.get("pick_date") or "")>="2026-07-20":
        byday[r["pick_date"]][0]+= known(r); byday[r["pick_date"]][1]+=1
for d in sorted(byday):
    k,n=byday[d]; print(f"  {d}: {k}/{n} known ({k/max(n,1):.0%})")
