"""Backtest candidate stricter elite definitions targeting >=60% sustained.
Each candidate is evaluated on hit rate, volume/day, and June-vs-July split
(a rule must hold in BOTH halves to count as real, not curve-fit)."""
import sys
from calibration_tracker import _sb_fetch
from parlay_builder import _passes_direction_gate


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def hfs_core(r, lo=6.0, hi=7.5):
    return (r["stat_type"] == "Hitter Fantasy Score" and r["direction"] == "OVER"
            and lo <= r["line"] <= hi)


CANDS = [
    ("current elite (approx)", lambda r:
        r["confidence"] >= 0.75
        or (hfs_core(r, 6.0, 99) and r["hit_rate"] >= 0.7
            and r["pitcher_tier"] in ("weak", "below_avg", "average", "ace"))
        or (r["stat_type"] == "Pitcher Fantasy Score" and r["direction"] == "OVER"
            and 12 <= r["line"] <= 25 and 0.6 <= r["hit_rate"] < 0.8)),
    ("V2a: HFS 6-7.5, hr .7-.9, not-hot", lambda r:
        hfs_core(r) and 0.7 <= r["hit_rate"] < 0.9
        and not (_f(r, "trend") > 0.15)),
    ("V2b: V2a + p_over>=0.70", lambda r:
        hfs_core(r) and 0.7 <= r["hit_rate"] < 0.9
        and not (_f(r, "trend") > 0.15) and _f(r, "p_over") >= 0.70),
    ("V2c: HFS 6-7.5 + p_over>=0.75 only", lambda r:
        hfs_core(r) and _f(r, "p_over") >= 0.75),
    ("V2d: V2a + PFS prime w/ conf>=0.60", lambda r:
        (hfs_core(r) and 0.7 <= r["hit_rate"] < 0.9 and not (_f(r, "trend") > 0.15))
        or (r["stat_type"] == "Pitcher Fantasy Score" and r["direction"] == "OVER"
            and 12 <= r["line"] <= 25 and 0.6 <= r["hit_rate"] < 0.8
            and r["confidence"] >= 0.60)),
    ("V2e: V2a + conf>=0.62", lambda r:
        hfs_core(r) and 0.7 <= r["hit_rate"] < 0.9
        and not (_f(r, "trend") > 0.15) and r["confidence"] >= 0.62),
]

rows = _sb_fetch("select=sport,stat_type,direction,confidence,result,line,"
                 "edge_pct,hit_rate,pitcher_tier,pick_date,trend,p_over"
                 "&resolved=eq.true&result=neq.void")
rows = [r for r in rows if r.get("result") in ("hit", "miss")
        and r.get("sport") == "MLB"]
for r in rows:
    for k in ("hit_rate", "edge_pct", "line", "confidence"):
        r[k] = _f(r, k)
    r["pitcher_tier"] = r.get("pitcher_tier") or ""
gated = [r for r in rows if _passes_direction_gate(r)]
days = len({r["pick_date"] for r in gated})
print(f"ELITE V2 SEARCH — {len(gated)} gated MLB, {days} days")
print(f"{'candidate':<40} {'n':>4} {'rate':>7} {'/day':>5} {'June':>12} {'July':>12}")
for name, pred in CANDS:
    seg = [r for r in gated if pred(r)]
    if not seg:
        print(f"{name:<40} {'0':>4}")
        continue
    h = sum(r["result"] == "hit" for r in seg)
    jn = [r for r in seg if (r["pick_date"] or "") < "2026-07-01"]
    jl = [r for r in seg if (r["pick_date"] or "") >= "2026-07-01"]
    def rr(x):
        if not x: return "—"
        hh = sum(r["result"] == "hit" for r in x)
        return f"{hh}/{len(x)}={hh/len(x):.0%}"
    print(f"{name:<40} {len(seg):>4} {h/len(seg):>7.1%} {len(seg)/days:>5.1f} "
          f"{rr(jn):>12} {rr(jl):>12}")
