"""
Re-grade all resolved Hitter Fantasy Score picks with the corrected
PrizePicks formula (the pre-2026-07-24 grades used a wrong formula).

Usage (via adhoc_report.yml):
  arg = "dry"   -> report old vs new hit rate + how many flip, NO writes
  arg = "apply" -> patch changed rows in Supabase, then report

One game-log fetch per distinct player (cached), then re-score locally
via the shared compute_hitter_fs, so picks and grades share one scale.
"""
import sys, json, urllib.request, time
from calibration_tracker import _sb_fetch, _sb_patch
from data.mlb_batter_stats import find_player_id, compute_hitter_fs

MODE = sys.argv[1] if len(sys.argv) > 1 else "dry"

rows = _sb_fetch("select=pick_date,player,line,direction,result,actual_value"
                 "&sport=eq.MLB&stat_type=eq.Hitter Fantasy Score"
                 "&resolved=eq.true&result=neq.void")
rows = [r for r in rows if r.get("result") in ("hit", "miss")]
print(f"Re-grading {len(rows)} resolved HFS picks (mode={MODE})")

_log_cache = {}
def game_log(player):
    if player in _log_cache:
        return _log_cache[player]
    pid = find_player_id(player)
    splits = {}
    if pid:
        try:
            url = (f"https://statsapi.mlb.com/api/v1/people/{pid}/stats"
                   f"?stats=gameLog&group=hitting&season=2026")
            data = json.loads(urllib.request.urlopen(url, timeout=10).read())
            for s in data.get("stats", [{}])[0].get("splits", []):
                splits[s.get("date")] = s["stat"]
        except Exception:
            pass
        time.sleep(0.05)
    _log_cache[player] = splits
    return splits

old_h = new_h = flips = unresolvable = 0
changed = []
for r in rows:
    st = game_log(r["player"]).get(r["pick_date"])
    if st is None:
        unresolvable += 1
        continue
    actual = round(compute_hitter_fs(st), 2)
    line = float(r["line"]); direction = r["direction"]
    new_hit = (actual > line) if direction == "OVER" else (actual < line)
    old_hit = (r["result"] == "hit")
    old_h += old_hit
    new_h += new_hit
    if new_hit != old_hit:
        flips += 1
        changed.append((r, actual, new_hit))

n = len(rows) - unresolvable
print(f"\nResolvable: {n}  (couldn't re-fetch: {unresolvable})")
print(f"OLD graded hit rate: {old_h}/{len(rows)} = {old_h/max(len(rows),1):.1%}")
print(f"NEW graded hit rate: {new_h}/{n} = {new_h/max(n,1):.1%}")
print(f"Grades that FLIP: {flips} ({flips/max(n,1):.1%} of resolvable)")
fh2m = sum(1 for _,__,nh in changed if not nh)
fm2h = sum(1 for _,__,nh in changed if nh)
print(f"  hit->miss: {fh2m}   miss->hit: {fm2h}")

if MODE == "apply" and changed:
    ok = 0
    for r, actual, new_hit in changed:
        upd = {"actual_value": actual, "result": "hit" if new_hit else "miss"}
        if _sb_patch(r["pick_date"], r["player"], "Hitter Fantasy Score", upd):
            ok += 1
    print(f"\nApplied {ok}/{len(changed)} corrections to Supabase.")
elif MODE == "dry":
    print("\nDRY RUN — no writes. Sample of flips:")
    for r, actual, new_hit in changed[:12]:
        print(f"  {r['pick_date']} {r['player']:<20} {r['direction']} {r['line']}: "
              f"was {r['result']}, actual={actual} -> {'hit' if new_hit else 'miss'}")
