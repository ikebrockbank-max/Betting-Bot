"""Does the 0.85+ overconfidence penalty still hold AFTER the park+pitcher
filters? And does park>=0.98 vs >=1.0 matter? Tests whether we're too strict."""
from calibration_tracker import _sb_fetch
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=[r for r in _sb_fetch("select=stat_type,direction,line,p_over,pitcher_tier,"
     "park_factor,result,pick_date&resolved=eq.true&result=neq.void")
     if r.get("result") in ("hit","miss")]
for r in rows: r["line"]=f(r,"line"); r["pitcher_tier"]=r.get("pitcher_tier") or ""
# "good context" = HFS OVER, line 5.5-6.5, soft pitcher (any known-soft) — vary park + p_over
def ctx(r, parkmin):
    return (r["stat_type"]=="Hitter Fantasy Score" and r["direction"]=="OVER"
            and 5.5<=r["line"]<=6.5 and r["pitcher_tier"] in ("weak","below_avg","average")
            and f(r,"park_factor")>=parkmin)
def rate(rs):
    if len(rs)<8: return f"n={len(rs)} (thin)"
    h=sum(x["result"]=="hit" for x in rs); return f"{h}/{len(rs)} = {h/len(rs):.1%}"
for parkmin in (1.0, 0.98, 0.95):
    pool=[r for r in rows if ctx(r,parkmin)]
    lo=[r for r in pool if 0.75<=f(r,"p_over")<=0.85]
    hi=[r for r in pool if f(r,"p_over")>0.85]
    print(f"park>={parkmin}: full pool {rate(pool)}  | p_over 0.75-0.85 {rate(lo)}  | p_over 0.85+ {rate(hi)}")
