"""
Elite-tier search: the user wants only 1-4 picks/day. Test candidate
"elite" definitions on resolved history and report hit rate + volume
(picks per active day), so the daily push can send a small number of
picks with a defensible probability attached.

All candidates require passing the current direction gate first.
Run via adhoc_report.yml. Optional CLI arg = since-date.
"""
import sys
from collections import defaultdict

from calibration_tracker import _sb_fetch
from parlay_builder import _passes_direction_gate

GOOD_TIERS = {"weak", "below_avg", "average", "ace"}  # everything but above_avg/unknown


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def hfs_prime(r):
    return (r["sport"] == "MLB" and r["stat_type"] == "Hitter Fantasy Score"
            and r["direction"] == "OVER" and _f(r, "line") >= 6.0
            and _f(r, "hit_rate") >= 0.7
            and (r.get("pitcher_tier") or "") in GOOD_TIERS)


def hfs_prime_loose(r):
    return (r["sport"] == "MLB" and r["stat_type"] == "Hitter Fantasy Score"
            and r["direction"] == "OVER" and _f(r, "line") >= 6.0
            and _f(r, "hit_rate") >= 0.7)


def pfs_prime(r):
    return (r["sport"] == "MLB" and r["stat_type"] == "Pitcher Fantasy Score"
            and r["direction"] == "OVER"
            and 0.6 <= _f(r, "hit_rate") < 0.8
            and 12 <= _f(r, "line") <= 25)


def runs_under_prime(r):
    return (r["sport"] == "MLB" and r["stat_type"] == "Runs"
            and r["direction"] == "UNDER"
            and _f(r, "hit_rate") >= 0.7 and _f(r, "edge_pct") >= 0.35)


def conf70(r):
    return _f(r, "confidence") >= 0.70


def conf75(r):
    return _f(r, "confidence") >= 0.75


CANDIDATES = [
    ("A: gate + conf>=0.70", conf70),
    ("B: gate + conf>=0.75", conf75),
    ("C: HFS prime (line>=6, hr>=0.7, good tier)", hfs_prime),
    ("C2: HFS prime loose (no tier req)", hfs_prime_loose),
    ("D: PFS prime (line 12-25, hr .6-.8)", pfs_prime),
    ("E: Runs UNDER prime (hr>=.7, edge>=.35)", runs_under_prime),
    ("F: union C+D+E", lambda r: hfs_prime(r) or pfs_prime(r) or runs_under_prime(r)),
    ("G: union A+C+D+E", lambda r: conf70(r) or hfs_prime(r) or pfs_prime(r)
     or runs_under_prime(r)),
]


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else None
    params = ("select=sport,stat_type,direction,confidence,result,line,"
              "edge_pct,hit_rate,pitcher_tier,pick_date"
              "&resolved=eq.true&result=neq.void")
    if since:
        params += f"&pick_date=gte.{since}"
    rows = [r for r in _sb_fetch(params) if r.get("result") in ("hit", "miss")]
    for r in rows:
        r["hit_rate"] = _f(r, "hit_rate")
        r["edge_pct"] = _f(r, "edge_pct")
        r["line"] = _f(r, "line")
        r["pitcher_tier"] = r.get("pitcher_tier") or ""
    gated = [r for r in rows if _passes_direction_gate(r)]
    days = len({r["pick_date"] for r in gated})
    print(f"ELITE SEARCH — {len(rows)} resolved, {len(gated)} pass gate, "
          f"{days} active days")
    print(f"{'candidate':<44} {'hits':>5} {'n':>5} {'rate':>7} {'per day':>8}")
    for name, pred in CANDIDATES:
        seg = [r for r in gated if pred(r)]
        n = len(seg)
        if not n:
            print(f"{name:<44} {'—':>5} {0:>5}")
            continue
        h = sum(r["result"] == "hit" for r in seg)
        print(f"{name:<44} {h:>5} {n:>5} {h/n:>7.1%} {n/days:>8.1f}")

    # Recency check on the leading candidates — does the edge survive the
    # most recent month, or is it an artifact of the early era?
    print("\nRecency split (G union), by month:")
    by_month = defaultdict(lambda: [0, 0])
    for r in gated:
        if conf70(r) or hfs_prime(r) or pfs_prime(r) or runs_under_prime(r):
            m = (r.get("pick_date") or "")[:7]
            by_month[m][0] += r["result"] == "hit"
            by_month[m][1] += 1
    for m in sorted(by_month):
        h, n = by_month[m]
        print(f"  {m}: {h}/{n} = {h/n:.1%}")


if __name__ == "__main__":
    main()
