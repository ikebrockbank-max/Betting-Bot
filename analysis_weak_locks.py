"""Actual pick-by-pick results for goblin LOCKS vs WEAK (and below_avg)
opposing pitchers — the surviving edge. Lists every graded pick newest-first
with result, plus summary rates."""
from datetime import datetime, timezone, timedelta
from calibration_tracker import _sb_fetch


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


rows = _sb_fetch("select=player,stat_type,direction,line,pitcher_tier,opp_team,"
                 "result,pick_date&sport=eq.MLB&resolved=eq.true")
locks = [r for r in rows if "(Goblin)" in (r.get("stat_type") or "")
         and r["direction"] == "OVER" and r.get("result") in ("hit", "miss")]
today = (datetime.now(timezone.utc) - timedelta(hours=4)).date()
c30 = (today - timedelta(days=30)).strftime("%Y-%m-%d")


def summ(sub, label):
    if not sub:
        print(f"{label}: none"); return
    h = sum(1 for r in sub if r["result"] == "hit")
    r30 = [r for r in sub if r["pick_date"] >= c30]
    h30 = sum(1 for r in r30 if r["result"] == "hit")
    print(f"{label}: all-time {h}/{len(sub)} ({h/len(sub):.0%}) | "
          f"last-30d {h30}/{len(r30)} ({(h30/len(r30)) if r30 else 0:.0%})")


for tier in ("weak", "below_avg"):
    sub = [r for r in locks if (r.get("pitcher_tier") or "") == tier]
    print("=" * 60)
    summ(sub, f"LOCKS vs {tier.upper()} pitcher")
    print("-" * 60)
    for r in sorted(sub, key=lambda x: x.get("pick_date", ""), reverse=True):
        mark = "✅" if r["result"] == "hit" else "❌"
        st = (r["stat_type"].replace(" (Goblin)", ""))
        opp = (r.get("opp_team") or "").split()[-1] if r.get("opp_team") else "?"
        print(f"  {mark} {r['pick_date']} {r['player'][:19]:19} O{_f(r,'line')} {st[:22]:22} vs {opp}")
    print()
