"""Refine the WNBA combo-OVER-hot edge: slice by home/away, p_over, n_games
(returning-player proxy — low n = unstable), edge, opponent. Keep n honest."""
from collections import defaultdict
from calibration_tracker import _sb_fetch
def f(r,k,d=None):
    v=r.get(k)
    if v in (None,""): return d
    try: return float(v)
    except: return d
rows=[r for r in _sb_fetch("select=sport,stat_type,direction,p_over,park_factor,hit_rate,"
     "trend,line,home_away,opp_team,n_games,edge_pct,result,pick_date&resolved=eq.true&result=neq.void")
     if r.get("result") in ("hit","miss")]
# the validated pool: WNBA PRA + Pts+Rebs OVER, hot
pool=[r for r in rows if r.get("sport")=="WNBA" and r.get("direction")=="OVER"
      and r.get("stat_type") in ("Pts+Rebs+Asts","Pts+Rebs") and (f(r,"trend") or 0)>0.15]
def rate(rs,minn=8):
    if len(rs)<minn: return f"n={len(rs)} (thin)"
    h=sum(x["result"]=="hit" for x in rs); return f"{h}/{len(rs)} = {h/len(rs):.0%}"
print(f"WNBA combo-OVER-hot pool: {rate(pool,1)}\n")
def sl(name,keyfn):
    cells=defaultdict(list)
    for r in pool:
        k=keyfn(r)
        if k is not None: cells[k].append(r)
    print(f"by {name}:")
    for k,rs in sorted(cells.items(), key=lambda kv:-(sum(x['result']=='hit' for x in kv[1])/max(len(kv[1]),1))):
        print(f"  {str(k):<16} {rate(rs)}")
    print()
sl("home_away", lambda r: r.get("home_away") if r.get("home_away") not in (None,"","unknown") else None)
sl("p_over band", lambda r: ("0.80+" if (f(r,"p_over") or 0)>=0.80 else "0.70-0.80" if (f(r,"p_over") or 0)>=0.70 else "<0.70"))
sl("n_games (return proxy)", lambda r: ("<6 (unstable)" if (f(r,"n_games") or 99)<6 else "6-9" if (f(r,"n_games") or 99)<10 else "10+ (established)"))
sl("edge_pct", lambda r: (">0.35" if (f(r,"edge_pct") or 0)>0.35 else "0.2-0.35" if (f(r,"edge_pct") or 0)>0.2 else "<0.2"))
sl("stat", lambda r: r.get("stat_type"))
sl("trend strength", lambda r: ("very hot >0.30" if (f(r,"trend") or 0)>0.30 else "hot 0.15-0.30"))
