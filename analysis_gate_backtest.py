"""
Backtest _passes_direction_gate against the resolved pick_log history.

Answers: if the current gate had been active all along, what hit rate
would the surviving picks have had vs. what actually happened? Also shows
what each individual segment gate removed and at what hit rate, so a rule
that filters winners gets caught before it costs money.

Run via adhoc_report.yml (Supabase creds are Actions-only).
Optional CLI arg = since-date (YYYY-MM-DD).
"""
import sys

from calibration_tracker import _sb_fetch
from parlay_builder import (
    _passes_direction_gate,
    MLB_HITTER_STATS,
    MIN_HITTER_OVER_HIT_RATE,
    BLOCKED_PITCHER_TIERS_FOR_HITTER_OVER,
    MIN_HFS_OVER_LINE,
    MAX_EDGE_COUNTING_STATS,
    WNBA_BLOCKED_STATS,
    EXCLUDED_STAT_TYPES,
    UNDER_EXCEPTIONS,
    MIN_HIT_RATE_UNDER_EXCEPTION,
    PARLAY_OVERS_ONLY,
)


def _rate(rows):
    n = len(rows)
    if not n:
        return "0/0"
    h = sum(r["result"] == "hit" for r in rows)
    return f"{h}/{n} = {h / n:.1%}"


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else None
    params = ("select=sport,stat_type,direction,confidence,result,line,"
              "edge_pct,hit_rate,pitcher_tier,pick_date"
              "&resolved=eq.true&result=neq.void")
    if since:
        params += f"&pick_date=gte.{since}"
    rows = [r for r in _sb_fetch(params) if r.get("result") in ("hit", "miss")]
    # Gate expects the same keys score_pick emits; nulls become gate-friendly defaults
    for r in rows:
        r["hit_rate"] = float(r.get("hit_rate") or 0)
        r["edge_pct"] = float(r.get("edge_pct") or 0)
        r["line"] = float(r.get("line") or 0)
        r["pitcher_tier"] = r.get("pitcher_tier") or ""

    print(f"GATE BACKTEST — {len(rows)} resolved picks"
          + (f" since {since}" if since else " (all time)"))
    print(f"\nAll picks (no gate):        {_rate(rows)}")

    passed = [r for r in rows if _passes_direction_gate(r)]
    blocked = [r for r in rows if not _passes_direction_gate(r)]
    print(f"Pass current gate:          {_rate(passed)}")
    print(f"Blocked by current gate:    {_rate(blocked)}")

    # Per-rule attribution: what does each segment rule block, and at what
    # hit rate? Evaluated independently (a pick can trip several rules).
    def hitter_over(r):
        return (r["sport"] == "MLB" and r["direction"] == "OVER"
                and r["stat_type"] in MLB_HITTER_STATS)

    rules = [
        ("excluded stat types",
         lambda r: r["stat_type"] in EXCLUDED_STAT_TYPES
         and not (r["stat_type"] in UNDER_EXCEPTIONS and r["direction"] == "UNDER"
                  and r["hit_rate"] >= MIN_HIT_RATE_UNDER_EXCEPTION)),
        ("UNDER (overs-only policy)",
         lambda r: PARLAY_OVERS_ONLY and r["direction"] != "OVER"
         and not (r["stat_type"] in UNDER_EXCEPTIONS
                  and r["hit_rate"] >= MIN_HIT_RATE_UNDER_EXCEPTION)),
        ("WNBA Points/Rebounds",
         lambda r: r["sport"] == "WNBA" and r["stat_type"] in WNBA_BLOCKED_STATS),
        (f"hitter OVER season hr < {MIN_HITTER_OVER_HIT_RATE}",
         lambda r: hitter_over(r) and r["hit_rate"] < MIN_HITTER_OVER_HIT_RATE),
        ("hitter OVER vs above_avg pitcher",
         lambda r: hitter_over(r)
         and r["pitcher_tier"] in BLOCKED_PITCHER_TIERS_FOR_HITTER_OVER),
        (f"HFS OVER line < {MIN_HFS_OVER_LINE}",
         lambda r: hitter_over(r) and r["stat_type"] == "Hitter Fantasy Score"
         and r["line"] < MIN_HFS_OVER_LINE),
        ("Runs OVER edge > 100%",
         lambda r: hitter_over(r)
         and MAX_EDGE_COUNTING_STATS.get(r["stat_type"]) is not None
         and r["edge_pct"] > MAX_EDGE_COUNTING_STATS[r["stat_type"]]),
    ]
    print("\nPer-rule attribution (independent; blocked picks' hit rate —")
    print("a rule is GOOD when the rate it blocks is well under 52.4%):")
    for name, pred in rules:
        seg = [r for r in rows if pred(r)]
        print(f"  {name:<38} blocks {_rate(seg)}")

    # Survivors by confidence band — what the daily push would draw from
    print("\nSurvivors by confidence band:")
    bands = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.65), (0.65, 0.7), (0.7, 1.01)]
    for lo, hi in bands:
        seg = [r for r in passed if lo <= float(r.get("confidence") or 0) < hi]
        print(f"  conf {lo:.2f}-{hi:.2f}: {_rate(seg)}")


if __name__ == "__main__":
    main()
