"""Do star picks vs SEASON-ELITE pitchers (who got tiered 'soft' via a recent
slump) underperform? Reconstruct each star's opposing starter + season ERA."""
import json, urllib.request, time
from collections import defaultdict
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite
from data.mlb_batter_stats import find_player_id, get_player_team_id
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=[r for r in _sb_fetch("select=player,stat_type,direction,line,p_over,pitcher_tier,"
     "park_factor,result,pick_date&resolved=eq.true&result=neq.void")
     if r.get("result") in ("hit","miss")]
for r in rows: r["line"]=f(r,"line")
stars=[r for r in rows if _is_elite(r)]
print(f"Resolved stars: {len(stars)}")

_sched={}; _era={}
def opp_starter_era(team_id, date):
    if not team_id: return None
    key=date
    if key not in _sched:
        try:
            _sched[key]=json.loads(urllib.request.urlopen(
              f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}&hydrate=probablePitcher",timeout=15).read())
        except Exception: _sched[key]={}
        time.sleep(0.03)
    for d in _sched[key].get("dates",[]):
        for g in d.get("games",[]):
            home=g["teams"]["home"]; away=g["teams"]["away"]
            opp_pid=None
            if home["team"]["id"]==team_id: opp_pid=away.get("probablePitcher",{}).get("id")
            elif away["team"]["id"]==team_id: opp_pid=home.get("probablePitcher",{}).get("id")
            if opp_pid:
                if opp_pid not in _era:
                    try:
                        s=json.loads(urllib.request.urlopen(
                          f"https://statsapi.mlb.com/api/v1/people/{opp_pid}/stats?stats=season&group=pitching&season=2026",timeout=15).read())
                        splits=s.get("stats",[{}])[0].get("splits",[])
                        _era[opp_pid]=float(splits[0]["stat"]["era"]) if splits else None
                    except Exception: _era[opp_pid]=None
                    time.sleep(0.03)
                return _era[opp_pid]
    return None

buckets=defaultdict(list)
for r in stars:
    tid=get_player_team_id(r["player"])
    era=opp_starter_era(tid, r["pick_date"])
    if era is None: buckets["unknown"].append(r); continue
    b="elite (ERA<3.0)" if era<3.0 else "good (3.0-3.75)" if era<3.75 else "avg+ (>3.75)"
    buckets[b].append(r)
def rate(rs):
    if not rs: return "0/0"
    h=sum(x["result"]=="hit" for x in rs); return f"{h}/{len(rs)} = {h/len(rs):.0%}"
print("\nStar hit rate by opposing starter's SEASON ERA:")
for b in ("elite (ERA<3.0)","good (3.0-3.75)","avg+ (>3.75)","unknown"):
    if buckets[b]: print(f"  {b:<20} {rate(buckets[b])}")
