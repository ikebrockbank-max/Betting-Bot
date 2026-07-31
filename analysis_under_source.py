"""Test the mirror hypothesis: hitter UNDER picks vs TOUGH pitchers. Can
Total Bases/Hits/Runs/HFS UNDER be dialed to 70% by requiring ace/above_avg
pitcher + high model prob + pitcher park?"""
from calibration_tracker import _sb_fetch
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=[r for r in _sb_fetch("select=sport,stat_type,direction,p_over,p_under,pitcher_tier,"
     "park_factor,hit_rate,line,result,pick_date&resolved=eq.true&result=neq.void")
     if r.get("result") in ("hit","miss")]
rows=[r for r in rows if "(Goblin)" not in (r.get("stat_type") or "")]
days=len({r["pick_date"] for r in rows})
UNDER_STATS={"Total Bases","Hits","Runs","Hitter Fantasy Score","Hits+Runs+RBIs"}
unders=[r for r in rows if r.get("sport")=="MLB" and r.get("direction")=="UNDER"
        and r.get("stat_type") in UNDER_STATS]
def rate(rs):
    if len(rs)<12: return None
    h=sum(x["result"]=="hit" for x in rs); return h,len(rs),h/len(rs)
def show(name,rs):
    r=rate(rs)
    if r: print(f"  {name:<46} {r[0]:3d}/{r[1]:<4d} = {r[2]:.0%}  {r[1]/days:.1f}/day")
    else: print(f"  {name:<46} n<12")
hard=lambda r:(r.get("pitcher_tier") or "") in ("ace","above_avg")
pu=lambda r:f(r,"p_under") if f(r,"p_under")>0 else (1-f(r,"p_over"))
print(f"MLB hitter UNDER pool: {rate(unders)}\n")
print("Dialing UNDER vs hard pitchers:")
show("all hitter UNDER", unders)
show("vs hard pitcher (ace/above_avg)", [r for r in unders if hard(r)])
show("vs hard pitcher + p_under>=0.70", [r for r in unders if hard(r) and pu(r)>=0.70])
show("vs hard pitcher + p_under>=0.75", [r for r in unders if hard(r) and pu(r)>=0.75])
show("vs hard + pitcher park (<1.0)", [r for r in unders if hard(r) and f(r,"park_factor")<1.0])
show("vs hard + park<1.0 + p_under>=0.70", [r for r in unders if hard(r) and f(r,"park_factor")<1.0 and pu(r)>=0.70])
print("\nBy stat (vs hard pitcher):")
for st in UNDER_STATS:
    show(f"{st} UNDER vs hard", [r for r in unders if r.get("stat_type")==st and hard(r)])
