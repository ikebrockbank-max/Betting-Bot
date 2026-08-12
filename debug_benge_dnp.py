"""Reproduce the Benge DNP-counted-as-hit bug. Show: (1) what's stored in
pick_log for Carson Benge, (2) which player id find_player_id resolves and
whether it's the right person, (3) his 2026 game-log dates, (4) what
_fetch_actual_mlb returns for recent dates + how that grades an OVER."""
import json, urllib.request
from calibration_tracker import _sb_fetch, _fetch_actual_mlb
from data.mlb_batter_stats import find_player_id

NAME = "Carson Benge"

print("=== pick_log rows for Carson Benge ===")
rows = _sb_fetch("select=player,pick_date,stat_type,direction,line,result,resolved&player=eq.Carson%20Benge")
for r in rows:
    print(f"  {r.get('pick_date')} {r.get('stat_type')} {r.get('direction')} "
          f"{r.get('line')} -> result={r.get('result')} resolved={r.get('resolved')}")

print("\n=== find_player_id resolution ===")
pid = find_player_id(NAME)
print("pid:", pid)
if pid:
    info = json.loads(urllib.request.urlopen(
        f"https://statsapi.mlb.com/api/v1/people/{pid}", timeout=10).read())
    p = info.get("people", [{}])[0]
    print(f"  resolved to: {p.get('fullName')} | team debut {p.get('mlbDebutDate')} "
          f"| position {p.get('primaryPosition',{}).get('abbreviation')}")

    gl = json.loads(urllib.request.urlopen(
        f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=hitting&season=2026",
        timeout=10).read())
    splits = gl.get("stats", [{}])[0].get("splits", [])
    print(f"  2026 hitting game-log entries: {len(splits)}")
    for s in splits[-8:]:
        st = s.get("stat", {})
        print(f"    {s.get('date')}  H{st.get('hits')} 2B{st.get('doubles')} "
              f"HR{st.get('homeRuns')} R{st.get('runs')} RBI{st.get('rbi')} BB{st.get('baseOnBalls')}")

print("\n=== _fetch_actual_mlb for recent dates (Hitter Fantasy Score) ===")
for d in ["2026-08-09", "2026-08-10", "2026-08-11"]:
    a = _fetch_actual_mlb(NAME, "Hitter Fantasy Score", d)
    verdict = "VOID/pending" if a in (None, "DNP") else f"value={a}"
    grade = "n/a" if a in (None, "DNP") else ("HIT" if a > 6.5 else "miss")
    print(f"  {d}: returns {a!r} -> {verdict} | OVER 6.5 would grade: {grade}")
