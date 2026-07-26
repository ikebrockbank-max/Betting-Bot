"""How often are opponent/park/pitcher-context fields actually populated
for recent MLB picks (vs falling back to unknown/missing)?"""
from datetime import date, timedelta
from calibration_tracker import _sb_fetch
cut=(date.today()-timedelta(days=14)).isoformat()
rows=_sb_fetch(f"select=sport,stat_type,opp_team,pitcher_tier,park_factor,home_away,pick_date"
               f"&sport=eq.MLB&pick_date=gte.{cut}")
n=len(rows)
def pct(cond): 
    c=sum(1 for r in rows if cond(r)); return f"{c}/{n} = {c/max(n,1):.0%}"
print(f"MLB picks last 14d: {n}")
print(f"  opp_team known:      {pct(lambda r: r.get('opp_team') and r['opp_team']!='unknown')}")
print(f"  pitcher_tier known:  {pct(lambda r: r.get('pitcher_tier') and r['pitcher_tier'] not in ('','unknown'))}")
print(f"  park_factor present: {pct(lambda r: r.get('park_factor') is not None)}")
print(f"  home_away known:     {pct(lambda r: r.get('home_away') and r['home_away']!='unknown')}")
