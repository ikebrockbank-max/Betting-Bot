"""Recent-window hit rate by sent-tier (stars/primes/locks), last 7 and 14
days. Note: the most recent 1-2 days may not be resolved in the DB yet (the
scorecard grades those live), so recent windows can lag by a day or two."""
from datetime import datetime, timezone, timedelta
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite, _is_prime


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def _tier(r):
    st = r.get("stat_type") or ""
    if "(Goblin)" in st:  return "lock"
    if "(WNBAhot)" in st: return "wnba"
    if _is_prime(r):      return "prime"
    if _is_elite(r):      return "star"
    return None


today = (datetime.now(timezone.utc) - timedelta(hours=4)).date()
since = (today - timedelta(days=14)).strftime("%Y-%m-%d")
rows = _sb_fetch("select=player,stat_type,direction,line,p_over,pitcher_tier,"
                 f"park_factor,result,pick_date&resolved=eq.true&pick_date=gte.{since}")
for r in rows:
    r["line"] = _f(r, "line"); r["park_factor"] = _f(r, "park_factor")
    r["pitcher_tier"] = r.get("pitcher_tier") or ""

def rate(bucket):
    b = [r for r in bucket if r.get("result") in ("hit", "miss")]
    if not b: return "—"
    h = sum(1 for r in b if r["result"] == "hit")
    return f"{h}/{len(b)} ({h/len(b):.0%})"

for label, days in (("last 7d", 7), ("last 14d", 14)):
    cut = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    win = [r for r in rows if r.get("pick_date", "") >= cut]
    print(f"\n{label} (resolved, since {cut}):")
    for t in ("prime", "star", "lock"):
        sel = [r for r in win if _tier(r) == t]
        print(f"  {t:6}: {rate(sel)}")

# star detail last 14d so we can see the individual results
print("\nSTAR picks last 14d (individual):")
stars = sorted([r for r in rows if _tier(r) == "star" and r.get("result") in ("hit","miss")],
               key=lambda x: x["pick_date"])
for r in stars:
    mark = "✅" if r["result"] == "hit" else "❌"
    print(f"  {mark} {r['pick_date']} {r['player'][:20]:20} O{r['line']} p_over {r.get('p_over')}")
