"""
Reconstruct the daily top-4 list for recent days and show hit/miss.

Uses pick_log (every scored pick is logged there by the parlay workflow
even on days the ntfy push was blocked), applies the current direction
gate + elite tier + ranking, and prints what the push would have sent
plus how each pick resolved.

Run via adhoc_report.yml. Optional CLI arg = how many days back (default 5).
"""
import sys
from collections import defaultdict
from datetime import date, timedelta

from calibration_tracker import _sb_fetch
from parlay_builder import _passes_direction_gate
from daily_top_picks import _is_elite


def main():
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    since = (date.today() - timedelta(days=days_back)).isoformat()
    params = ("select=pick_date,player,sport,stat_type,direction,line,"
              "confidence,result,hit_rate,edge_pct,pitcher_tier,opp_team"
              f"&pick_date=gte.{since}&order=pick_date.desc")
    rows = _sb_fetch(params)
    for r in rows:
        r["hit_rate"] = float(r.get("hit_rate") or 0)
        r["edge_pct"] = float(r.get("edge_pct") or 0)
        r["line"] = float(r.get("line") or 0)
        r["confidence"] = float(r.get("confidence") or 0)
        r["pitcher_tier"] = r.get("pitcher_tier") or ""

    by_day = defaultdict(list)
    for r in rows:
        if r.get("sport") == "MLB" and _passes_direction_gate(r):
            by_day[r["pick_date"]].append(r)

    tot_h = tot_n = e_h = e_n = 0
    for day in sorted(by_day, reverse=True):
        picks = by_day[day]
        # Same ranking as the daily push: elite first, then confidence.
        # Dedup by player like get_top_picks does.
        picks.sort(key=lambda p: (_is_elite(p), p["confidence"]), reverse=True)
        seen, top = set(), []
        for p in picks:
            if p["player"] not in seen:
                seen.add(p["player"])
                top.append(p)
            if len(top) >= 4:
                break
        print(f"\n=== {day} (top {len(top)} of {len(picks)} gate-passing MLB) ===")
        for p in top:
            star = "⭐" if _is_elite(p) else "  "
            res = (p.get("result") or "pending").upper()
            mark = {"HIT": "✅", "MISS": "❌", "VOID": "⬜"}.get(res, "⏳")
            print(f"  {mark} {star} {p['player']:<24} {p['direction']:<5} "
                  f"{p['line']:<5} {p['stat_type']:<22} conf={p['confidence']:.2f}")
            if res in ("HIT", "MISS"):
                tot_h += res == "HIT"
                tot_n += 1
                if _is_elite(p):
                    e_h += res == "HIT"
                    e_n += 1

    if tot_n:
        print(f"\nTop-4 picks resolved: {tot_h}/{tot_n} = {tot_h/tot_n:.1%}")
    if e_n:
        print(f"Elite (starred) only: {e_h}/{e_n} = {e_h/e_n:.1%}")


if __name__ == "__main__":
    main()
