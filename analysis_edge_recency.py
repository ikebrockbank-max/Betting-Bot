"""Recency-aware edge hunt: for every market/filter pocket, compare ALL-TIME vs
LAST-30d hit rate, so we can tell (a) what's still working now, (b) what's newly
emerging, and (c) what DECAYED (the star/lock cooldown). Guardrails: report a
pocket only if last-30d n >= 15. Rank by last-30d Wilson lower bound (what's
trustworthy NOW), and separately list the biggest decayers."""
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta
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


rows = _sb_fetch("select=sport,stat_type,direction,line,confidence,pitcher_tier,"
                 "park_factor,home_away,batting_order,edge_pct,result,pick_date"
                 "&resolved=eq.true")
rows = [r for r in rows if r.get("result") in ("hit", "miss")]
today = (datetime.now(timezone.utc) - timedelta(hours=4)).date()
cut30 = (today - timedelta(days=30)).strftime("%Y-%m-%d")
for r in rows:
    r["confidence"] = _f(r, "confidence"); r["park_factor"] = _f(r, "park_factor")
    r["line"] = _f(r, "line"); r["pitcher_tier"] = r.get("pitcher_tier") or "?"
    r["home_away"] = (r.get("home_away") or "?").lower()
    r["hit"] = r["result"] == "hit"
    r["recent"] = r.get("pick_date", "") >= cut30
print(f"resolved: {len(rows)} | last-30d cutoff {cut30} "
      f"({sum(1 for r in rows if r['recent'])} recent)\n")


def bands(r):
    c = r["confidence"]
    cb = ("c75+" if c >= 0.75 else "c70-75" if c >= 0.70 else "c<70")
    pf = r["park_factor"]
    pb = ("parkHI" if pf >= 1.0 else "parkMID" if pf >= 0.95 else "parkLOW") if pf else "park?"
    bo = r.get("batting_order")
    try:
        bo = int(bo); bob = "bo1-2" if bo <= 2 else "bo3-5" if bo <= 5 else "bo6-9"
    except (TypeError, ValueError):
        bob = "bo?"
    sd = f"{r['sport']}|{r['stat_type']}|{r['direction']}"
    return [sd, f"{sd}|{cb}", f"{sd}|{pb}", f"{sd}|pit:{r['pitcher_tier']}",
            f"{sd}|{r['home_away']}", f"{sd}|{bob}", f"{sd}|{pb}|{r['home_away']}",
            f"{sd}|{cb}|pit:{r['pitcher_tier']}"]


agg = defaultdict(lambda: {"ah": 0, "an": 0, "rh": 0, "rn": 0})
for r in rows:
    for k in bands(r):
        a = agg[k]
        a["an"] += 1; a["ah"] += r["hit"]
        if r["recent"]:
            a["rn"] += 1; a["rh"] += r["hit"]

# What's working NOW: last-30d n>=15, rate>=60%, ranked by recent Wilson LB
print("=== WORKING NOW (last-30d n>=15, rate>=60%, by recent Wilson LB) ===")
hot = []
for k, a in agg.items():
    if a["rn"] >= 15 and a["rh"]/a["rn"] >= 0.60:
        hot.append((wlb(a["rh"], a["rn"]), a["rh"]/a["rn"], a["rh"], a["rn"],
                    a["ah"]/a["an"], a["an"], k))
for lb, rr, rh, rn, ar, an, k in sorted(hot, reverse=True)[:25]:
    print(f"  now {rr:4.0%} ({rh:3}/{rn:<3}) LB{lb:.2f} | all-time {ar:4.0%} (n={an:<4}) | {k}")
if not hot:
    print("  (nothing clears last-30d n>=15 & >=60%)")

# Biggest DECAYERS: all-time strong, recent weak (the cooldown)
print("\n=== DECAYED (all-time>=65% n>=40, recent n>=15, drop>=10pts) ===")
dec = []
for k, a in agg.items():
    if a["an"] >= 40 and a["rn"] >= 15 and a["ah"]/a["an"] >= 0.65:
        drop = a["ah"]/a["an"] - a["rh"]/a["rn"]
        if drop >= 0.10:
            dec.append((drop, a["ah"]/a["an"], a["an"], a["rh"]/a["rn"], a["rn"], k))
for drop, ar, an, rr, rn, k in sorted(dec, reverse=True)[:20]:
    print(f"  -{drop*100:2.0f}pts | all-time {ar:.0%} (n={an}) -> now {rr:.0%} ({rn}) | {k}")
if not dec:
    print("  (no significant decayers)")
