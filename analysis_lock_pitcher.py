"""Pin the lock fix: goblin-lock hit rate by opposing pitcher_tier, all-time vs
last-30d vs last-14d. The recency sweep showed locks vs below_avg still 87% but
vs average decayed 80->60%. This defines the exact tightened soft-pitcher filter.
Also: is ANY filter rescuing the standard HFS-OVER star market recently?"""
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


rows = _sb_fetch("select=stat_type,direction,park_factor,pitcher_tier,confidence,"
                 "result,pick_date&sport=eq.MLB&resolved=eq.true")
rows = [r for r in rows if r.get("result") in ("hit", "miss")]
today = (datetime.now(timezone.utc) - timedelta(hours=4)).date()
c30 = (today - timedelta(days=30)).strftime("%Y-%m-%d")
c14 = (today - timedelta(days=14)).strftime("%Y-%m-%d")
for r in rows:
    r["pitcher_tier"] = r.get("pitcher_tier") or "?"
    r["hit"] = r["result"] == "hit"


def wr(sub):
    n = len(sub)
    if not n:
        return "  —  "
    h = sum(1 for r in sub if r["hit"])
    return f"{h:3}/{n:<3}={h/n:3.0%}"


def by_tier(name, subset):
    print(f"\n=== {name} by pitcher_tier (all / 30d / 14d) ===")
    tiers = sorted(set(r["pitcher_tier"] for r in subset))
    for t in tiers:
        s = [r for r in subset if r["pitcher_tier"] == t]
        s30 = [r for r in s if r["pick_date"] >= c30]
        s14 = [r for r in s if r["pick_date"] >= c14]
        lb = wlb(sum(1 for r in s30 if r["hit"]), len(s30))
        print(f"  {t:12} all {wr(s)} | 30d {wr(s30)} (LB{lb:.2f}) | 14d {wr(s14)}")


locks = [r for r in rows if "(Goblin)" in (r.get("stat_type") or "") and r["direction"] == "OVER"]
by_tier("GOBLIN LOCKS", locks)

# star market: standard HFS OVER — anything working recently?
stars_mkt = [r for r in rows if r.get("stat_type") == "Hitter Fantasy Score" and r["direction"] == "OVER"]
s30 = [r for r in stars_mkt if r["pick_date"] >= c30]
print(f"\n=== STANDARD HFS OVER (star market): all {wr(stars_mkt)} | 30d {wr(s30)} ===")
by_tier("  standard HFS OVER", stars_mkt)
print("\n  standard HFS OVER, 30d by park:")
for lo, hi, lab in [(1.0, 9, "park>=1.0"), (0.95, 1.0, "0.95-1.0"), (0, 0.95, "<0.95")]:
    s = [r for r in s30 if lo <= _f(r, "park_factor") < hi]
    print(f"    {lab:10} {wr(s)}")
