"""Deeper ERA-Allowed UNDER dig: list the actual pitcher-park picks with
results, and test more dimensions (home/away, opponent, line value, recency,
frequency) to see how robust/actionable the ~70-74% pocket really is."""
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


rows = _sb_fetch("select=player,line,confidence,edge_pct,pitcher_tier,park_factor,"
                 "home_away,opp_team,day_of_week,result,pick_date&sport=eq.MLB"
                 "&stat_type=eq.Earned%20Runs%20Allowed&direction=eq.UNDER&resolved=eq.true")
rows = [r for r in rows if r.get("result") in ("hit", "miss")]
for r in rows:
    r["line"] = _f(r, "line"); r["park_factor"] = _f(r, "park_factor")
    r["edge_pct"] = _f(r, "edge_pct"); r["hit"] = r["result"] == "hit"
    r["home_away"] = (r.get("home_away") or "?").lower()

low = [r for r in rows if r["park_factor"] and r["park_factor"] < 1.0]
h = sum(1 for r in low if r["hit"])
print(f"ERA-UNDER park<1.0: {h}/{len(low)} = {h/len(low):.0%} (Wilson LB {wlb(h,len(low)):.2f})")

# recency: is it still working?
today = (datetime.now(timezone.utc) - timedelta(hours=4)).date()
for days in (14, 30):
    cut = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    w = [r for r in low if r["pick_date"] >= cut]
    if w:
        wh = sum(1 for r in w if r["hit"])
        print(f"  last {days}d: {wh}/{len(w)} = {wh/len(w):.0%}")
# frequency
dates = sorted(set(r["pick_date"] for r in low))
if dates:
    span = (datetime.strptime(dates[-1], "%Y-%m-%d") - datetime.strptime(dates[0], "%Y-%m-%d")).days + 1
    print(f"  frequency: {len(low)} picks over {span} days ≈ {len(low)/max(span,1)*7:.1f}/week")


def bucket(name, keyfn):
    print(f"\nby {name}:")
    g = defaultdict(lambda: [0, 0])
    for r in low:
        k = keyfn(r)
        g[k][1] += 1; g[k][0] += 1 if r["hit"] else 0
    for k in sorted(g, key=lambda x: -g[x][1]):
        hh, nn = g[k]
        if nn >= 5:
            print(f"  {str(k):16} {hh:2}/{nn:<2} = {hh/nn:.0%}")


bucket("home/away", lambda r: r["home_away"])
bucket("line", lambda r: f"line {r['line']}")
bucket("pitcher_tier", lambda r: r["pitcher_tier"] or "?")
bucket("park band", lambda r: "0.97-1.0" if r["park_factor"] >= 0.97 else "0.95-0.97" if r["park_factor"] >= 0.95 else "<0.95")
bucket("opponent", lambda r: (r.get("opp_team") or "?").split()[-1])

print("\n=== the actual picks (park<1.0), newest first ===")
for r in sorted(low, key=lambda x: x["pick_date"], reverse=True)[:35]:
    mark = "✅" if r["hit"] else "❌"
    print(f"  {mark} {r['pick_date']} {r['player'][:18]:18} U{r['line']} "
          f"park{r['park_factor']:.2f} {r['home_away'][:4]:4} vs {(r.get('opp_team') or '?').split()[-1]}")
