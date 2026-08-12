"""FULL edge hunt — systematically slice the resolved pick_log to find NEW
pockets that hit >=70% with enough sample to trust and stability across time.

Guardrails against curve-fit false positives:
  - n >= 25 (a "great" rate on n<25 is indistinguishable from luck)
  - Wilson 95% lower bound >= 0.62 (penalises small samples)
  - era split: rate in first half vs second half of the date range must BOTH
    be >= 0.60 (a real edge persists; a curve-fit one lives in one era)

Slices: 1-way (sport x stat x direction), then 2-way and 3-way crosses on the
signals most likely to carry an edge (confidence, pitcher_tier, park band,
home/away, batting order, day of week, edge magnitude).
"""
import math
from collections import defaultdict
from calibration_tracker import _sb_fetch


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def wilson_lb(h, n, z=1.96):
    if n == 0:
        return 0.0
    p = h / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (c - m) / d


rows = _sb_fetch("select=sport,stat_type,direction,line,confidence,p_over,p_under,"
                 "edge_pct,pitcher_tier,park_factor,home_away,batting_order,day_of_week,"
                 "trend,rest_days,avg_val,n_games,result,pick_date&resolved=eq.true")
rows = [r for r in rows if r.get("result") in ("hit", "miss")]
for r in rows:
    for k in ("line", "confidence", "edge_pct", "park_factor", "avg_val", "trend"):
        r[k] = _f(r, k)
    r["pitcher_tier"] = r.get("pitcher_tier") or "?"
    r["home_away"] = (r.get("home_away") or "?").lower()
    r["hit"] = r["result"] == "hit"
dates = sorted(r["pick_date"] for r in rows if r.get("pick_date"))
mid = dates[len(dates) // 2] if dates else "2026-07-15"
print(f"resolved picks: {len(rows)} | date split at {mid}\n")


def band_conf(r):
    c = r["confidence"]
    for lo, hi in [(0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 1.01)]:
        if lo <= c < hi:
            return f"conf{int(lo*100)}-{int(hi*100)}"
    return "conf<60"


def band_park(r):
    p = r["park_factor"]
    if p <= 0:
        return "park?"
    return "park>=1.0" if p >= 1.0 else ("park0.98-1.0" if p >= 0.98 else "park<0.98")


def band_bo(r):
    bo = r.get("batting_order")
    try:
        bo = int(bo)
    except (TypeError, ValueError):
        return "bo?"
    if bo <= 2:
        return "bo1-2"
    if bo <= 5:
        return "bo3-5"
    return "bo6-9"


def evaluate(label, subset):
    n = len(subset)
    if n < 25:
        return None
    h = sum(1 for r in subset if r["hit"])
    rate = h / n
    if rate < 0.70:
        return None
    lb = wilson_lb(h, n)
    if lb < 0.62:
        return None
    first = [r for r in subset if r["pick_date"] < mid]
    second = [r for r in subset if r["pick_date"] >= mid]
    r1 = (sum(1 for r in first if r["hit"]) / len(first)) if first else 0
    r2 = (sum(1 for r in second if r["hit"]) / len(second)) if second else 0
    stable = (len(first) >= 8 and len(second) >= 8 and r1 >= 0.60 and r2 >= 0.60)
    return (lb, rate, h, n, r1, r2, len(first), len(second), stable, label)


# Build slice keys per row
found = []
by = defaultdict(list)
for r in rows:
    sp, st, d = r["sport"], r["stat_type"], r["direction"]
    keys = [
        f"{sp}|{st}|{d}",
        f"{sp}|{st}|{d}|{band_conf(r)}",
        f"{sp}|{st}|{d}|{r['pitcher_tier']}",
        f"{sp}|{st}|{d}|{band_park(r)}",
        f"{sp}|{st}|{d}|{r['home_away']}",
        f"{sp}|{st}|{d}|{band_bo(r)}",
        f"{sp}|{st}|{d}|dow{r.get('day_of_week')}",
        # 2-way crosses (MLB signal-rich)
        f"{sp}|{st}|{d}|{r['pitcher_tier']}|{band_park(r)}",
        f"{sp}|{st}|{d}|{band_conf(r)}|{r['pitcher_tier']}",
        f"{sp}|{st}|{d}|{band_conf(r)}|{r['home_away']}",
        f"{sp}|{st}|{d}|{band_park(r)}|{r['home_away']}",
        f"{sp}|{st}|{d}|{band_bo(r)}|{r['pitcher_tier']}",
    ]
    for k in keys:
        by[k].append(r)

for label, subset in by.items():
    res = evaluate(label, subset)
    if res:
        found.append(res)

found.sort(reverse=True)
print(f"{'WilsonLB':>8} {'rate':>5} {'hits':>5} {'n':>4}  {'1st':>4} {'2nd':>4} {'stable':>6}  slice")
print("-" * 100)
for lb, rate, h, n, r1, r2, n1, n2, stable, label in found[:30]:
    flag = "✅STABLE" if stable else "  (era-thin/unstable)"
    print(f"{lb:8.2f} {rate:5.0%} {h:5} {n:4}  {r1:4.0%} {r2:4.0%} {flag:>10}  {label}")

if not found:
    print("No pocket cleared n>=25, rate>=70%, WilsonLB>=0.62.")
