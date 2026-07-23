"""Is p_over miscalibrated vs realized in the elite tier, and does also
requiring empirical season hit_rate (clear rate over the line) fix it?"""
from calibration_tracker import _sb_fetch
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=_sb_fetch("select=stat_type,direction,line,result,hit_rate,p_over,confidence,pick_date"
               "&resolved=eq.true&result=neq.void&sport=eq.MLB")
rows=[r for r in rows if r.get("result") in ("hit","miss")]
def elite(r):
    return (r["stat_type"]=="Hitter Fantasy Score" and r["direction"]=="OVER"
            and 6.0<=f(r,"line")<=7.5 and f(r,"p_over")>=0.75)
el=[r for r in rows if elite(r)]
def rate(x):
    if not x: return "—"
    h=sum(r["result"]=="hit" for r in x); return f"{h}/{len(x)}={h/len(x):.1%}"
print(f"Elite v2 (current): {rate(el)}")
print(f"  avg p_over of these picks: {sum(f(r,'p_over') for r in el)/max(len(el),1):.2f}")
print(f"  → the tier realizes ~60% while picks are labeled 0.75+ (the gap you found)\n")
print("Add empirical season clear-rate floor (hit_rate) on top of elite v2:")
for thr in (0.55,0.60,0.65,0.70):
    seg=[r for r in el if f(r,"hit_rate")>=thr]
    days=len({r["pick_date"] for r in seg})
    tot_days=len({r["pick_date"] for r in el})
    print(f"  hit_rate>={thr:.2f}: {rate(seg):<14} ({len(seg)/max(tot_days,1):.1f}/day)")
print("\nElite v2 split by whether empirical agrees with model:")
agree=[r for r in el if f(r,"hit_rate")>=0.65]
disagree=[r for r in el if f(r,"hit_rate")<0.65]
print(f"  model & empirical agree (hr>=0.65): {rate(agree)}")
print(f"  model says yes, empirical weak (hr<0.65): {rate(disagree)}")
