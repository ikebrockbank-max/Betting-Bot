"""Examine every resolved Lock with its features to see what misses share,
and test whether the star-proven signals (park, pitcher) would help."""
from calibration_tracker import _sb_fetch
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=[r for r in _sb_fetch("select=player,stat_type,line,p_over,hit_rate,pitcher_tier,"
     "park_factor,result,pick_date&resolved=eq.true&result=neq.void")
     if r.get("result") in ("hit","miss")]
locks=[r for r in rows if "(Goblin)" in (r.get("stat_type") or "")]
print(f"Resolved Locks: {len(locks)}")
print(f"{'res':4}{'player':<20}{'line':>5} {'p_over':>7}{'seas_hr':>8}{'pitcher':>11}{'park':>6}")
for r in sorted(locks,key=lambda x:x.get('result')):
    m="✅" if r["result"]=="hit" else "❌"
    print(f"{m:4}{r.get('player','?'):<20}{f(r,'line'):>5.1f} {f(r,'p_over'):>7.2f}{f(r,'hit_rate'):>8.2f}"
          f"{(r.get('pitcher_tier') or '?'):>11}{f(r,'park_factor'):>6.2f}")
def rate(rs):
    if not rs: return "0/0"
    h=sum(x['result']=='hit' for x in rs); return f"{h}/{len(rs)} = {h/len(rs):.0%}"
print(f"\nbaseline: {rate(locks)}")
print(f"park>=1.0:            {rate([r for r in locks if f(r,'park_factor')>=1.0])}")
print(f"soft pitcher:         {rate([r for r in locks if (r.get('pitcher_tier') or '') in ('weak','below_avg','average')])}")
print(f"p_over>=0.90:         {rate([r for r in locks if f(r,'p_over')>=0.90])}")
print(f"season hr>=0.90:      {rate([r for r in locks if f(r,'hit_rate')>=0.90])}")
print(f"park>=1.0 & soft pit: {rate([r for r in locks if f(r,'park_factor')>=1.0 and (r.get('pitcher_tier') or '') in ('weak','below_avg','average')])}")
