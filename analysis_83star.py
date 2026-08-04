from calibration_tracker import _sb_fetch, _fetch_actual_mlb
from daily_top_picks import _is_elite
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=_sb_fetch("select=player,stat_type,direction,line,p_over,pitcher_tier,park_factor,confidence,result,pick_date&pick_date=eq.2026-08-03")
for r in rows: r["line"]=f(r,"line")
stars=[r for r in rows if _is_elite(r)]
print(f"8/3 logged stars: {len(stars)}")
for r in stars:
    a=_fetch_actual_mlb(r["player"], r["stat_type"], "2026-08-03")
    print(f"  {r['player']} OVER {r['line']} | p_over={f(r,'p_over'):.2f} pit={r.get('pitcher_tier')} park={f(r,'park_factor'):.2f} | result={r.get('result')} actual={a}")
