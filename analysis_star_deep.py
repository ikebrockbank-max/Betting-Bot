"""Deep dive on the TIGHTENED star pool: verify on clean-formula era, then
slice every dimension to find a >=70% path at usable volume."""
from collections import defaultdict
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite, _is_prime

def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0

rows=_sb_fetch("select=player,sport,stat_type,direction,line,p_over,hit_rate,"
               "pitcher_tier,home_away,park_factor,batting_order,result,pick_date"
               "&resolved=eq.true&result=neq.void")
rows=[r for r in rows if r.get("result") in ("hit","miss")]
for r in rows:
    r["line"]=f(r,"line"); r["hit_rate"]=f(r,"hit_rate"); r["pitcher_tier"]=r.get("pitcher_tier") or ""
stars=[r for r in rows if _is_elite(r)]
days=len({r["pick_date"] for r in stars})
def rate(rs,minn=12):
    if len(rs)<minn: return None
    h=sum(x["result"]=="hit" for x in rs); return h,len(rs),h/len(rs)

base=rate(stars,1)
print(f"TIGHTENED STAR base: {base[0]}/{base[1]} = {base[2]:.1%}  ({base[1]/days:.1f}/day)")
clean=[r for r in stars if (r.get('pick_date') or '')>='2026-07-24']
c=rate(clean,1); print(f"  clean-formula era (since 7/24): {c[0]}/{c[1]} = {c[2]:.1%}")
pr=rate([r for r in stars if _is_prime(r)],1); print(f"  of which PRIME: {pr[0]}/{pr[1]} = {pr[2]:.1%}")

def sl(name,keyfn):
    cells=defaultdict(list)
    for r in stars:
        k=keyfn(r)
        if k is not None: cells[k].append(r)
    out=[(rr[2],rr[0],rr[1],k) for k,rs in cells.items() if (rr:=rate(rs))]
    if out:
        print(f"\nby {name}:")
        for rt,h,n,k in sorted(out,reverse=True):
            print(f"  {str(k):<20} {h:3d}/{n:<4d} = {rt:.1%}")

sl("pitcher_tier", lambda r: r["pitcher_tier"] or None)
sl("line", lambda r: r["line"])
sl("p_over band", lambda r: "0.80-0.85" if f(r,"p_over")>=0.80 else "0.75-0.80")
sl("season hit_rate", lambda r: "0.8+" if r["hit_rate"]>=0.8 else "0.7-0.8" if r["hit_rate"]>=0.7 else "<0.7")
sl("home/away", lambda r: r.get("home_away") or None)
sl("park_factor", lambda r: "hitter(>1.0)" if f(r,"park_factor")>1.0 else "pitcher(<1.0)" if 0<f(r,"park_factor")<1.0 else None)

print("\n=== >=70% candidates (stacked) ===")
def cand(name,pred):
    rr=rate([r for r in stars if pred(r)],10)
    if rr: print(f"  {name:<52} {rr[0]:3d}/{rr[1]:<4d} = {rr[2]:.1%}  {rr[1]/days:.1f}/day")
po=lambda r:f(r,"p_over")
weak=lambda r:r["pitcher_tier"] in ("weak","below_avg")
cand("line>=6.0", lambda r: r["line"]>=6.0)
cand("weak/below pitcher", weak)
cand("weak/below pitcher & line>=6.0", lambda r: weak(r) and r["line"]>=6.0)
cand("weak/below & p_over 0.80-0.85", lambda r: weak(r) and po(r)>=0.80)
cand("season hr>=0.8 & weak/below", lambda r: r["hit_rate"]>=0.8 and weak(r))
cand("home & weak/below", lambda r: r.get("home_away")=="home" and weak(r))
cand("line>=6 & p_over 0.80-0.85 (~prime)", lambda r: r["line"]>=6.0 and po(r)>=0.80)
