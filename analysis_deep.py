"""
Deep-dive segment analysis of resolved picks in pick_log.

Answers "WHY does a stat×bucket cell hit or miss" by slicing each focus
segment along every logged signal dimension: line value, season hit rate,
edge size, batting order, pitcher tier, home/away, trend, sample size.

Runs on the GitHub runner via adhoc_report.yml (Supabase creds live only
in GitHub Secrets). Optional CLI arg = since-date (YYYY-MM-DD).
"""
import sys
from collections import defaultdict

from calibration_tracker import _sb_fetch

MIN_N = 15


def _rate_str(h: int, n: int) -> str:
    return f"{h:4d}/{n:<4d} = {h / n:5.1%}"


def _slice(rows: list[dict], title: str, keyfn) -> None:
    cells = defaultdict(lambda: [0, 0])
    for r in rows:
        k = keyfn(r)
        if k is None:
            k = "(missing)"
        cells[k][0] += r["result"] == "hit"
        cells[k][1] += 1
    printable = [(k, h, n) for k, (h, n) in cells.items() if n >= MIN_N]
    if not printable:
        return
    print(f"  by {title}:")
    for k, h, n in sorted(printable, key=lambda x: -(x[1] / x[2])):
        print(f"    {str(k):<22} {_rate_str(h, n)}")


def _band(val, edges: list[float], fmt: str = "{lo}-{hi}"):
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi:
            return fmt.format(lo=lo, hi=hi)
    return None


def _analyze(rows: list[dict], label: str) -> None:
    n = len(rows)
    if n < MIN_N:
        print(f"\n### {label}: only {n} picks — skipped")
        return
    h = sum(r["result"] == "hit" for r in rows)
    print(f"\n### {label} — {_rate_str(h, n)}")
    _slice(rows, "confidence", lambda r: _band(r.get("confidence"),
           [0, 0.5, 0.6, 0.65, 0.7, 0.75, 1.01]))
    _slice(rows, "line value", lambda r: _band(r.get("line"),
           [0, 1, 2, 4, 6, 8, 12, 25, 45, 200]))
    _slice(rows, "season hit_rate", lambda r: _band(r.get("hit_rate"),
           [0, 0.4, 0.5, 0.6, 0.7, 0.8, 1.01]))
    _slice(rows, "edge_pct", lambda r: _band(r.get("edge_pct"),
           [0, 0.10, 0.20, 0.35, 0.60, 1.0, 99.0]))
    _slice(rows, "n_games sample", lambda r: _band(r.get("n_games"),
           [0, 5, 8, 11, 99]))
    _slice(rows, "batting_order", lambda r: (
        None if not r.get("batting_order")
        else "1-3" if r["batting_order"] <= 3
        else "4-6" if r["batting_order"] <= 6 else "7-9"))
    _slice(rows, "pitcher_tier", lambda r: r.get("pitcher_tier") or None)
    _slice(rows, "home_away", lambda r: r.get("home_away") or None)
    _slice(rows, "trend", lambda r: (
        None if r.get("trend") is None
        else "hot (L3>L8)" if r["trend"] > 0.15
        else "cold (L3<L8)" if r["trend"] < -0.15 else "flat"))


def main() -> None:
    since = sys.argv[1] if len(sys.argv) > 1 else None
    params = ("select=sport,stat_type,direction,confidence,result,line,"
              "edge_pct,hit_rate,n_games,home_away,pitcher_tier,"
              "batting_order,trend,pick_date"
              "&resolved=eq.true&result=neq.void")
    if since:
        params += f"&pick_date=gte.{since}"
    rows = [r for r in _sb_fetch(params) if r.get("result") in ("hit", "miss")]
    print(f"DEEP DIVE — {len(rows)} resolved picks"
          + (f" since {since}" if since else " (all time)"))

    def seg(sport, stat, direction=None):
        return [r for r in rows
                if r.get("sport") == sport and r.get("stat_type") == stat
                and (direction is None or r.get("direction") == direction)]

    # Winners — what exactly is carrying them
    _analyze(seg("MLB", "Hitter Fantasy Score", "OVER"), "MLB HFS OVER (the volume stat)")
    _analyze(seg("MLB", "Pitcher Fantasy Score", "OVER"), "MLB PFS OVER")
    _analyze(seg("MLB", "Pitcher Fantasy Score", "UNDER"), "MLB PFS UNDER")
    _analyze(seg("MLB", "Runs", "UNDER"), "MLB Runs UNDER (top matrix cell)")
    _analyze(seg("MLB", "Runs", "OVER"), "MLB Runs OVER")
    _analyze(seg("MLB", "Total Bases", "OVER"), "MLB Total Bases OVER")
    _analyze(seg("MLB", "Hits", "UNDER"), "MLB Hits UNDER")

    # Losers — where the bleed comes from
    _analyze(seg("MLB", "Hits+Runs+RBIs", "OVER"), "MLB H+R+RBI OVER (loser: why)")
    _analyze(seg("MLB", "Singles", "OVER"), "MLB Singles OVER (loser: why)")
    _analyze(seg("MLB", "Pitcher Strikeouts"), "MLB Pitcher Ks (loser: why)")
    _analyze(seg("MLB", "Walks Allowed"), "MLB Walks Allowed (loser: why)")
    _analyze(seg("WNBA", "Points"), "WNBA Points (loser: why)")
    _analyze(seg("WNBA", "Rebounds"), "WNBA Rebounds (worst stat: why)")


if __name__ == "__main__":
    main()
