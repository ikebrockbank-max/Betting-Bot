"""Find the highest-hit-rate sub-slices WITHIN the star tier, so we can see
if a stricter 'best of the best' definition beats the tier's ~57%."""
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite

def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0

rows=_sb_fetch("select=player,sport,stat_type,direction,line,p_over,hit_rate,"
               "pitcher_tier,home_away,result,pick_date&resolved=eq.true&result=neq.void")
rows=[r for r in rows if r.get("result") in ("hit","miss")]
for r in rows: r["line"]=f(r,"line")
stars=[r for r in rows if _is_elite(r)]

def rate(rs, minn=15):
    if len(rs)<minn: return None
    h=sum(x["result"]=="hit" for x in rs); return h,len(rs),h/len(rs)

def slice_by(name, keyfn):
    from collections import defaultdict
    cells=defaultdict(list)
    for r in stars:
        k=keyfn(r)
        if k is not None: cells[k].append(r)
    out=[]
    for k,rs in cells.items():
        rr=rate(rs)
        if rr: out.append((rr[2],rr[0],rr[1],k))
    if out:
        print(f"\nby {name}:")
        for rt,h,n,k in sorted(out,reverse=True):
            print(f"  {str(k):<22} {h:3d}/{n:<4d} = {rt:.1%}")

print(f"STAR tier base: {sum(r['result']=='hit' for r in stars)}/{len(stars)} = {sum(r['result']=='hit' for r in stars)/len(stars):.1%}")
slice_by("p_over band", lambda r: ("0.85+" if f(r,"p_over")>=0.85 else "0.80-0.85" if f(r,"p_over")>=0.80 else "0.75-0.80"))
slice_by("opposing pitcher tier", lambda r: r.get("pitcher_tier") or "unknown")
slice_by("season hit_rate", lambda r: ("0.8+" if f(r,"hit_rate")>=0.8 else "0.7-0.8" if f(r,"hit_rate")>=0.7 else "<0.7"))
slice_by("line", lambda r: ("4.5-5" if r["line"]<=5 else "5.5-6" if r["line"]<=6 else "6.5"))
slice_by("home/away", lambda r: r.get("home_away") or None)
# stacked: best combo
best=[r for r in stars if f(r,"p_over")>=0.82 and (r.get("pitcher_tier") or "") in ("weak","below_avg","average")]
rr=rate(best,10)
if rr: print(f"\nSTACKED (p_over>=0.82 & pitcher weak/below/avg): {rr[0]}/{rr[1]} = {rr[2]:.1%}")
