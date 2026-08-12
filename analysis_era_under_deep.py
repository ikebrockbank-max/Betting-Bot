"""Deeper ERA-Allowed UNDER dig: (1) confirm goblin vs regular lines, (2) try
to push past 70% with finer park bands + other signals (line, edge, opponent,
rest, day). Era-split stability required for any winner."""
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


rows = _sb_fetch("select=stat_type,direction,line,confidence,edge_pct,pitcher_tier,"
                 "park_factor,opp_team,rest_days,day_of_week,result,pick_date"
                 "&sport=eq.MLB&resolved=eq.true")
era = [r for r in rows if r.get("result") in ("hit", "miss")
       and (r.get("stat_type") or "").startswith("Earned Runs Allowed")
       and r.get("direction") == "UNDER"]

# 1) goblin vs regular
kinds = defaultdict(lambda: [0, 0])
for r in era:
    k = "GOBLIN" if "(Goblin)" in (r.get("stat_type") or "") else "REGULAR"
    kinds[k][0] += 1
    kinds[k][1] += 1 if r["result"] == "hit" else 0
print("=== line type (Earned Runs Allowed UNDER) ===")
for k, (n, h) in kinds.items():
    print(f"  {k}: {h}/{n} = {h/n:.0%}")

for r in era:
    r["line"] = _f(r, "line"); r["park_factor"] = _f(r, "park_factor")
    r["edge_pct"] = _f(r, "edge_pct"); r["hit"] = r["result"] == "hit"
dates = sorted(r["pick_date"] for r in era if r.get("pick_date"))
mid = dates[len(dates)//2] if dates else "2026-07-07"

# 2) push higher: only the parkLOW universe, add finer signals
low = [r for r in era if r["park_factor"] and r["park_factor"] < 1.0]
print(f"\n=== parkLOW universe: {sum(1 for r in low if r['hit'])}/{len(low)} "
      f"= {sum(1 for r in low if r['hit'])/len(low):.0%} ===")


def cells(r):
    out = []
    p = r["park_factor"]
    out.append(f"park<{'0.95' if p<0.95 else '0.97' if p<0.97 else '1.0'}")
    out.append(f"line{'<=2.5' if r['line']<=2.5 else '3' if r['line']<=3 else '3.5+'}")
    out.append(f"edge{'>=0.15' if r['edge_pct']>=0.15 else '<0.15'}")
    out.append(f"conf{'>=.70' if r['confidence']>=0.70 else '<.70' if 'confidence' in r else '?'}")
    rd = r.get("rest_days")
    if rd not in (None, ""):
        out.append(f"rest{'0-4' if _f(r,'rest_days')<=4 else '5+'}")
    # 2-way with the sharpest park band
    if p < 0.97:
        out.append(f"park<0.97+line{'<=2.5' if r['line']<=2.5 else '3+'}")
        out.append(f"park<0.97+edge{'>=0.15' if r['edge_pct']>=0.15 else '<0.15'}")
    return out


groups = defaultdict(list)
for r in low:
    for c in cells(r):
        groups[c].append(r)

res = []
for c, sub in groups.items():
    if len(sub) < 25:
        continue
    h = sum(1 for x in sub if x["hit"]); n = len(sub)
    first = [x for x in sub if x["pick_date"] < mid]; second = [x for x in sub if x["pick_date"] >= mid]
    r1 = sum(1 for x in first if x["hit"])/len(first) if first else 0
    r2 = sum(1 for x in second if x["hit"])/len(second) if second else 0
    stable = len(first) >= 8 and len(second) >= 8 and r1 >= 0.62 and r2 >= 0.62
    res.append((wlb(h, n), h/n, h, n, r1, r2, stable, c))
res.sort(reverse=True)
print(f"\n{'LB':>5} {'rate':>5} {'h/n':>8} {'1st':>4} {'2nd':>4} {'ok':>3}  filter")
print("-"*66)
for lb, rate, h, n, r1, r2, stable, c in res:
    print(f"{lb:5.2f} {rate:5.0%} {h:3}/{n:<3} {r1:4.0%} {r2:4.0%} {'✅' if stable else '  ':>3}  {c}")
