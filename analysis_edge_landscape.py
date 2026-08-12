"""Near-miss landscape: for every (sport, stat_type, direction) with enough
volume, show the OVERALL rate and the BEST filtered sub-slice (n>=25). Surfaces
areas that are 63-70% and might reach >=70% with one more filter — the refine
targets — vs areas that are hopeless coinflips. Relaxed bar vs the strict hunt."""
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


rows = _sb_fetch("select=sport,stat_type,direction,confidence,pitcher_tier,park_factor,"
                 "home_away,batting_order,edge_pct,result,pick_date&resolved=eq.true")
rows = [r for r in rows if r.get("result") in ("hit", "miss")]
for r in rows:
    r["confidence"] = _f(r, "confidence"); r["park_factor"] = _f(r, "park_factor")
    r["pitcher_tier"] = r.get("pitcher_tier") or "?"
    r["home_away"] = (r.get("home_away") or "?").lower()
    r["hit"] = r["result"] == "hit"


def subslices(r):
    out = ["all"]
    c = r["confidence"]
    out.append(f"conf>={'0.75' if c>=0.75 else '0.70' if c>=0.70 else 'lt70'}")
    if r["park_factor"] > 0:
        out.append("park>=1.0" if r["park_factor"] >= 1.0 else "park<1.0")
    out.append(f"pit:{r['pitcher_tier']}")
    out.append(f"ha:{r['home_away']}")
    return out


groups = defaultdict(lambda: defaultdict(list))
for r in rows:
    key = f"{r['sport']}|{r['stat_type']}|{r['direction']}"
    for s in subslices(r):
        groups[key][s].append(r)

results = []
for key, subs in groups.items():
    allrows = subs["all"]
    if len(allrows) < 40:
        continue
    ah = sum(1 for r in allrows if r["hit"])
    arate = ah / len(allrows)
    # best sub-slice by rate with n>=25
    best = None
    for s, sub in subs.items():
        if s == "all" or len(sub) < 25:
            continue
        h = sum(1 for r in sub if r["hit"]); n = len(sub)
        rate = h / n
        if best is None or rate > best[0]:
            best = (rate, h, n, wlb(h, n), s)
    results.append((arate, ah, len(allrows), best, key))

results.sort(reverse=True)
print(f"resolved: {len(rows)}\n")
print(f"{'ovr':>4} {'n':>5}  {'best-slice-rate':>15}  market | best filter")
print("-" * 92)
for arate, ah, an, best, key in results:
    bs = ""
    if best:
        rate, h, n, lb, s = best
        bs = f"{rate:.0%} ({h}/{n}, LB{lb:.2f}) via {s}"
    print(f"{arate:4.0%} {an:5}  {bs:>15}  {key}")
