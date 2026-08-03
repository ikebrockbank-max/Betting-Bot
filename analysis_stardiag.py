"""Why does the scorecard drop logged stars? Grade each 8/1 star like the
scorecard does and show the result."""
from calibration_tracker import _sb_fetch, _fetch_actual_mlb
from daily_top_picks import _is_elite
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
for date in ("2026-07-31","2026-08-01"):
    rows=_sb_fetch(f"select=player,stat_type,direction,line,p_over,pitcher_tier,park_factor,result,pick_date&pick_date=eq.{date}")
    for r in rows: r["line"]=f(r,"line")
    stars=[r for r in rows if _is_elite(r)]
    print(f"\n{date}: {len(stars)} stars logged")
    for r in stars:
        actual=_fetch_actual_mlb(r["player"], r["stat_type"].replace(" (Goblin)",""), date)
        stored=r.get("result")
        print(f"  {r['player']:<20} O{r['line']} | stored_result={stored} | live_actual={actual}")
