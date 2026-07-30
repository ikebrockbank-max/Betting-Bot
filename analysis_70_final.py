"""Finalize the >=70% definition: test line>=6.0 with park-factor filters."""
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=[r for r in _sb_fetch("select=line,p_over,pitcher_tier,park_factor,hit_rate,"
     "home_away,stat_type,direction,result,pick_date&resolved=eq.true&result=neq.void")
     if r.get("result") in ("hit","miss")]
for r in rows: r["line"]=f(r,"line"); r["pitcher_tier"]=r.get("pitcher_tier") or ""
stars=[r for r in rows if _is_elite(r)]
days=len({r["pick_date"] for r in stars})
def c(name,pred):
    seg=[r for r in stars if pred(r)]
    if len(seg)<10: print(f"  {name:<50} n={len(seg)} (thin)"); return
    h=sum(x['result']=='hit' for x in seg)
    print(f"  {name:<50} {h:3d}/{len(seg):<4d} = {h/len(seg):.1%}  {len(seg)/days:.1f}/day")
pk=lambda r:f(r,"park_factor")
po=lambda r:f(r,"p_over")
weak=lambda r:r["pitcher_tier"] in ("weak","below_avg","average")
print("Toward >=70% at usable volume:")
c("line>=6.0 (baseline)", lambda r:r["line"]>=6.0)
c("line>=6.0 & park>=1.0 (not pitcher park)", lambda r:r["line"]>=6.0 and pk(r)>=1.0)
c("line>=6.0 & park>=0.98", lambda r:r["line"]>=6.0 and pk(r)>=0.98)
c("line>=5.5 & park>=1.0", lambda r:r["line"]>=5.5 and pk(r)>=1.0)
c("line>=5.5 & park>=1.0 & pitcher weak/below/avg", lambda r:r["line"]>=5.5 and pk(r)>=1.0 and weak(r))
c("line>=6.0 & pitcher weak/below/avg", lambda r:r["line"]>=6.0 and weak(r))
c("park>=1.0 only", lambda r:pk(r)>=1.0)
