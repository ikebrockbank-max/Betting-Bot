"""Backtest a recency guard on the star tier: for each resolved star, pull
the player's games BEFORE the pick and see how many cleared the line, then
compare hit rates for cold (0/3) vs warm vs hot pre-pick form."""
import json, urllib.request, time
from collections import defaultdict
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite
from data.mlb_batter_stats import find_player_id, compute_hitter_fs
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=[r for r in _sb_fetch("select=player,stat_type,direction,line,p_over,pitcher_tier,"
     "park_factor,result,pick_date&resolved=eq.true&result=neq.void")
     if r.get("result") in ("hit","miss")]
for r in rows: r["line"]=f(r,"line")
stars=[r for r in rows if _is_elite(r)]
print(f"Resolved stars: {len(stars)}")

_cache={}
def gamelog(player):
    if player in _cache: return _cache[player]
    pid=find_player_id(player); sp={}
    if pid:
        try:
            d=json.loads(urllib.request.urlopen(f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=hitting&season=2026",timeout=10).read())
            for s in d.get("stats",[{}])[0].get("splits",[]):
                sp[s.get("date")]=compute_hitter_fs(s["stat"])
        except Exception: pass
        time.sleep(0.05)
    _cache[player]=sp; return sp

buckets=defaultdict(list)
for r in stars:
    log=gamelog(r["player"])
    prior=sorted([(d,v) for d,v in log.items() if d<r["pick_date"]])[-3:]
    if len(prior)<3:
        buckets["<3 games"].append(r); continue
    cleared=sum(1 for _,v in prior if v>r["line"])
    buckets[f"{cleared}/3 cleared"].append(r)
def rate(rs):
    if not rs: return "0/0"
    h=sum(x["result"]=="hit" for x in rs); return f"{h}/{len(rs)} = {h/len(rs):.0%}"
print("\nStar hit rate by pre-pick form (cleared line in last 3 games):")
for k in ("0/3 cleared","1/3 cleared","2/3 cleared","3/3 cleared","<3 games"):
    if buckets[k]: print(f"  {k:<14} {rate(buckets[k])}")
# the guard: exclude 0/3 (cold)
warm=[r for r in stars if r not in buckets["0/3 cleared"]]
print(f"\nAll stars:              {rate(stars)}")
print(f"Excluding 0/3 cold:     {rate(warm)}   (drops {len(buckets['0/3 cleared'])} picks)")
