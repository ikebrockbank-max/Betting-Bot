"""Are stars being logged but not counted? Check recent days: how many logged
picks pass _is_elite, and what's blocking the near-misses."""
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=_sb_fetch("select=player,stat_type,direction,line,p_over,pitcher_tier,park_factor,"
               "result,pick_date&pick_date=gte.2026-07-30")
rows=[r for r in rows if r.get("stat_type")=="Hitter Fantasy Score" and r.get("direction")=="OVER"]
from collections import defaultdict
byday=defaultdict(lambda:{"elite":0,"total":0,"near":[]})
for r in rows:
    r["line"]=f(r,"line")
    d=r["pick_date"]; byday[d]["total"]+=1
    if _is_elite(r):
        byday[d]["elite"]+=1
    elif 5.0<=r["line"]<=7.0 and f(r,"p_over")>=0.75:
        # near-miss: why blocked?
        why=[]
        if not(5.5<=r["line"]<=6.5): why.append(f"line{r['line']}")
        if not(0.75<=f(r,"p_over")<=0.85): why.append(f"pover{f(r,'p_over'):.2f}")
        if (r.get("pitcher_tier") or "") not in ("weak","below_avg","average"): why.append(f"pit={r.get('pitcher_tier')}")
        if f(r,"park_factor")<1.0: why.append(f"park{f(r,'park_factor'):.2f}")
        byday[d]["near"].append(f"{r['player']} O{r['line']} p{f(r,'p_over'):.2f} [{','.join(why)}]")
print("HFS OVER logged picks by day (elite count / total, + near-misses):")
for d in sorted(byday):
    b=byday[d]
    print(f"\n{d}: {b['elite']} stars / {b['total']} HFS-OVER logged")
    for nm in b["near"][:6]: print(f"    near: {nm}")
