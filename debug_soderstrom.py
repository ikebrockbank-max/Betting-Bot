"""Check Soderstrom the same way: pick_log rows, player-id resolution, what
_fetch_actual_mlb returns for 08-11, and whether he ACTUALLY appeared in the
08-11 boxscore (or was a true DNP that got mis-graded)."""
import json, urllib.request
from calibration_tracker import _sb_fetch, _fetch_actual_mlb
from data.mlb_batter_stats import find_player_id


def get(u):
    return json.loads(urllib.request.urlopen(u, timeout=12).read())


NAME = "Tyler Soderstrom"
print("=== pick_log rows for Soderstrom (last ~10) ===")
rows = _sb_fetch("select=player,pick_date,stat_type,direction,line,result,resolved"
                 "&player=eq.Tyler%20Soderstrom&order=pick_date.desc&limit=10")
for r in rows:
    print(f"  {r.get('pick_date')} {r.get('stat_type')} {r.get('direction')} "
          f"{r.get('line')} -> result={r.get('result')} resolved={r.get('resolved')}")

pid = find_player_id(NAME)
print(f"\nfind_player_id -> {pid}")
if pid:
    info = get(f"https://statsapi.mlb.com/api/v1/people/{pid}")
    p = info.get("people", [{}])[0]
    print(f"  resolved to: {p.get('fullName')} pos={p.get('primaryPosition',{}).get('abbreviation')}")

print("\n=== what _fetch_actual_mlb returns for 08-10 / 08-11 ===")
for d in ["2026-08-10", "2026-08-11"]:
    for stat in ["Hitter Fantasy Score", "Hits"]:
        a = _fetch_actual_mlb(NAME, stat, d)
        print(f"  {d} {stat}: {a!r}")

print("\n=== game-log entries around 08-11 (with real game date) ===")
if pid:
    gl = get(f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=hitting&season=2026")
    splits = gl.get("stats", [{}])[0].get("splits", [])
    for s in splits[-6:]:
        st = s.get("stat", {})
        print(f"  split={s.get('date')} gamePk={s.get('game',{}).get('gamePk')} "
              f"PA={st.get('plateAppearances')} AB={st.get('atBats')} H={st.get('hits')}")

print("\n=== did Soderstrom appear in any 08-11 boxscore? ===")
sched = get("https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-08-11")
games = [g["gamePk"] for dd in sched.get("dates", []) for g in dd.get("games", [])]
found = False
for pk in games:
    try:
        box = get(f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore")
        for side in ("home", "away"):
            players = box.get("teams", {}).get(side, {}).get("players", {})
            key = f"ID{pid}"
            if key in players:
                bat = players[key].get("stats", {}).get("batting", {})
                gs = players[key].get("gameStatus", {})
                print(f"  game {pk}: PRESENT PA={bat.get('plateAppearances')} "
                      f"AB={bat.get('atBats')} H={bat.get('hits')} bench={gs.get('isOnBench')} sub={gs.get('isSubstitute')}")
                found = True
    except Exception:
        pass
if not found:
    print("  Soderstrom did NOT appear in any 08-11 boxscore -> REAL DNP")
