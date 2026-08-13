"""Final pre-build ERA-under check: pin the EXACT refined pocket
(park 0.95-1.0, line 2.5, +/- away) with n / rate / Wilson LB / era-split, and
test whether the model's own edge_pct / confidence adds signal on top. Goal: a
definition that's stable across both halves of the season, not curve-fit."""
import math
from collections import defaultdict
from calibration_tracker import _sb_fetch


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def wlb(h, n, z=1.96):
    if n == 0:
        return 0.0
    p = h / n
    return (p + z*z/(2*n) - z*math.sqrt((p*(1-p)+z*z/(4*n))/n)) / (1 + z*z/n)


rows = _sb_fetch("select=player,line,confidence,edge_pct,p_under,park_factor,"
                 "home_away,result,pick_date&sport=eq.MLB"
                 "&stat_type=eq.Earned%20Runs%20Allowed&direction=eq.UNDER&resolved=eq.true")
rows = [r for r in rows if r.get("result") in ("hit", "miss")]
for r in rows:
    r["line"] = _f(r, "line"); r["park_factor"] = _f(r, "park_factor")
    r["edge_pct"] = _f(r, "edge_pct"); r["confidence"] = _f(r, "confidence")
    r["p_under"] = _f(r, "p_under"); r["hit"] = r["result"] == "hit"
    r["home_away"] = (r.get("home_away") or "?").lower()
dates = sorted(r["pick_date"] for r in rows if r.get("pick_date"))
mid = dates[len(dates)//2]


def report(label, sub):
    if not sub:
        print(f"  {label:38} (none)"); return
    h = sum(1 for r in sub if r["hit"]); n = len(sub)
    f = [r for r in sub if r["pick_date"] < mid]; s = [r for r in sub if r["pick_date"] >= mid]
    r1 = sum(1 for r in f if r["hit"])/len(f) if f else 0
    r2 = sum(1 for r in s if r["hit"])/len(s) if s else 0
    ok = "✅" if (len(f) >= 8 and len(s) >= 8 and r1 >= 0.65 and r2 >= 0.65) else "  "
    print(f"  {label:38} {h:3}/{n:<3} = {h/n:.0%}  LB{wlb(h,n):.2f}  [1st {r1:.0%} 2nd {r2:.0%}] {ok}")


print(f"date split at {mid}\n=== candidate definitions ===")
mild = [r for r in rows if 0.95 <= r["park_factor"] < 1.0]
report("park 0.95-1.0 (all lines)", mild)
report("park 0.95-1.0 + line 2.5", [r for r in mild if r["line"] == 2.5])
report("park 0.95-1.0 + line 2.5 + AWAY", [r for r in mild if r["line"] == 2.5 and r["home_away"] == "away"])
report("park 0.95-1.0 + line 2.5 + HOME", [r for r in mild if r["line"] == 2.5 and r["home_away"] == "home"])

print("\n=== does the model's own signal add? (within park 0.95-1.0, line 2.5) ===")
base = [r for r in mild if r["line"] == 2.5]
report("all", base)
report("edge_pct >= 0.15", [r for r in base if r["edge_pct"] >= 0.15])
report("edge_pct < 0.15", [r for r in base if r["edge_pct"] < 0.15])
report("p_under >= 0.60", [r for r in base if r["p_under"] >= 0.60])
report("confidence >= 0.65", [r for r in base if r["confidence"] >= 0.65])

print("\n=== volume of the shippable pocket ===")
print(f"park 0.95-1.0 + line 2.5: {len([r for r in base])} picks "
      f"over {len(set(r['pick_date'] for r in base))} distinct days")
