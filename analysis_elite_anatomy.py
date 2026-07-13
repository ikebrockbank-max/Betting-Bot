"""
Anatomy of the elite/top-pick pool: which features separate the hits
from the misses INSIDE the picks we actually surface, and what a 75%
subtier would have to look like.

Run via adhoc_report.yml. Optional CLI arg = since-date.
"""
import sys
from collections import defaultdict

from calibration_tracker import _sb_fetch
from parlay_builder import _passes_direction_gate
from daily_top_picks import _is_elite


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def _slice(rows, title, keyfn, min_n=10):
    cells = defaultdict(lambda: [0, 0])
    for r in rows:
        k = keyfn(r)
        if k is None:
            continue
        cells[k][0] += r["result"] == "hit"
        cells[k][1] += 1
    printable = [(k, h, n) for k, (h, n) in cells.items() if n >= min_n]
    if printable:
        print(f"  by {title}:")
        for k, h, n in sorted(printable, key=lambda x: -(x[1] / x[2])):
            print(f"    {str(k):<26} {h:3d}/{n:<4d} = {h/n:.1%}")


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else None
    params = ("select=sport,stat_type,direction,confidence,result,line,"
              "edge_pct,hit_rate,pitcher_tier,pick_date,home_away,trend,"
              "n_games,adj_hit_rate,p_over,p_under"
              "&resolved=eq.true&result=neq.void&sport=eq.MLB")
    if since:
        params += f"&pick_date=gte.{since}"
    rows = [r for r in _sb_fetch(params) if r.get("result") in ("hit", "miss")]
    for r in rows:
        for k in ("hit_rate", "edge_pct", "line", "confidence", "adj_hit_rate"):
            r[k] = _f(r, k)
        r["pitcher_tier"] = r.get("pitcher_tier") or ""
    gated = [r for r in rows if _passes_direction_gate(r)]
    elite = [r for r in gated if _is_elite(r)]
    h = sum(r["result"] == "hit" for r in elite)
    print(f"ELITE ANATOMY — {len(rows)} MLB resolved, {len(gated)} gated, "
          f"{len(elite)} elite → {h}/{len(elite)} = {h/max(len(elite),1):.1%}")

    print("\n### Inside the elite pool:")
    _slice(elite, "stat_type", lambda r: f"{r['stat_type']} {r['direction']}")
    _slice(elite, "line band (HFS only)", lambda r: (
        None if r["stat_type"] != "Hitter Fantasy Score"
        else "6-6.5" if r["line"] <= 6.5
        else "7-7.5" if r["line"] <= 7.5 else "8+"))
    _slice(elite, "season hit_rate", lambda r: (
        "0.9+" if r["hit_rate"] >= 0.9 else "0.8-0.9" if r["hit_rate"] >= 0.8
        else "0.7-0.8" if r["hit_rate"] >= 0.7 else "<0.7"))
    _slice(elite, "pitcher_tier", lambda r: r["pitcher_tier"] or "(missing)")
    _slice(elite, "p_over (model prob)", lambda r: (
        None if not r.get("p_over") else
        "0.75+" if _f(r, "p_over") >= 0.75 else
        "0.65-0.75" if _f(r, "p_over") >= 0.65 else "<0.65"))
    _slice(elite, "trend", lambda r: (
        None if r.get("trend") is None
        else "hot" if _f(r, "trend") > 0.15
        else "cold" if _f(r, "trend") < -0.15 else "flat"))
    _slice(elite, "home_away", lambda r: r.get("home_away") or None)

    # The 75% question: does ANY objective subtier of the whole gated pool
    # sustain >= 70%? Stack the best features and report honestly.
    print("\n### Stacked filters toward 75% (whole gated MLB pool):")
    combos = [
        ("hr>=0.8 & tier weak/below & HFS OVER 6+",
         lambda r: r["stat_type"] == "Hitter Fantasy Score"
         and r["direction"] == "OVER" and r["line"] >= 6
         and r["hit_rate"] >= 0.8 and r["pitcher_tier"] in ("weak", "below_avg")),
        ("hr>=0.8 & conf>=0.65", lambda r: r["hit_rate"] >= 0.8
         and r["confidence"] >= 0.65),
        ("hr>=0.9 (any stat, OVER)", lambda r: r["hit_rate"] >= 0.9
         and r["direction"] == "OVER"),
        ("p_over>=0.75 & hr>=0.7", lambda r: _f(r, "p_over") >= 0.75
         and r["hit_rate"] >= 0.7 and r["direction"] == "OVER"),
        ("adj_hit_rate>=0.75 & conf>=0.62", lambda r: r["adj_hit_rate"] >= 0.75
         and r["confidence"] >= 0.62),
    ]
    days = len({r["pick_date"] for r in gated})
    for name, pred in combos:
        seg = [r for r in gated if pred(r)]
        n = len(seg)
        if n:
            hh = sum(r["result"] == "hit" for r in seg)
            print(f"  {name:<44} {hh:3d}/{n:<4d} = {hh/n:.1%}  ({n/max(days,1):.1f}/day)")
        else:
            print(f"  {name:<44} 0 picks")


if __name__ == "__main__":
    main()
