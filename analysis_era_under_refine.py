"""Refine the one live lead: MLB Earned Runs Allowed UNDER in pitcher parks
(69% raw). Cross park x pitcher_tier x confidence x line to find a >=70% pocket
with n>=30 AND era-split stability (both halves >=0.62). Earned Runs Allowed is
a raw box-score stat (not the PFS formula), so it's unaffected by the old
scoring bug."""
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


rows = _sb_fetch("select=stat_type,direction,line,confidence,pitcher_tier,park_factor,"
                 "home_away,result,pick_date&sport=eq.MLB&resolved=eq.true")
rows = [r for r in rows if r.get("result") in ("hit", "miss")
        and r.get("stat_type") == "Earned Runs Allowed" and r.get("direction") == "UNDER"]
for r in rows:
    r["line"] = _f(r, "line"); r["confidence"] = _f(r, "confidence")
    r["park_factor"] = _f(r, "park_factor"); r["pitcher_tier"] = r.get("pitcher_tier") or "?"
    r["hit"] = r["result"] == "hit"
dates = sorted(r["pick_date"] for r in rows if r.get("pick_date"))
mid = dates[len(dates)//2] if dates else "2026-07-15"
print(f"Earned Runs Allowed UNDER: {len(rows)} resolved | split {mid}")
ah = sum(1 for r in rows if r["hit"])
print(f"overall: {ah}/{len(rows)} = {ah/len(rows):.0%}\n")


def cells(r):
    out = []
    pk = "parkLOW" if r["park_factor"] and r["park_factor"] < 1.0 else "parkHI/?"
    tier = r["pitcher_tier"]
    strong = tier in ("ace", "above_avg", "good")
    cb = "c>=.70" if r["confidence"] >= 0.70 else "c<.70"
    lb = "line<=2.5" if r["line"] <= 2.5 else "line>2.5"
    out.append(("park", pk))
    out.append(("tier", f"tier:{tier}"))
    out.append(("strongpit", "strongPit" if strong else "softPit"))
    out.append(("park+strong", f"{pk}+{'strongPit' if strong else 'softPit'}"))
    out.append(("park+conf", f"{pk}+{cb}"))
    out.append(("park+line", f"{pk}+{lb}"))
    out.append(("park+strong+line", f"{pk}+{'strongPit' if strong else 'softPit'}+{lb}"))
    return out


groups = defaultdict(list)
for r in rows:
    for _, c in cells(r):
        groups[c].append(r)

res = []
for c, sub in groups.items():
    if len(sub) < 30:
        continue
    h = sum(1 for x in sub if x["hit"]); n = len(sub)
    first = [x for x in sub if x["pick_date"] < mid]
    second = [x for x in sub if x["pick_date"] >= mid]
    r1 = sum(1 for x in first if x["hit"])/len(first) if first else 0
    r2 = sum(1 for x in second if x["hit"])/len(second) if second else 0
    stable = len(first) >= 10 and len(second) >= 10 and r1 >= 0.62 and r2 >= 0.62
    res.append((wlb(h, n), h/n, h, n, r1, r2, stable, c))

res.sort(reverse=True)
print(f"{'LB':>5} {'rate':>5} {'h/n':>8} {'1st':>4} {'2nd':>4} {'stable':>7}  filter")
print("-"*70)
for lb, rate, h, n, r1, r2, stable, c in res:
    print(f"{lb:5.2f} {rate:5.0%} {h:3}/{n:<3} {r1:4.0%} {r2:4.0%} {'✅' if stable else '  ':>7}  {c}")
