from calibration_tracker import _sb_fetch
rows=_sb_fetch("select=player,stat_type,line,direction,pitcher_tier,p_over,park_factor,pick_date&pick_date=gte.2026-07-30&player=like.*Suzuki*")
for r in rows:
    print(f"{r.get('pick_date')} {r.get('player')} {r.get('direction')} {r.get('line')} {r.get('stat_type')} | pitcher={r.get('pitcher_tier')} p_over={r.get('p_over')}")
