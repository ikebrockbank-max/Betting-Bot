"""Tier hit rates since the HFS formula fix (2026-07-24) — the clean-data era.
Grades live from box scores so late/unresolved picks still count."""
import sys
from calibration_tracker import _sb_fetch, _fetch_actual_mlb
from daily_top_picks import _is_elite, _is_prime
FIX="2026-07-24"
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=_sb_fetch(f"select=player,sport,stat_type,direction,line,p_over,hit_rate,"
               f"pitcher_tier,result,pick_date&pick_date=gte.{FIX}")
for r in rows:
    r["line"]=f(r,"line"); r["hit_rate"]=f(r,"hit_rate"); r["pitcher_tier"]=r.get("pitcher_tier") or ""
def is_lock(r): return "(Goblin)" in (r.get("stat_type") or "")
def grade(r):
    if r.get("sport")=="MLB":
        a=_fetch_actual_mlb(r["player"], r["stat_type"].replace(" (Goblin)",""), r["pick_date"])
        if a in (None,"DNP"): return None
        return (a>r["line"]) if r["direction"]=="OVER" else (a<r["line"])
    if r.get("result") in ("hit","miss"): return r["result"]=="hit"
    return None
buckets={"PRIME":[],"STAR":[],"LOCK":[]}
for r in rows:
    h=grade(r)
    if h is None: continue
    if _is_prime(r): buckets["PRIME"].append(h)
    if _is_elite(r) and not _is_prime(r): buckets["STAR"].append(h)
    if is_lock(r): buckets["LOCK"].append(h)
print(f"SINCE FORMULA FIX ({FIX}), clean-data era:")
for k in ("PRIME","STAR","LOCK"):
    b=buckets[k]
    if b: print(f"  {k:6}: {sum(b)}/{len(b)} = {sum(b)/len(b):.1%}")
    else: print(f"  {k:6}: (none)")
# combined elite (prime+star)
allstar=buckets["PRIME"]+buckets["STAR"]
if allstar: print(f"  ALL STARS (prime+star): {sum(allstar)}/{len(allstar)} = {sum(allstar)/len(allstar):.1%}")
# by day
from collections import defaultdict
byday=defaultdict(lambda:[0,0])
for r in rows:
    if _is_elite(r):
        h=grade(r)
        if h is None: continue
        byday[r["pick_date"]][0]+=h; byday[r["pick_date"]][1]+=1
print("\n  Stars by day:")
for d in sorted(byday):
    h,n=byday[d]; print(f"    {d}: {h}/{n}")
