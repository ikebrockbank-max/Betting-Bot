"""Lock hit rate over trailing windows (30/14/7 days) + overall. Locks are
tagged with a ' (Goblin)' suffix in pick_log."""
from datetime import datetime, timezone, timedelta
from calibration_tracker import _sb_fetch

today = (datetime.now(timezone.utc) - timedelta(hours=4)).date()
rows = _sb_fetch("select=player,stat_type,result,pick_date&resolved=eq.true")
locks = [r for r in rows
         if r.get("result") in ("hit", "miss") and "(Goblin)" in (r.get("stat_type") or "")]


def rate(days=None):
    b = locks
    if days is not None:
        cut = today - timedelta(days=days)
        b = [r for r in locks
             if datetime.strptime(r["pick_date"], "%Y-%m-%d").date() >= cut]
    n = len(b)
    h = sum(1 for r in b if r["result"] == "hit")
    return h, n

for lbl, d in [("Last 30d", 30), ("Last 14d", 14), ("Last 7d", 7), ("All-time", None)]:
    h, n = rate(d)
    pct = f"{h/n:.0%}" if n else "—"
    print(f"🔒 Locks {lbl:9}: {h}/{n} = {pct}")

# also list the misses in the last 30d so we can see what broke
cut = today - timedelta(days=30)
misses = [r for r in locks
          if r["result"] == "miss"
          and datetime.strptime(r["pick_date"], "%Y-%m-%d").date() >= cut]
print(f"\nlast-30d lock MISSES ({len(misses)}):")
for r in sorted(misses, key=lambda x: x["pick_date"]):
    print(f"  ❌ {r['pick_date']}  {r['player']}  {r['stat_type'].replace(' (Goblin)','')}")
