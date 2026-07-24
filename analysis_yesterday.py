"""Grade every pick logged for a given date with the CURRENT (corrected)
formula. arg = date YYYY-MM-DD (default: yesterday ET)."""
import sys
from datetime import datetime, timezone, timedelta
from calibration_tracker import _sb_fetch, _fetch_actual_mlb
from parlay_builder import _passes_direction_gate
from daily_top_picks import _is_elite

date = sys.argv[1] if len(sys.argv) > 1 else (
    datetime.now(timezone.utc) - timedelta(hours=4) - timedelta(days=1)).strftime("%Y-%m-%d")
rows = _sb_fetch(f"select=player,sport,stat_type,direction,line,confidence,p_over,"
                 f"hit_rate,result,actual_value,pick_date&pick_date=eq.{date}")
print(f"Picks logged {date}: {len(rows)}")

def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0

def grade(r):
    # regrade live with corrected formula for MLB; trust stored for others
    if r.get("sport") == "MLB":
        # Strip the " (Goblin)" lock suffix — the box-score lookup only
        # knows real stat names (same handling as the resolver).
        lookup_stat = r["stat_type"].replace(" (Goblin)", "")
        a = _fetch_actual_mlb(r["player"], lookup_stat, date)
        if a in (None, "DNP"): return None, a
        line=f(r,"line"); return ((a>line) if r["direction"]=="OVER" else (a<line)), a
    if r.get("result") in ("hit","miss"): return r["result"]=="hit", r.get("actual_value")
    return None, None

for r in rows:
    r["line"]=f(r,"line"); r["hit_rate"]=f(r,"hit_rate")
def is_lock(r):
    return "(Goblin)" in (r.get("stat_type") or "")

buckets={"STAR":[], "LOCK":[], "gate":[], "all":[]}
print(f"\n{'':2}{'tier':5}{'player':<22}{'pick':<34}{'actual':>7}")
for r in sorted(rows, key=lambda x:(not (_is_elite(x) or is_lock(x)), -f(x,"confidence"))):
    hit,a = grade(r)
    tier = ("STAR" if _is_elite(r) else "LOCK" if is_lock(r)
            else "gate" if (r.get("sport")=="MLB" and _passes_direction_gate(r)) else "")
    mark = "⬜" if hit is None else ("✅" if hit else "❌")
    pick=f"{r['direction']} {r['line']} {r['stat_type']}"
    if _is_elite(r) or is_lock(r) or (r.get("sport")=="MLB" and _passes_direction_gate(r) and f(r,'confidence')>=0.7):
        print(f"{mark} {tier:5}{r['player']:<22}{pick:<34}{str(a):>7}")
    if hit is not None:
        buckets["all"].append(hit)
        if _is_elite(r): buckets["STAR"].append(hit)
        if is_lock(r): buckets["LOCK"].append(hit)
        # gate bucket = standard vetted MLB picks (exclude goblin locks)
        if r.get("sport")=="MLB" and not is_lock(r) and _passes_direction_gate(r):
            buckets["gate"].append(hit)
print()
for k in ("STAR","LOCK","gate","all"):
    b=buckets[k]
    if b: print(f"{k:5}: {sum(b)}/{len(b)} = {sum(b)/len(b):.0%}")
