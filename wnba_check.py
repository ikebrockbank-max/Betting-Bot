"""Diagnostic: every WNBA row ever logged to pick_log — how many, resolved
or not, which stat_types (incl. the (WNBAhot) tag), date range, and a few
samples. Answers 'have we ever sent/logged WNBA picks?'."""
from collections import Counter
from calibration_tracker import _sb_fetch

rows = _sb_fetch("select=player,stat_type,direction,line,pick_date,result,resolved,home_away&sport=eq.WNBA")
print(f"TOTAL WNBA rows in pick_log: {len(rows)}")
if rows:
    dates = sorted(r.get("pick_date", "") for r in rows if r.get("pick_date"))
    print(f"date range: {dates[0]} .. {dates[-1]}")
    print(f"resolved: {sum(1 for r in rows if r.get('resolved'))}  "
          f"| hit: {sum(1 for r in rows if r.get('result')=='hit')}  "
          f"| miss: {sum(1 for r in rows if r.get('result')=='miss')}  "
          f"| unresolved: {sum(1 for r in rows if not r.get('resolved'))}")
    hot = [r for r in rows if '(WNBAhot)' in (r.get('stat_type') or '')]
    print(f"(WNBAhot)-tagged rows: {len(hot)}")
    print("\nby stat_type:")
    for st, n in Counter((r.get('stat_type') or '') for r in rows).most_common():
        print(f"  {n:3d}  {st}")
    print("\nlast 12 rows:")
    for r in sorted(rows, key=lambda x: x.get('pick_date',''))[-12:]:
        print(f"  {r.get('pick_date')}  {r.get('player','?'):22.22} "
              f"{r.get('direction','')} {r.get('line')} {r.get('stat_type','')} "
              f"[{r.get('home_away','')}] -> {r.get('result') or ('unresolved' if not r.get('resolved') else '?')}")
else:
    print("No WNBA rows have ever been logged.")
